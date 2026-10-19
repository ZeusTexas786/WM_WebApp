from datetime import timedelta
from django.utils import timezone

def require_analysis_access(profile):
    if profile.cancelled_at or profile.status!='active': raise ValueError('This account is not accepting new analyses.')
    # Paid access is not sold by this release; operator entitlement integration is separate.
    if not profile.verified_at or timezone.now()>=profile.verified_at+timedelta(hours=72):
        raise ValueError('Your three-day trial has ended. Existing results remain available.')
    if profile.completed_credits>=30: raise ValueError('All 30 trial briefs are used.')
