"""Durable lifecycle work. Preview is read-only; production execution is explicit."""
import hashlib
import secrets
import shutil
from datetime import timedelta
from pathlib import Path
from django.conf import settings
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone
from .lifecycle import evaluate_lifecycle
from .models import Profile,LifecycleAction,DeletionTombstone,Job,HiringSession

def execution_allowed():
    if settings.LOCAL_DEVELOPMENT or not settings.LIFECYCLE_EXECUTION_ENABLED:
        raise ValueError('Lifecycle execution requires explicit production configuration.')

def schedule_lifecycle(now=None,dry_run=True):
    now=now or timezone.now()
    if not dry_run: execution_allowed()
    proposals=[]
    for pk in Profile.objects.values_list('pk',flat=True).iterator():
        with transaction.atomic():
            profile=Profile.objects.select_for_update().filter(pk=pk).first()
            if not profile or (profile.status=='archived' and not profile.cancelled_at): continue
            kind=evaluate_lifecycle(profile,now)
            if kind not in ('send_download_notice','delete_account','archive_data_keep_subscription'): continue
            proposals.append({'account':str(profile.account_key),'kind':kind})
            if not dry_run:
                stamp=profile.cancelled_at or profile.last_activity_at
                cycle=hashlib.sha256(f'{profile.account_key}:{kind}:{stamp.isoformat()}'.encode()).hexdigest()
                LifecycleAction.objects.get_or_create(cycle=cycle,defaults={'account':profile,'kind':kind})
    return proposals

def fence_work(profile):
    """DB-only invalidation. Caller holds the profile lock; never touch disk here."""
    from .cloud.models import CloudBatch,CloudOutbox,StageAttempt
    from .cloud.contracts import TERMINAL_STAGES
    profile.account_epoch+=1
    profile.save(update_fields=['account_epoch'])
    batches=CloudBatch.objects.filter(owner_id=profile.user_id)
    now=timezone.now()
    batches.exclude(stage__in=TERMINAL_STAGES).update(stage='cancelled',error_code='account_inactive_or_epoch_changed',reserved_credits=0,finished_at=now)
    StageAttempt.objects.filter(batch__owner_id=profile.user_id,state='running').update(state='cancelled',lease_token='',finished_at=now)
    CloudOutbox.objects.filter(batch__owner_id=profile.user_id).exclude(state='delivered').update(state='cancelled',lease_token='',lease_until=None)
    Job.objects.filter(candidate__session__owner_id=profile.user_id).update(
        reserved=False,lease_token='',lease_until=None,interview_requested=False,
        interview_lease_token='',interview_lease_until=None)
    Job.objects.filter(candidate__session__owner_id=profile.user_id,status__in=['queued','processing']).update(status='cancelled')
    HiringSession.objects.filter(owner_id=profile.user_id,rubric_status__in=['queued','processing']).update(rubric_status='failed',rubric_lease='',rubric_lease_until=None)

def account_folder(profile):
    root=Path(settings.PRIVATE_ROOT).resolve()
    folder=root/str(profile.user_id)
    if folder.is_symlink() or folder.resolve().parent!=root:
        raise ValueError('Unsafe account storage path.')
    return folder

