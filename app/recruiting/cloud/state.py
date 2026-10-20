"""Transactional metadata transitions, with account locks before batch locks.

All callers use these functions rather than updating workflow rows. Worker
authentication and opaque byte transfer are separate adapters, never parsers.
"""
import secrets
import time
from contextlib import contextmanager
from datetime import timedelta
from functools import wraps
from django.db import OperationalError, transaction
from django.db.models import F, Sum
from django.utils import timezone
from recruiting.assessment import validate_criteria
from recruiting.entitlements import require_analysis_access
from recruiting.models import DeletionTombstone, HiringSession, Job, Profile
from .models import CloudBatch, CloudOutbox, CloudResultReference, StageAttempt, UploadReservation
from .contracts import (MAX_BATCH_BYTES, MAX_CANDIDATES, MAX_FILE_BYTES, MAX_JD_CHARACTERS,
                        TERMINAL_STAGES, WORKER_STAGES, checked_input, check_file,
                        digest_manifest, private_result_key)

_INTERNAL_WORKER = object()


def _retry_locked(function):
    """SQLite shared-cache tests return SQLITE_LOCKED without honoring timeout."""
    @wraps(function)
    def invoke(*args, **kwargs):
        for retry in range(5):
            try:
                return function(*args, **kwargs)
            except OperationalError as exc:
                if 'locked' not in str(exc).lower() or retry == 4:
                    raise
                time.sleep(.05*(retry+1))
    return invoke


def _active(profile):
    return bool(profile and not profile.cancelled_at and profile.status == 'active'
                and not DeletionTombstone.objects.filter(account_key=profile.account_key).exists())


@contextmanager
def _locked_batch(batch_id, owner_id=_INTERNAL_WORKER):
    identity = CloudBatch.objects.filter(pk=batch_id).values('owner_id', 'session_id').first()
    if identity is None or (owner_id is not _INTERNAL_WORKER and identity['owner_id'] != owner_id):
        yield None, None
        return
    with transaction.atomic():
        actual_owner = identity['owner_id']
        profile = Profile.objects.select_for_update().filter(user_id=actual_owner).first()
        # All cloud flows use profile -> session -> batch -> attempt order.
        session = HiringSession.objects.select_for_update().filter(pk=identity['session_id'], owner_id=actual_owner).first() if profile else None
        batch = CloudBatch.objects.select_for_update().filter(pk=batch_id, owner_id=actual_owner, session_id=session.pk).first() if session else None
        yield profile, batch


def _require_owned(profile, batch):
    if not profile or not batch:
        raise ValueError('Batch unavailable.')
    require_analysis_access(profile)
    if not _active(profile) or profile.account_key != batch.account_key or profile.account_epoch != batch.account_epoch:
        raise ValueError('Account is no longer accepting this batch.')
    if batch.expires_at <= timezone.now() or batch.stage in TERMINAL_STAGES:
        raise ValueError('Batch no longer accepts uploads.')


def _finish(batch, stage, error_code=''):
    batch.stage, batch.error_code = stage, error_code
    batch.reserved_credits = 0
    batch.finished_at = timezone.now()
    batch.save()
    batch.attempts.filter(state='running').update(state=stage, finished_at=batch.finished_at)
    batch.outbox.filter(kind='dispatch').exclude(state='delivered').update(state='cancelled', lease_token='', lease_until=None)


def _dispatch(batch):
    CloudOutbox.objects.get_or_create(batch=batch, event_key=f'dispatch:{batch.stage}',
                                     defaults={'kind': 'dispatch', 'stage': batch.stage})


