import secrets
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from .models import HiringSession
from .coordination import lock_profile

def process_one_role():
    HiringSession.objects.filter(rubric_status='processing',rubric_lease_until__lt=timezone.now()).update(rubric_status='failed',rubric_lease='',rubric_lease_until=None,rubric_error='Role analysis interrupted. Please retry.')
    choice=HiringSession.objects.filter(rubric_status='queued').order_by('created_at').values_list('pk','owner_id').first()
    if not choice: return False
    pk,owner_id=choice
    with transaction.atomic():
        profile=lock_profile(owner_id)
        hiring=HiringSession.objects.select_for_update().filter(pk=pk,rubric_status='queued').first()
        if not hiring: return True
        if profile.cancelled_at or profile.status!='active':
            hiring.rubric_status='failed'; hiring.rubric_error='Account is not accepting new analyses.'; hiring.save(); return True
        from .entitlements import require_analysis_access
        try: require_analysis_access(profile)
        except ValueError as exc:
            hiring.rubric_status='failed'; hiring.rubric_error=str(exc); hiring.save(); return True
        token=secrets.token_hex(24)
        hiring.rubric_status='processing'; hiring.rubric_lease=token; hiring.rubric_lease_until=timezone.now()+timedelta(minutes=4); hiring.save()
    try:
        from .providers import propose_rubric
        rubric=propose_rubric(hiring.jd)
        error=''
    except (ValueError,RuntimeError) as exc: rubric=[]; error=str(exc)[:400]
    except Exception: rubric=[]; error='Role analysis interrupted. Please retry.'
    with transaction.atomic():
        profile=lock_profile(owner_id)
        current=HiringSession.objects.select_for_update().filter(pk=pk,rubric_status='processing',rubric_lease=token,rubric_lease_until__gt=timezone.now()).first()
        if current and not profile.cancelled_at and profile.status=='active':
            current.rubric=rubric; current.rubric_status='failed' if error else 'ready'; current.rubric_error=error
            current.rubric_lease=''; current.rubric_lease_until=None; current.save()
    return True
