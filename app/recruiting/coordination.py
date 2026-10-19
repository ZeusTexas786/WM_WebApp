"""Database coordination: account locks precede job locks everywhere."""
from contextlib import contextmanager
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from .models import ApiUsage,Profile,Job

def lock_profile(user_id):
    return Profile.objects.select_for_update().get(user_id=user_id)

@contextmanager
def locked_job(job_id,owner_id=None):
    actual_owner=Job.objects.filter(pk=job_id).values_list('candidate__session__owner_id',flat=True).first()
    if actual_owner is None or (owner_id is not None and actual_owner!=owner_id):
        yield None,None
        return
    with transaction.atomic():
        profile=Profile.objects.select_for_update().filter(user_id=actual_owner).first()
        job=Job.objects.select_for_update().filter(pk=job_id,candidate__session__owner_id=actual_owner).first() if profile else None
        yield profile,job

def reserve_api_request(limit):
    with transaction.atomic():
        row,_=ApiUsage.objects.get_or_create(day=timezone.localdate())
        row=ApiUsage.objects.select_for_update().get(pk=row.pk)
        if row.requests>=limit: return False
        ApiUsage.objects.filter(pk=row.pk).update(requests=F('requests')+1)
        return True
