"""Bounded zero-spend fixture dispatcher. Remote execution is unavailable.

Claims commit before execution. Immutable private receipts commit to disk before
the fenced database completion; subsequent ticks reconcile those same receipts.
No document parser, scanner, provider, research or network adapter is invoked.
"""
import hashlib
import secrets
from datetime import timedelta
from django.conf import settings
from django.db.models import F, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from recruiting.assessment import validate_assessment
from recruiting.models import Candidate, Job
from recruiting.samples import sample_interviews
from . import state
from .artifacts import AttemptArtifacts, encode
from .contracts import TERMINAL_STAGES
from .models import CloudBatch, CloudOutbox
from .sample_contract import verified_sample_inputs
from .storage import storage_admission

LEASE_SECONDS = 120
MAX_ATTEMPTS = 5


def _pending_ids(limit):
    now = timezone.now()
    return list(CloudOutbox.objects.filter(kind='dispatch', state__in=['pending','running'])
        .filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now))
        .order_by('created_at','pk').values_list('pk', flat=True)[:limit])


@state._retry_locked
def _claim(event_id):
    identity = CloudOutbox.objects.filter(pk=event_id, kind='dispatch').first()
    if identity is None: return None
    with storage_admission(), state._locked_batch(identity.batch_id) as (profile, batch):
        event = CloudOutbox.objects.select_for_update().filter(pk=event_id).first()
        now = timezone.now()
        if not batch or not event or event.state not in ('pending','running') or (event.lease_until and event.lease_until > now):
            return None
        if batch.stage != event.stage or state._invalidate(profile, batch):
            event.state = 'cancelled'
            event.lease_token, event.lease_until = '', None
            event.save(update_fields=['state','lease_token','lease_until'])
            return None
        # Reuse the active attempt even if its execution lease elapsed. A durable
        # receipt must be checked before expiration, never a new paid submission.
        attempt = batch.attempts.select_for_update().filter(stage=event.stage, state='running').first()
        if not attempt:
            attempt = state.claim_stage(batch.pk)
        if not attempt: return None
        event.state, event.lease_token = 'running', secrets.token_hex(24)
        event.attempts += 1
        event.lease_until = now+timedelta(seconds=LEASE_SECONDS)
        event.save(update_fields=['state','lease_token','attempts','lease_until'])
        return event, attempt


def _fence(batch, attempt):
    return {'account_key':str(batch.account_key), 'account_epoch':batch.account_epoch,
        'input_manifest_digest':batch.input_manifest_digest, 'rubric_version':batch.rubric_version,
        'lease_token':attempt.lease_token}


def _publish(event, attempt):
    from .fixtures import evaluate_fixture
    with storage_admission(), state._locked_batch(event.batch_id) as (profile, current):
        claimed = CloudOutbox.objects.select_for_update().filter(pk=event.pk,
            batch_id=event.batch_id, kind='dispatch', state='running', lease_token=event.lease_token).first()
        if not current or not claimed or claimed.lease_until <= timezone.now() or current.stage != attempt.stage or claimed.stage != current.stage:
            return None
        if state._invalidate(profile,current): return None
        active = current.attempts.select_for_update().filter(pk=attempt.pk,stage=current.stage,state='running').first()
        if not active or (active.account_key != current.account_key or active.account_epoch != current.account_epoch
                or active.input_manifest_digest != current.input_manifest_digest or active.rubric_version != current.rubric_version):
            return None
        artifacts = AttemptArtifacts(current, active)
        # Both the lease fence and this second receipt read belong inside the
        # publication lock. Another owner may have published after the caller's
        # earlier missing-receipt read; reuse that immutable receipt verbatim.
        try:
            return artifacts.read('receipt.json')
        except FileNotFoundError:
            pass
        if active.lease_until <= timezone.now() or claimed.attempts > MAX_ATTEMPTS:
            return None
        inputs = verified_sample_inputs(current)
        envelope = _fence(current, active)
        references = []
        if active.stage == 'validate':
            envelope.update(synthetic=True, validation_method='canonical_fixture',
                expanded_inputs=[{'source_id':str(item.pk), 'filename':item.filename,
                    'byte_size':item.byte_size, 'sha256':item.sha256,
                    'scan_status':'trusted_fixture'} for item, _ in inputs])
        elif active.stage == 'analyze':
            for index, (item, content) in enumerate(inputs):
                result = evaluate_fixture(item.filename, content, current.rubric_snapshot)
                name = f'brief-{index}.json'
                digest = artifacts.publish(name, result)
                references.append({'input_sha256':item.sha256, 'storage_key':artifacts.key(name), 'sha256':digest})
            envelope['results'] = [{key:row[key] for key in ('input_sha256','storage_key')} for row in references]
        else:
            raise ValueError('Canonical fixtures do not require role generation.')
        receipt = {'schema_version':1, 'synthetic':True, 'batch_id':str(current.pk),
            'attempt_id':str(active.pk), 'stage':active.stage,
            'published_at':timezone.now().isoformat(), 'execution_deadline':active.lease_until.isoformat(),
            'envelope':envelope, 'artifacts':references}
        artifacts.publish('receipt.json', receipt)
        return receipt