def create_batch(owner_id, session_id, input_mode):
    """Create a frozen role/rubric snapshot; inputs reserve quota separately."""
    if input_mode not in ('files', 'zip'):
        raise ValueError('Invalid input mode.')
    with transaction.atomic():
        profile = Profile.objects.select_for_update().filter(user_id=owner_id).first()
        if not _active(profile):
            raise ValueError('Account unavailable.')
        require_analysis_access(profile)
        session = HiringSession.objects.select_for_update().filter(pk=session_id, owner_id=owner_id).first()
        if not session:
            raise ValueError('Hiring session unavailable.')
        if len(session.jd) > MAX_JD_CHARACTERS:
            raise ValueError('Job description exceeds 30,000 characters.')
        reusable_rubric = session.rubric if session.confirmed_at and session.rubric_status == 'ready' else []
        if reusable_rubric:
            validate_criteria(reusable_rubric)
        return CloudBatch.objects.create(owner_id=owner_id, session=session, input_mode=input_mode,
            account_key=profile.account_key, account_epoch=profile.account_epoch,
            rubric_version=session.rubric_version, rubric_snapshot=reusable_rubric,
            jd_text=session.jd, expires_at=timezone.now()+timedelta(hours=24))


def _available_credits(profile, batch):
    legacy = Job.objects.filter(candidate__session__owner_id=profile.user_id, reserved=True).count()
    others = CloudBatch.objects.filter(owner_id=profile.user_id).exclude(pk=batch.pk).aggregate(total=Sum('reserved_credits'))['total'] or 0
    return max(0, MAX_CANDIDATES-profile.completed_credits-legacy-others)


@_retry_locked
def reserve_inputs(batch_id, owner_id, files):
    """Reserve immutable metadata. ZIP reserves ALL remaining candidate capacity.

    files: list[InputSpec]. client_key makes retries idempotent within this batch.
    Storage keys are generated here; filenames never become storage paths.
    """
    if not isinstance(files, (list, tuple)) or not 1 <= len(files) <= MAX_CANDIDATES+1:
        raise ValueError('Provide a bounded list of inputs.')
    specs = [checked_input(item) for item in files]
    if len({s['client_key'] for s in specs}) != len(specs):
        raise ValueError('Repeated input identity.')
    with _locked_batch(batch_id, owner_id) as (profile, batch):
        _require_owned(profile, batch)
        if batch.stage != 'uploading':
            raise ValueError('Inputs are sealed.')
        existing = {item.client_key: item for item in batch.inputs.all()}
        new = []
        for spec in specs:
            old = existing.get(spec['client_key'])
            if old:
                if any(getattr(old, name) != value for name, value in spec.items()):
                    raise ValueError('Input identity cannot be reused for different bytes.')
            else:
                new.append(spec)
        combined = list(existing.values()) + new
        def field(item, key):
            return item[key] if isinstance(item, dict) else getattr(item, key)
        count = lambda role: sum(field(item, 'role') == role for item in combined)
        if count('resume') > MAX_CANDIDATES or count('jd') > 1 or count('archive') > 1 or (count('archive') and batch.input_mode != 'zip'):
            raise ValueError('Input count exceeds batch limits.')
        if sum(field(item, 'byte_size') for item in combined) > MAX_BATCH_BYTES:
            raise ValueError('Uploaded batch exceeds 300 MB.')
        digests = [field(item, 'sha256') for item in combined if field(item, 'role') == 'resume']
        if len(digests) != len(set(digests)):
            raise ValueError('Duplicate resume content.')
        available = _available_credits(profile, batch)
        needed = available if count('archive') else count('resume')
        if needed > available or (count('archive') and (not needed or count('resume') > needed)):
            raise ValueError('All 30 trial briefs are used or reserved.')
        batch.reserved_credits = needed
        batch.save(update_fields=['reserved_credits'])
        for spec in new:
            row = UploadReservation(batch=batch, **spec)
            row.storage_key = f'cloud/{batch.account_key}/{batch.pk}/inputs/{row.pk}'
            row.save()
            existing[row.client_key] = row
        return [existing[spec['client_key']] for spec in specs]


def confirm_upload(batch_id, owner_id, reservation_id, byte_size, sha256):
    """Trusted storage adapter calls after verifying actual bytes against reservation.

    This is not an HTTP endpoint and must never trust a client's assertion that
    an object exists. It performs no parsing, scanning or network operations.
    """
    with _locked_batch(batch_id, owner_id) as (profile, batch):
        _require_owned(profile, batch)
        if batch.stage != 'uploading':
            raise ValueError('Inputs are sealed.')
        item = batch.inputs.filter(pk=reservation_id).first()
        if not item or type(byte_size) is not int or item.byte_size != byte_size or item.sha256 != sha256:
            raise ValueError('Uploaded bytes differ from reservation.')
        if not item.uploaded_at:
            item.uploaded_at = timezone.now()
            item.save(update_fields=['uploaded_at'])
        return item


