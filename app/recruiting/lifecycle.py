"""Pure lifecycle proposals only. No deletion or archive executor is installed."""
import calendar
from datetime import timedelta
from django.utils import timezone

def add_months(value, months):
    local = timezone.localtime(value)
    month_index=local.year*12+local.month-1+months
    year,month=divmod(month_index,12); month+=1
    return local.replace(year=year,month=month,day=min(local.day,calendar.monthrange(year,month)[1]))

def evaluate_lifecycle(profile, now):
    if profile.cancelled_at:
        return 'delete_account' if now>=profile.cancelled_at+timedelta(days=3) else 'cancellation_recovery_window'
    if profile.paid:
        return 'archive_data_keep_subscription' if now>=add_months(profile.last_activity_at,6) else 'retain'
    if now<add_months(profile.last_activity_at,1): return 'retain'
    if not profile.notice_at: return 'send_download_notice'
    if now>=profile.notice_at+timedelta(days=3): return 'delete_account'
    return 'download_window'

def record_notice_dispatch(profile,now,success):
    if success and not profile.notice_at:
        profile.notice_at=now
        profile.save(update_fields=['notice_at'])