def _validated_outputs(batch, attempt, receipt, inputs):
    artifacts = AttemptArtifacts(batch, attempt)
    references = receipt.get('artifacts')
    if not isinstance(references, list) or len(references) != len(inputs):
        raise ValueError('Incomplete fictional result artifacts.')
    lookup = {row['input_sha256']:row for row in references if isinstance(row,dict)}
    if len(lookup) != len(inputs): raise ValueError('Duplicate fictional result artifacts.')
    outputs = []
    for index, (item, content) in enumerate(inputs):
        name = f'brief-{index}.json'
        ref = lookup.get(item.sha256)
        if not ref or ref.get('storage_key') != artifacts.key(name):
            raise ValueError('Result artifact outside assigned attempt.')
        raw = artifacts.read_bytes(name)
        if hashlib.sha256(raw).hexdigest() != ref.get('sha256'):
            raise ValueError('Result artifact digest differs.')
        result = artifacts.read(name)
        if set(result) != {'schema_version','synthetic','input_sha256','blocks','assessment','interviews'} or result['schema_version'] != 1 or result['synthetic'] is not True or result['input_sha256'] != item.sha256:
            raise ValueError('Invalid fictional result schema.')
        # These bytes are already proven to be exact built-in text; no customer
        # document parsing or archive expansion is performed here.
        blocks = [{'id':f'b{i+1}', 'text':paragraph, 'location':f'Synthetic sample, paragraph {i+1}'}
            for i, paragraph in enumerate(content.decode('utf-8').split('\n\n'))]
        if result['blocks'] != blocks: raise ValueError('Result evidence differs from trusted source.')
        assessment = validate_assessment(result['assessment'], blocks, batch.rubric_snapshot)
        assessment['sample'] = True
        if result['assessment'] != assessment or result['interviews'] != sample_interviews():
            raise ValueError('Invalid fictional assessment or interview pack.')
        outputs.append((item, result))
    expected = [{'input_sha256':item.sha256, 'storage_key':artifacts.key(f'brief-{index}.json')} for index, (item, _) in enumerate(inputs)]
    if receipt['envelope'].get('results') != expected:
        raise ValueError('Result manifest differs from assigned artifacts.')
    return outputs


@state._retry_locked
def _apply_receipt(event, attempt, receipt):
    """The transition, credits and compatibility reports commit together."""
    with storage_admission(), state._locked_batch(event.batch_id) as (profile, batch):
        current = CloudOutbox.objects.select_for_update().filter(pk=event.pk, state='running', lease_token=event.lease_token).first()
        if not batch or not current or current.lease_until <= timezone.now() or batch.stage != attempt.stage:
            return False
        if state._invalidate(profile, batch): return False
        active = batch.attempts.select_for_update().filter(pk=attempt.pk, state='running').first()
        if not active: return False
        durable = AttemptArtifacts(batch, active).read('receipt.json')
        if encode(durable) != encode(receipt):
            raise ValueError('Completion differs from its immutable private receipt.')
        if not isinstance(receipt,dict) or receipt.get('schema_version') != 1 or receipt.get('synthetic') is not True or receipt.get('batch_id') != str(batch.pk) or receipt.get('attempt_id') != str(active.pk) or receipt.get('stage') != active.stage:
            raise ValueError('Invalid private receipt scope.')
        envelope = receipt.get('envelope')
        if not isinstance(envelope,dict) or any(envelope.get(key) != value for key,value in _fence(batch,active).items()):
            return False
        published = parse_datetime(receipt.get('published_at',''))
        deadline = parse_datetime(receipt.get('execution_deadline',''))
        if not published or not deadline or timezone.is_naive(published) or timezone.is_naive(deadline) or not active.created_at <= published <= deadline <= active.created_at+timedelta(seconds=901):
            raise ValueError('Invalid durable receipt time.')
        inputs = verified_sample_inputs(batch)
        outputs = _validated_outputs(batch,active,receipt,inputs) if active.stage == 'analyze' else []
        # The bytes were durably published during execution. Reconciliation may
        # complete after the execution lease, without rerunning the evaluator.
        if active.lease_until <= timezone.now():
            active.lease_until = timezone.now()+timedelta(seconds=LEASE_SECONDS)
            active.save(update_fields=['lease_until'])
        if not state.apply_transition(batch.pk,active.pk,active.stage,envelope): return False
        for item,result in outputs:
            candidate = Candidate.objects.create(session_id=batch.session_id, filename=item.filename,
                storage_name=item.storage_key, digest=item.sha256, rubric_version=batch.rubric_version,
                blocks=result['blocks'], byte_size=item.byte_size, scan_status='trusted_fixture')
            job = Job.objects.create(candidate=candidate, rubric=batch.rubric_snapshot, jd=batch.jd_text,
                status='completed', reserved=False, result=result['assessment'], interviews=result['interviews'],
                completed_at=timezone.now())
            batch.result_references.filter(input_sha256=item.sha256).update(candidate=candidate,job=job)
        current.state, current.delivered_at = 'delivered', timezone.now()
        current.lease_token, current.lease_until = '', None
        current.save(update_fields=['state','delivered_at','lease_token','lease_until'])
        return True


