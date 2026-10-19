import uuid
from django.conf import settings
from django.db import models
from django.utils import timezone

class Profile(models.Model):
    account_key=models.UUIDField(default=uuid.uuid4,editable=False,unique=True)
    archive_name=models.CharField(max_length=120,default='')
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    verified_at = models.DateTimeField(null=True, blank=True)
    last_activity_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=30, default='active')
    paid = models.BooleanField(default=False)
    notice_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    completed_credits = models.PositiveIntegerField(default=0)

class LifecycleAction(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    account=models.ForeignKey(Profile,null=True,on_delete=models.SET_NULL)
    cycle=models.CharField(max_length=64,unique=True)
    kind=models.CharField(max_length=40)
    state=models.CharField(max_length=20,default='pending')
    attempts=models.PositiveIntegerField(default=0)
    error=models.CharField(max_length=100,default='')
    lease_until=models.DateTimeField(null=True)
    lease_token=models.CharField(max_length=64,default='')
    created_at=models.DateTimeField(default=timezone.now)
    finished_at=models.DateTimeField(null=True)

class DeletionTombstone(models.Model):
    account_key=models.UUIDField(unique=True)
    deleted_at=models.DateTimeField(default=timezone.now)

class Verification(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    digest = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(default=timezone.now)
    used_at = models.DateTimeField(null=True)

class Throttle(models.Model):
    key = models.CharField(max_length=64, unique=True)
    count = models.PositiveIntegerField(default=0)
    window_at = models.DateTimeField(default=timezone.now)

class ApiUsage(models.Model):
    day = models.DateField(unique=True)
    requests = models.PositiveIntegerField(default=0)

class ServiceHeartbeat(models.Model):
    service=models.CharField(max_length=30,unique=True)
    seen_at=models.DateTimeField(default=timezone.now)

class HiringSession(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    title = models.CharField(max_length=150)
    jd = models.TextField()
    mode = models.CharField(max_length=10, default='live')
    rubric = models.JSONField(default=list)
    rubric_version = models.PositiveIntegerField(default=0)
    rubric_status=models.CharField(max_length=20,default='ready')
    rubric_error=models.CharField(max_length=400,default='')
    rubric_lease=models.CharField(max_length=64,default='')
    rubric_lease_until=models.DateTimeField(null=True)
    confirmed_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(default=timezone.now)

class Preference(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    text = models.CharField(max_length=1000)
    created_at = models.DateTimeField(default=timezone.now)

class Candidate(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(HiringSession, on_delete=models.CASCADE, related_name='candidates')
    filename = models.CharField(max_length=200)
    storage_name = models.CharField(max_length=250)
    digest = models.CharField(max_length=64)
    rubric_version = models.PositiveIntegerField()
    blocks = models.JSONField(default=list)
    byte_size = models.PositiveBigIntegerField(default=0)
    scan_status = models.CharField(max_length=20,default='unscanned')
    created_at = models.DateTimeField(default=timezone.now)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['session', 'digest', 'rubric_version'], name='unique_candidate_content_per_rubric')]

class Job(models.Model):
    candidate = models.OneToOneField(Candidate, on_delete=models.CASCADE, related_name='job')
    rubric = models.JSONField(default=list)
    jd = models.TextField()
    status = models.CharField(max_length=20, default='queued')
    reserved = models.BooleanField(default=True)
    attempts = models.PositiveIntegerField(default=0)
    lease_token = models.CharField(max_length=64, default='')
    lease_until = models.DateTimeField(null=True)
    result = models.JSONField(default=dict)
    interviews = models.JSONField(default=dict)
    error = models.CharField(max_length=400, default='')
    interview_error = models.CharField(max_length=400, default='')
    interview_requested = models.BooleanField(default=False)
    interview_lease_token = models.CharField(max_length=64,default='')
    interview_lease_until = models.DateTimeField(null=True)
    created_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True)