def execute_action(action_id,now=None):
    execution_allowed(); now=now or timezone.now()
    owner=LifecycleAction.objects.filter(pk=action_id).values_list('account_id',flat=True).first()
    if owner is None:
        LifecycleAction.objects.filter(pk=action_id,account__isnull=True).exclude(state__in=['done','cancelled']).update(state='done',finished_at=now,lease_until=None,lease_token='')
        return 'done'
    # Profile always precedes action/job locks. Network work occurs outside locks.
    with transaction.atomic():
        profile=Profile.objects.select_for_update().get(pk=owner)
        action=LifecycleAction.objects.select_for_update().get(pk=action_id)
        if action.state in ('done','cancelled'): return action.state
        if action.kind=='archive_data_keep_subscription' and profile.status=='archived':
            action.state='done'; action.finished_at=now; action.lease_until=None; action.save(); return 'done'
        if action.state=='running' and action.lease_until and action.lease_until>now: return 'running'
        expected=evaluate_lifecycle(profile,now)
        if expected!=action.kind:
            action.state='cancelled'; action.finished_at=now; action.save(); return 'cancelled'
        token=secrets.token_hex(24)
        action.state='running'; action.attempts+=1; action.error=''; action.lease_until=now+timedelta(minutes=15); action.lease_token=token; action.save()
        if action.kind!='send_download_notice':
            profile.status='deleting' if action.kind=='delete_account' else 'archiving'
            profile.save(update_fields=['status']); fence_work(profile)
        email=profile.user.email
    try:
        if action.kind=='send_download_notice':
            submitted=send_mail('Your WebMonkie files: download within three days',
                f'Your unpaid workspace has been inactive for one month. Sign in to keep it active or download your files within three days: {settings.PUBLIC_ORIGIN}/account/',
                settings.DEFAULT_FROM_EMAIL,[email])
            if submitted!=1: raise ValueError('Notice not submitted')
        elif action.kind=='delete_account':
            from .cloud.storage import storage_admission
            from .cloud.lifecycle import remove_account_trees,require_exports_closed,safe_path
            from .archives import archive_path
            # Fencing committed above. Global admission now precedes all locks;
            # byte declarations survive any unlink failure until a successful retry.
            with storage_admission():
                # Status='deleting' was committed before admission. Requests
                # already streaming must close their named export before erase.
                require_exports_closed(profile)
                remove_account_trees(profile)
                archive_path(profile).unlink(missing_ok=True)
                scratch=safe_path(f'.staging/{profile.account_key}')
                if scratch.exists(): shutil.rmtree(scratch)
        elif action.kind=='archive_data_keep_subscription':
            from .archives import archive_account
            archive_account(profile.pk)
        with transaction.atomic():
            profile=Profile.objects.select_for_update().get(pk=owner)
            current=LifecycleAction.objects.select_for_update().get(pk=action_id)
            if current.state!='running' or current.lease_token!=token or current.lease_until<=timezone.now(): return current.state
            if action.kind=='send_download_notice':
                if evaluate_lifecycle(profile,now)!=action.kind:
                    current.state='cancelled'
                else:
                    profile.notice_at=now; profile.save(update_fields=['notice_at']); current.state='done'
            elif action.kind=='delete_account':
                DeletionTombstone.objects.get_or_create(account_key=profile.account_key,defaults={'deleted_at':now})
                User.objects.filter(pk=profile.user_id).delete()
                current.state='done'; current.account=None
            else: current.state='done'
            current.finished_at=now; current.lease_until=None; current.lease_token=''; current.save()
            return current.state
    except Exception:
        # Never persist SMTP errors, paths, tokens or document text.
        LifecycleAction.objects.filter(pk=action_id,state='running',lease_token=token).update(state='failed',error='lifecycle_operation_failed',lease_until=None,lease_token='')
        return 'failed'

def replay_tombstones(keys,now=None):
    """Apply separately preserved deletion ledger before a restored DB opens."""
    execution_allowed()
    failures=[]
    for key in keys:
        DeletionTombstone.objects.get_or_create(account_key=key,defaults={'deleted_at':now or timezone.now()})
        profile=Profile.objects.filter(account_key=key).first()
        if not profile: continue
        with transaction.atomic():
            profile=Profile.objects.select_for_update().get(pk=profile.pk)
            profile.cancelled_at=(now or timezone.now())-timedelta(days=3)
            profile.status='deleting'
            profile.save(update_fields=['cancelled_at','status'])
            fence_work(profile)
        schedule_lifecycle(now,dry_run=False)
        for action in LifecycleAction.objects.filter(account=profile,kind='delete_account').exclude(state='done'):
            if execute_action(action.pk,now)!='done': failures.append(str(key))
        if Profile.objects.filter(account_key=key).exists() and str(key) not in failures: failures.append(str(key))
    if failures: raise ValueError('Tombstone replay incomplete; keep restored service closed.')
    return {'status':'complete'}