def _fail(event, attempt):
    with storage_admission(), state._locked_batch(event.batch_id) as (_, batch):
        current = CloudOutbox.objects.select_for_update().filter(pk=event.pk, lease_token=event.lease_token, state='running').first()
        if not batch or not current or current.lease_until <= timezone.now() or batch.stage != attempt.stage: return
        active = batch.attempts.select_for_update().filter(pk=attempt.pk,state='running').first()
        if not active: return
        if active.lease_until <= timezone.now():
            state._finish(batch,'expired','attempt_expired')
        else:
            state.apply_transition(batch.pk,active.pk,active.stage,{**_fence(batch,active),'failed':True})
        batch.refresh_from_db()
        if batch.sealed_at and batch.stage in TERMINAL_STAGES:
            from .notifications import queue_terminal_notice
            queue_terminal_notice(batch)


def _backoff(event):
    with storage_admission(), state._locked_batch(event.batch_id) as (_, batch):
        current = CloudOutbox.objects.select_for_update().filter(pk=event.pk, state='running', lease_token=event.lease_token).first()
        if not batch or not current: return
        current.state, current.lease_token = 'pending', ''
        current.lease_until = timezone.now()+timedelta(seconds=min(300,5*2**min(current.attempts,6)))
        current.save(update_fields=['state','lease_token','lease_until'])


def dispatch_pending(limit=1):
    """Process at most 1..10 queued stage IDs; new stages wait for a later tick."""
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError('Use a dispatch tick limit from 1 to 10.')
    if not settings.AUTOMATIC_WORKFLOW_ENABLED or settings.AUTOMATIC_TRANSPORT != 'fixtures': return 0
    completed = 0
    for event_id in _pending_ids(limit):
        claimed = _claim(event_id)
        if not claimed: continue
        event, attempt = claimed
        batch = event.batch
        try:
            try:
                receipt = AttemptArtifacts(batch,attempt).read('receipt.json')
            except FileNotFoundError:
                if attempt.lease_until <= timezone.now() or event.attempts > MAX_ATTEMPTS:
                    _fail(event,attempt)
                    continue
                receipt = _publish(event,attempt)
                if receipt is None: continue
            completed += int(_apply_receipt(event,attempt,receipt))
        except (ValueError, TypeError, KeyError, UnicodeError):
            _fail(event,attempt)
        except Exception:
            # Database/filesystem failures can contain private details. Keep the
            # stable attempt and its receipt for bounded retries, log no payload.
            _backoff(event)
    return completed


def cleanup_pending(limit=10):
    """Bounded account/batch expiry and never-sealed staging cleanup.

    Execution-attempt expiry belongs to dispatch, which first checks its receipt.
    Do not call the legacy unconditional attempt sweeper from this command.
    """
    if type(limit) is not int or not 1 <= limit <= 10: raise ValueError('Invalid cleanup bound.')
    from .notifications import queue_terminal_notice
    from .storage import cleanup_abandoned_uploads
    ids = list(CloudBatch.objects.exclude(stage__in=TERMINAL_STAGES).filter(
        Q(expires_at__lte=timezone.now()) | Q(owner__profile__cancelled_at__isnull=False)
        | ~Q(owner__profile__status='active') | ~Q(account_epoch=F('owner__profile__account_epoch'))
        ).order_by('expires_at').values_list('pk',flat=True)[:limit])
    expired = 0
    for batch_id in ids:
        with storage_admission(), state._locked_batch(batch_id) as (profile,batch):
            if batch and batch.stage not in TERMINAL_STAGES and state._invalidate(profile,batch):
                if batch.sealed_at: queue_terminal_notice(batch)
                expired += 1
    cleanup_abandoned_uploads(expire=False,limit=limit)
    return expired
