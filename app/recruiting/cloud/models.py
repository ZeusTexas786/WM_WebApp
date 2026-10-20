"""Private workflow metadata. Payload bytes live under PRIVATE_ROOT, never public URLs."""
import uuid
from django.conf import settings
from django.db import models
from django.utils import timezone


class CloudBatch(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    session = models.ForeignKey('recruiting.HiringSession', on_delete=models.CASCADE)
    account_key = models.UUIDField()
    account_epoch = models.PositiveBigIntegerField()
    input_mode = models.CharField(max_length=10)
    stage = models.CharField(max_length=20, default='uploading', db_index=True)
    rubric_version = models.PositiveIntegerField()
    rubric_snapshot = models.JSONField(default=list)
    jd_text = models.TextField(default='')
    input_manifest = models.JSONField(default=list)
    input_manifest_digest = models.CharField(max_length=64, default='')
    expanded_manifest = models.JSONField(default=list)
    reserved_credits = models.PositiveSmallIntegerField(default=0)
    settled_credits = models.PositiveSmallIntegerField(default=0)
    error_code = models.CharField(max_length=80, default='')
    created_at = models.DateTimeField(default=timezone.now)
    sealed_at = models.DateTimeField(null=True)
    expires_at = models.DateTimeField(db_index=True)
    finished_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(reserved_credits__lte=30), name='cloud_batch_reservations_lte_30')]


class UploadReservation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(CloudBatch, on_delete=models.CASCADE, related_name='inputs')
    client_key = models.CharField(max_length=80)
    role = models.CharField(max_length=10)
    filename = models.CharField(max_length=200)
    byte_size = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    storage_backend = models.CharField(max_length=20, default='private_vps')
    storage_key = models.CharField(max_length=250, unique=True)
    uploaded_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['batch', 'client_key'], name='cloud_input_client_key_unique')]


class StageAttempt(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(CloudBatch, on_delete=models.CASCADE, related_name='attempts')
    stage = models.CharField(max_length=20)
    attempt_number = models.PositiveIntegerField()
    account_key = models.UUIDField()
    account_epoch = models.PositiveBigIntegerField()
    input_manifest_digest = models.CharField(max_length=64)
    rubric_version = models.PositiveIntegerField()
    lease_token = models.CharField(max_length=64)
    lease_until = models.DateTimeField()
    state = models.CharField(max_length=20, default='running')
    created_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['batch', 'stage', 'attempt_number'], name='cloud_stage_attempt_number_unique')]


class CloudOutbox(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(CloudBatch, on_delete=models.CASCADE, related_name='outbox')
    kind = models.CharField(max_length=20)
    stage = models.CharField(max_length=20)
    event_key = models.CharField(max_length=80)
    state = models.CharField(max_length=20, default='pending', db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    lease_token = models.CharField(max_length=64, default='')
    lease_until = models.DateTimeField(null=True)
    created_at = models.DateTimeField(default=timezone.now)
    delivered_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['batch', 'event_key'], name='cloud_outbox_event_unique')]


class CloudResultReference(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(CloudBatch, on_delete=models.CASCADE, related_name='result_references')
    input_sha256 = models.CharField(max_length=64)
    storage_backend = models.CharField(max_length=20, default='private_vps')
    storage_key = models.CharField(max_length=250)
    candidate = models.ForeignKey('recruiting.Candidate', on_delete=models.SET_NULL, null=True, related_name='cloud_results')
    job = models.ForeignKey('recruiting.Job', on_delete=models.SET_NULL, null=True, related_name='cloud_results')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['batch', 'input_sha256'], name='cloud_result_input_unique')]