def seal_batch(batch_id, owner_id):
    with _locked_batch(batch_id, owner_id) as (profile, batch):
        if batch and batch.stage in WORKER_STAGES and _active(profile) and batch.account_epoch == profile.account_epoch:
            return batch
        _require_owned(profile, batch)
        session = HiringSession.objects.select_for_update().filter(pk=batch.session_id, owner_id=owner_id).first()
        if not session or session.jd != batch.jd_text or session.rubric_version != batch.rubric_version or (batch.rubric_snapshot and (not session.confirmed_at or session.rubric_status != 'ready' or session.rubric != batch.rubric_snapshot)):
            raise ValueError('Rubric changed; create a new batch.')
        items = list(batch.inputs.order_by('client_key'))
        if not items or any(not item.uploaded_at for item in items):
            raise ValueError('Every reserved upload must be complete.')
        if not any(item.role in ('resume', 'archive') for item in items) or (batch.input_mode == 'zip' and not any(item.role == 'archive' for item in items)):
            raise ValueError('Batch requires resume inputs.')
        if not batch.jd_text.strip() and not any(item.role == 'jd' for item in items):
            raise ValueError('Provide a job description.')
        manifest = [{'id': str(item.pk), 'role': item.role, 'filename': item.filename,
                     'byte_size': item.byte_size, 'sha256': item.sha256,
                     'storage_backend': item.storage_backend, 'storage_key': item.storage_key} for item in items]
        batch.input_manifest = manifest
        batch.input_manifest_digest = digest_manifest({'inputs': manifest, 'jd_text': batch.jd_text})
        # A rubric copied from the previous JD cannot describe a new upload.
        # Keep the version fence, but require fresh role planning after scanning.
        if any(item.role == 'jd' for item in items):
            batch.rubric_snapshot = []
        batch.sealed_at, batch.stage = timezone.now(), 'validate'
        batch.save()
        _dispatch(batch)
        return batch


def _invalidate(profile, batch):
    if not _active(profile) or batch.account_key != profile.account_key or batch.account_epoch != profile.account_epoch:
        _finish(batch, 'cancelled', 'account_inactive_or_epoch_changed')
        return True
    if batch.expires_at <= timezone.now():
        _finish(batch, 'expired', 'batch_expired')
        return True
    return False


@_retry_locked
def claim_stage(batch_id, lease_seconds=600):
    """One authenticated dispatcher claims a stage; no external work occurs here."""
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 900:
        raise ValueError('Invalid lease duration.')
    with _locked_batch(batch_id) as (profile, batch):
        if not batch or batch.stage not in WORKER_STAGES or _invalidate(profile, batch):
            return None
        active = batch.attempts.filter(stage=batch.stage, state='running').first()
        if active:
            if active.lease_until <= timezone.now():
                _finish(batch, 'expired', 'attempt_expired')
            return None
        return StageAttempt.objects.create(batch=batch, stage=batch.stage,
            attempt_number=batch.attempts.filter(stage=batch.stage).count()+1,
            account_key=batch.account_key, account_epoch=batch.account_epoch,
            input_manifest_digest=batch.input_manifest_digest, rubric_version=batch.rubric_version,
            lease_token=secrets.token_hex(24), lease_until=timezone.now()+timedelta(seconds=lease_seconds))


