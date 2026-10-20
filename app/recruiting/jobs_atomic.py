import secrets
import time
from datetime import timedelta
from django.db import transaction,OperationalError
from django.db.models import F, Sum
from django.utils import timezone
from .models import Job,HiringSession,CloudBatch
from .coordination import lock_profile,locked_job

def enqueue(candidate,rubric_version):
    for retry in range(5):
        try:
            with transaction.atomic():
                owner_id=candidate.session.owner_id
                profile=lock_profile(owner_id)
                session=HiringSession.objects.select_for_update().get(pk=candidate.session_id)
                if not session.confirmed_at or rubric_version!=session.rubric_version or candidate.rubric_version!=rubric_version:
                    raise ValueError('Confirm the current rubric before processing; upload again for a changed rubric.')
                job=Job.objects.select_for_update().filter(candidate=candidate).first()
                if job and job.status in ('queued','processing','completed'): return job
                if profile.cancelled_at or profile.status!='active': raise ValueError('This account is not accepting new analyses.')
                if not profile.verified_at or timezone.now()>=profile.verified_at+timedelta(hours=72):
                    raise ValueError('Your three-day trial has ended. Existing results remain available.')
                reserved=Job.objects.filter(candidate__session__owner_id=owner_id,reserved=True).count()
                cloud_reserved=CloudBatch.objects.filter(owner_id=owner_id).aggregate(total=Sum('reserved_credits'))['total'] or 0
                if profile.completed_credits+reserved+cloud_reserved>=30:
                    raise ValueError('All 30 trial briefs are used or reserved by queued jobs.')
                if reserved>=30: raise ValueError('This account already has 30 queued or processing resumes. Wait for results before adding more.')
                if job:
                    job.status,job.error,job.reserved='queued','',True
                    job.lease_token,job.lease_until='',None
                    job.save()
                    return job
                return Job.objects.create(candidate=candidate,rubric=session.rubric,jd=session.jd)
        except OperationalError as exc:
            if 'locked' not in str(exc).lower() or retry==4: raise
            time.sleep(.05*(retry+1))

def recover_stale():
    with transaction.atomic():
        Job.objects.filter(status='processing',lease_until__lt=timezone.now()).update(status='failed',reserved=False,lease_token='',lease_until=None,error='Worker interrupted. Retry this resume to resume processing.')
        Job.objects.filter(interview_lease_until__lt=timezone.now()).update(interview_requested=False,interview_lease_token='',interview_lease_until=None,interview_error='Interview generation interrupted. Request the pack again.')

def claim_job(worker_id):
    recover_stale()
    for _ in range(32):
        key=Job.objects.filter(status='queued',reserved=True).order_by('created_at').values_list('pk',flat=True).first()
        if key is None: return None
        with locked_job(key) as (profile,job):
            if not job or job.status!='queued' or not job.reserved: continue
            if profile.cancelled_at or profile.status!='active':
                job.status,job.reserved='cancelled',False; job.save(); continue
            job.status='processing'; job.attempts+=1
            job.lease_token=secrets.token_hex(24); job.lease_until=timezone.now()+timedelta(minutes=10)
            job.save()
            return job
    return None

def complete_job(job_id,result,lease_token):
    with locked_job(job_id) as (profile,job):
        if not job or job.status!='processing' or not job.reserved or job.lease_token!=lease_token or not job.lease_until or job.lease_until<=timezone.now(): return
        if profile.cancelled_at or profile.status!='active':
            job.status,job.reserved,job.error='cancelled',False,'Account is no longer active.'
        else:
            type(profile).objects.filter(pk=profile.pk).update(completed_credits=F('completed_credits')+1)
            job.result,job.status,job.reserved=result,'completed',False
            job.completed_at=timezone.now()
        job.lease_until,job.lease_token=None,''
        job.save()

def fail_job(job_id,reason,lease_token):
    with locked_job(job_id) as (profile,job):
        if job and job.status=='processing' and job.lease_token==lease_token:
            job.status,job.reserved='failed',False
            job.error,job.lease_token,job.lease_until=reason[:400],'',None
            job.save()

def cancel_job(job_id,owner):
    with locked_job(job_id,owner.pk) as (profile,job):
        if job and job.status in ('queued','processing'):
            job.status,job.reserved='cancelled',False
            job.lease_token,job.lease_until='',None
            job.save()

def claim_interview():
    recover_stale()
    for _ in range(32):
        key=Job.objects.filter(status='completed',interview_requested=True,interview_lease_until__isnull=True).order_by('pk').values_list('pk',flat=True).first()
        if key is None: return None
        with locked_job(key) as (profile,job):
            if not job or job.status!='completed' or not job.interview_requested or job.interview_lease_until: continue
            if profile.cancelled_at or profile.status!='active':
                job.interview_requested=False; job.save(update_fields=['interview_requested']); continue
            job.interview_requested=False; job.interview_lease_token=secrets.token_hex(24)
            job.interview_lease_until=timezone.now()+timedelta(minutes=5); job.interview_error='Generating interview pack…'
            job.save()
            return job
    return None

def complete_interview(job_id,result,token,error=''):
    with locked_job(job_id) as (profile,job):
        if not job or job.status!='completed' or job.interview_lease_token!=token or not job.interview_lease_until or job.interview_lease_until<=timezone.now() or profile.cancelled_at or profile.status!='active': return
        if result: job.interviews=result
        job.interview_error=error[:400]; job.interview_lease_token=''; job.interview_lease_until=None
        job.save()