def _validated_manifest(batch, payload):
    expanded = payload.get('expanded_inputs')
    if not isinstance(expanded, list) or not 1 <= len(expanded) <= batch.reserved_credits:
        raise ValueError('Expanded count exceeds reserved capacity.')
    sources = {item['id']: item for item in batch.input_manifest}
    fixture_attestation = any(isinstance(item, dict) and item.get('scan_status') == 'trusted_fixture' for item in expanded)
    if fixture_attestation:
        from .sample_contract import verified_sample_inputs
        verified_sample_inputs(batch)
        if payload.get('synthetic') is not True or payload.get('validation_method') != 'canonical_fixture':
            raise ValueError('Invalid fictional validation attestation.')
    normalized, digests, individual_sources = [], set(), set()
    for member in expanded:
        if not isinstance(member, dict):
            raise ValueError('Invalid expanded input.')
        check_file(member)
        if member.get('scan_status') != 'clean' and not (fixture_attestation and member.get('scan_status') == 'trusted_fixture'):
            raise ValueError('Every expanded resume requires a clean cloud scan.')
        source_id = member.get('source_id')
        source = sources.get(source_id)
        if not source or source['role'] not in ('resume', 'archive') or member['byte_size'] > MAX_FILE_BYTES:
            raise ValueError('Expanded input lacks valid source or exceeds size limit.')
        if member.get('encrypted') or member.get('nested_archive') or member['filename'].lower().endswith(('.zip', '.7z', '.rar', '.tar', '.gz')):
            raise ValueError('Nested or encrypted archives are not accepted.')
        if source['role'] == 'resume':
            if source_id in individual_sources or any(member[key] != source[key] for key in ('filename', 'byte_size', 'sha256')):
                raise ValueError('Individual resume changed during validation.')
            individual_sources.add(source_id)
        if member['sha256'] in digests:
            raise ValueError('Duplicate expanded resume.')
        digests.add(member['sha256'])
        normalized.append({key: member[key] for key in ('source_id', 'filename', 'byte_size', 'sha256')})
    if individual_sources != {key for key, item in sources.items() if item['role'] == 'resume'}:
        raise ValueError('Validation omitted an individual resume.')
    if sum(member['byte_size'] for member in normalized) > MAX_BATCH_BYTES:
        raise ValueError('Expanded batch exceeds 300 MB.')
    has_uploaded_jd = any(item['role'] == 'jd' for item in sources.values())
    if has_uploaded_jd and payload.get('jd_scan_status') != 'clean':
        raise ValueError('Uploaded job description requires a clean cloud scan.')
    jd_text = payload.get('jd_text') if has_uploaded_jd else payload.get('jd_text', batch.jd_text)
    if not has_uploaded_jd and jd_text != batch.jd_text:
        raise ValueError('Pasted job description cannot change after sealing.')
    if not isinstance(jd_text, str) or not jd_text.strip() or len(jd_text) > MAX_JD_CHARACTERS:
        raise ValueError('Invalid extracted job description.')
    return normalized, jd_text


def _result_manifest(batch, payload):
    results = payload.get('results')
    if not isinstance(results, list) or not 1 <= len(results) <= batch.reserved_credits:
        raise ValueError('Invalid successful brief count.')
    allowed = {item['sha256'] for item in batch.expanded_manifest}
    seen, normalized = set(), []
    for result in results:
        if not isinstance(result, dict):
            raise ValueError('Invalid result reference.')
        digest = result.get('input_sha256')
        if digest not in allowed or digest in seen:
            raise ValueError('Result does not match validated input.')
        seen.add(digest)
        normalized.append({'input_sha256': digest, 'storage_key': private_result_key(batch, result.get('storage_key'))})
    return normalized


def _role_manifest(batch, payload):
    criteria = validate_criteria(payload.get('criteria'))
    normalized = []
    for item in criteria:
        quote = item.get('source_quote')
        if not isinstance(quote, str) or not quote.strip() or len(quote) > 1200 or quote not in batch.jd_text:
            raise ValueError('Role criterion requires exact submitted JD evidence.')
        normalized.append({key: item[key] for key in ('id', 'name', 'weight', 'source_quote')})
    return normalized


@_retry_locked
def apply_transition(batch_id, attempt_id, expected_stage, result_manifest):
    """Accept a single leased completion, fenced to tenant/epoch/inputs/rubric.

    Common manifest: account_key, account_epoch, input_manifest_digest,
    rubric_version, lease_token. validate adds expanded_inputs and optional
    jd_text; role adds criteria with JD source_quote evidence; analyze adds
    successful results[{input_sha256, storage_key}]. A
    failed=True completion terminates without charging. Partial success charges
    only provided unique successful briefs and releases all remaining credits.
    """
    with _locked_batch(batch_id) as (profile, batch):
        if not batch or expected_stage not in WORKER_STAGES or batch.stage != expected_stage:
            return False
        if _invalidate(profile, batch):
            return False
        attempt = batch.attempts.select_for_update().filter(pk=attempt_id, stage=expected_stage, state='running').first()
        if not attempt:
            return False
        if attempt.lease_until <= timezone.now():
            _finish(batch, 'expired', 'attempt_expired')
            return False
        if not isinstance(result_manifest, dict):
            return False
        expected = {'account_key': str(batch.account_key), 'account_epoch': batch.account_epoch,
                    'input_manifest_digest': batch.input_manifest_digest, 'rubric_version': batch.rubric_version,
                    'lease_token': attempt.lease_token}
        if any(result_manifest.get(key) != value for key, value in expected.items()):
            return False
        if (attempt.account_key != batch.account_key or attempt.account_epoch != batch.account_epoch
                or attempt.input_manifest_digest != batch.input_manifest_digest or attempt.rubric_version != batch.rubric_version):
            return False
        if result_manifest.get('failed') is True:
            _finish(batch, 'failed', 'worker_stage_failed')
            return True
        try:
            if expected_stage == 'validate':
                expanded, jd_text = _validated_manifest(batch, result_manifest)
            elif expected_stage == 'role':
                criteria = _role_manifest(batch, result_manifest)
                session = HiringSession.objects.get(pk=batch.session_id)
                original_input_digest = digest_manifest({'inputs': batch.input_manifest, 'jd_text': session.jd})
                if session.rubric_version != batch.rubric_version or original_input_digest != batch.input_manifest_digest:
                    raise ValueError('Job description or role version changed while generating criteria.')
            else:
                results = _result_manifest(batch, result_manifest)
        except (ValueError, TypeError, KeyError):
            _finish(batch, 'failed', 'invalid_worker_manifest')
            return False
        attempt.state, attempt.finished_at = 'succeeded', timezone.now()
        attempt.save(update_fields=['state', 'finished_at'])
        if expected_stage == 'validate':
            batch.expanded_manifest = expanded
            batch.jd_text = jd_text
            batch.reserved_credits = len(expanded)
            batch.stage = 'analyze' if batch.rubric_snapshot else 'role'
            batch.save()
            _dispatch(batch)
        elif expected_stage == 'role':
            session.jd = batch.jd_text
            session.rubric = criteria
            session.rubric_version += 1
            session.rubric_status, session.rubric_error = 'ready', ''
            session.confirmed_at = timezone.now()
            session.rubric_lease, session.rubric_lease_until = '', None
            session.save(update_fields=['jd', 'rubric', 'rubric_version', 'rubric_status', 'rubric_error', 'confirmed_at', 'rubric_lease', 'rubric_lease_until'])
            batch.rubric_version, batch.rubric_snapshot = session.rubric_version, criteria
            batch.stage = 'analyze'
            batch.save()
            _dispatch(batch)
        else:
            for result in results:
                CloudResultReference.objects.create(batch=batch, **result)
            Profile.objects.filter(pk=profile.pk).update(completed_credits=F('completed_credits')+len(results))
            batch.settled_credits = len(results)
            _finish(batch, 'completed')
            CloudOutbox.objects.get_or_create(batch=batch, event_key='notification:completed',
                defaults={'kind': 'notification', 'stage': 'completed'})
        return True


def expire_batches():
    """Release stale reservations and inactive account batches, with account first locks."""
    count = 0
    for batch_id in CloudBatch.objects.exclude(stage__in=TERMINAL_STAGES).values_list('pk', flat=True).iterator():
        with _locked_batch(batch_id) as (profile, batch):
            if not batch or batch.stage in TERMINAL_STAGES:
                continue
            if _invalidate(profile, batch):
                count += 1
            elif batch.attempts.filter(state='running', lease_until__lte=timezone.now()).exists():
                _finish(batch, 'expired', 'attempt_expired')
                count += 1
    return count
