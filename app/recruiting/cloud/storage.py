"""Bounded private quarantine. No document parsing, scanning or outbound calls.

Admission order: global transaction lock -> Profile -> HiringSession -> CloudBatch.
PostgreSQL advisory locking serializes all accounts; SQLite uses IMMEDIATE
transactions. Every operation reloads owner/batch metadata under state locks.
The private root must be a dedicated disk directory with operator-only ACLs;
Linux deployments must not place it on tmpfs or under a served document root.
"""
import hashlib
import os
import shutil
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone
from recruiting.models import Candidate
from . import state
from .models import UploadReservation

CHUNK_BYTES = 1024 * 1024
READ_BYTES = 65536


@dataclass(frozen=True)
class UploadGrant:
    reservation_id: str
    offset: int
    expires_at: object
    chunk_bytes: int = CHUNK_BYTES


@dataclass(frozen=True)
class ImmutableObject:
    reservation_id: str
    byte_size: int
    sha256: str


@contextmanager
def storage_admission():
    with transaction.atomic():
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_xact_lock(%s)', [882913174])
        yield


def admission_view(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        with storage_admission():
            return view(*args, **kwargs)
    return wrapped


def _private_footprint():
    root = Path(settings.PRIVATE_ROOT).absolute()
    for ancestor in [root, *root.parents]:
        if ancestor.is_symlink(): raise ValueError('Unsafe private storage path.')
    if not root.exists(): return 0
    total = 0
    for directory, folders, files in os.walk(root, followlinks=False):
        for name in folders + files:
            path = Path(directory)/name
            if path.is_symlink(): raise ValueError('Unsafe private storage path.')
            info = path.stat()
            if stat.S_ISREG(info.st_mode): total += info.st_size
            elif not stat.S_ISDIR(info.st_mode): raise ValueError('Unsafe private storage entry.')
    return total


def _declared_inputs(owner_id=None):
    """One physical source may also have a legacy report projection."""
    declarations = {}
    cloud = UploadReservation.objects.select_related('batch')
    legacy = Candidate.objects.all()
    if owner_id is not None:
        cloud = cloud.filter(batch__owner_id=owner_id)
        legacy = legacy.filter(session__owner_id=owner_id)
    for item in cloud.iterator():
        expected = f'cloud/{item.batch.account_key}/{item.batch_id}/inputs/{item.pk}'
        if item.storage_key != expected or item.storage_backend != 'private_vps':
            raise ValueError('Invalid private object identity.')
        declarations[expected] = max(item.byte_size, declarations.get(expected,0))
    for item in legacy.iterator():
        name = item.storage_name
        if not name or '\\' in name or name.startswith('/') or ':' in name or any(part in ('','.','..') for part in name.split('/')):
            raise ValueError('Unsafe retained input path.')
        declarations[name] = max(item.byte_size,declarations.get(name,0))
    return declarations


def _unmaterialized_declarations():
    root = Path(settings.PRIVATE_ROOT).absolute()
    pending = 0
    for name,size in _declared_inputs().items():
        final = root/name
        part = final.with_name(final.name+'.part')
        materialized = max((path.stat().st_size if path.exists() else 0) for path in (final,part))
        pending += max(0,size-materialized)
    return pending


def assert_disk_headroom(extra_bytes=0):
    """Call INSIDE storage_admission before any new private artifact growth.

    Footprint includes results, receipts, partials, orphan and temporary files.
    Outstanding input promises add only their unmaterialized bytes. Global lock
    must remain held through the bounded write; external disk users are outside
    this lock, so the free-space floor is rechecked before every chunk.
    """
    if not connection.in_atomic_block:
        raise RuntimeError('Private writes require a storage admission transaction.')
    if type(extra_bytes) is not int or extra_bytes < 0:
        raise ValueError('Invalid private storage growth.')
    used = _private_footprint()
    pending = _unmaterialized_declarations()
    if used + pending + extra_bytes > settings.AUTOMATIC_PROJECT_STORAGE_BYTES:
        raise ValueError('Private project storage limit reached.')
    root = Path(settings.PRIVATE_ROOT)
    while not root.exists(): root = root.parent
    if shutil.disk_usage(root).free < settings.AUTOMATIC_MIN_FREE_BYTES + pending + extra_bytes:
        raise ValueError('Private storage free-space floor reached.')


def check_storage_capacity(owner_id, additional):
    """Caller holds global + profile locks; declarations release after removal."""
    if sum(_declared_inputs(owner_id).values()) + additional > settings.MAX_ACCOUNT_STORAGE_BYTES:
        raise ValueError('Account storage limit reached.')
    assert_disk_headroom(additional)


@state._retry_locked
def reserve_uploads(batch_id, owner_id, specs):
    with storage_admission():
        with state._locked_batch(batch_id, owner_id) as (profile, batch):
            state._require_owned(profile, batch)
            if not isinstance(specs, list) or not 1 <= len(specs) <= 31:
                raise ValueError('Provide a bounded list of inputs.')
            checked = [state.checked_input(item) for item in specs]
            existing = set(batch.inputs.values_list('client_key', flat=True))
            additional = sum(item['byte_size'] for item in checked if item['client_key'] not in existing)
            check_storage_capacity(owner_id, additional)
            return state.reserve_inputs(batch_id, owner_id, specs)


class PrivateDiskStorage:
    def __init__(self, owner_id):
        if type(owner_id) is not int:
            raise ValueError('Owner required.')
        self.owner_id = owner_id

    @contextmanager
    def _locked(self, reservation, sealed=False):
        with state._locked_batch(reservation.batch_id, self.owner_id) as (profile, batch):
            if sealed and batch and batch.sealed_at:
                from .contracts import TERMINAL_STAGES
                if not state._active(profile) or profile.account_epoch != batch.account_epoch or batch.stage in TERMINAL_STAGES or batch.expires_at <= timezone.now():
                    raise ValueError('Batch unavailable.')
            else:
                state._require_owned(profile, batch)
                if batch.stage != 'uploading':
                    raise ValueError('Inputs are sealed.')
            current = batch.inputs.filter(pk=reservation.pk).first()
            if not current:
                raise ValueError('Upload unavailable.')
            expected = f'cloud/{batch.account_key}/{batch.pk}/inputs/{current.pk}'
            if current.storage_key != expected or current.storage_backend != 'private_vps' or any(getattr(reservation, key) != getattr(current, key) for key in ('storage_key','storage_backend','sha256','byte_size')):
                raise ValueError('Invalid private object identity.')
            yield current

    def _path(self, item, part=False):
        root = Path(settings.PRIVATE_ROOT).absolute()
        # Check each component before mkdir/open. Private parent ACLs prevent an
        # untrusted local process racing directory replacement. O_NOFOLLOW also
        # fences final-component symlinks on platforms that support it.
        for ancestor in [root, *root.parents]:
            if ancestor.is_symlink():
                raise ValueError('Unsafe private storage path.')
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        current = root
        for name in item.storage_key.split('/')[:-1]:
            current = current/name
            if current.is_symlink():
                raise ValueError('Unsafe private storage path.')
            current.mkdir(mode=0o700, exist_ok=True)
        result = current/(str(item.pk)+('.part' if part else ''))
        if result.is_symlink():
            raise ValueError('Unsafe private storage path.')
        if not result.resolve().is_relative_to(root.resolve()):
            raise ValueError('Unsafe private storage path.')
        return result

    def _open(self, path, create=False):
        flags = os.O_RDWR | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        if create: flags |= os.O_CREAT
        fd = os.open(path, flags, 0o600)
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
            os.close(fd); raise ValueError('Private input is not a regular file.')
        return os.fdopen(fd, 'r+b')

    def _offset(self, item):
        final = self._path(item)
        part = self._path(item, True)
        target = final if final.exists() else part
        if not target.exists():
            if item.uploaded_at: raise ValueError('Verified bytes are missing.')
            return 0
        with self._open(target) as file:
            size = os.fstat(file.fileno()).st_size
        if size > item.byte_size:
            raise ValueError('Private input exceeds declared size.')
        return size

    def initiate_upload(self, reservation):
        with self._locked(reservation) as item:
            return UploadGrant(str(item.pk), self._offset(item), item.batch.expires_at)

    def committed_offset(self, reservation):
        with self._locked(reservation) as item:
            return self._offset(item)

    def write_chunk(self, reservation, offset, stream, content_length):
        if type(offset) is not int or offset < 0 or type(content_length) is not int or not 1 <= content_length <= CHUNK_BYTES:
            raise ValueError('Invalid chunk size or offset.')
        with storage_admission(), self._locked(reservation) as item:
            if item.uploaded_at or self._path(item).exists():
                raise ValueError('Input is finalized.')
            old = self._offset(item)
            if offset > old or offset + content_length > item.byte_size or (offset < old and offset + content_length > old):
                raise ValueError('Chunk does not match committed offset.')
            if offset == old: assert_disk_headroom()
            with self._open(self._path(item, True), create=True) as file:
                file.seek(offset)
                remaining = content_length
                try:
                    while remaining:
                        chunk = stream.read(min(READ_BYTES, remaining))
                        if not isinstance(chunk, bytes) or not chunk or len(chunk) > min(READ_BYTES, remaining):
                            raise ValueError('Chunk body differs from declared length.')
                        if offset < old:
                            if file.read(len(chunk)) != chunk:
                                raise ValueError('Previously committed chunk differs.')
                        else:
                            file.write(chunk)
                        remaining -= len(chunk)
                    if stream.read(1):
                        raise ValueError('Chunk body exceeds declared length.')
                    file.flush(); os.fsync(file.fileno())
                except Exception:
                    if offset == old:
                        file.truncate(old); file.flush(); os.fsync(file.fileno())
                    raise
            return max(old, offset+content_length)

    def _verify(self, item):
        final, part = self._path(item), self._path(item, True)
        if item.uploaded_at and not final.exists():
            raise ValueError('Verified bytes are missing.')
        target = final if final.exists() else part
        digest, size = hashlib.sha256(), 0
        try:
            with self._open(target) as file:
                while True:
                    chunk = file.read(READ_BYTES)
                    if not chunk: break
                    size += len(chunk)
                    if size > item.byte_size: raise ValueError('Private input exceeds declared size.')
                    digest.update(chunk)
        except FileNotFoundError as exc:
            raise ValueError('Upload is incomplete or missing.') from exc
        if size != item.byte_size or digest.hexdigest() != item.sha256:
            raise ValueError('Actual bytes do not match reservation.')
        return ImmutableObject(str(item.pk), size, digest.hexdigest())

    def finalize_upload(self, reservation):
        with self._locked(reservation, sealed=True) as item:
            result = self._verify(item)
            if item.uploaded_at: return result
            final = self._path(item)
            if not final.exists():
                os.replace(self._path(item, True), final)
            state.confirm_upload(item.batch_id, self.owner_id, item.pk, result.byte_size, result.sha256)
            return result

    def stat_input(self, reservation):
        with self._locked(reservation, sealed=True) as item:
            return self._verify(item)

    def read_input(self, reservation, max_bytes):
        """Bounded private executor read; never an HTTP download or parser."""
        if type(max_bytes) is not int or not 1 <= max_bytes <= CHUNK_BYTES:
            raise ValueError('Invalid bounded private read.')
        with self._locked(reservation, sealed=True) as item:
            if not item.uploaded_at or item.byte_size > max_bytes:
                raise ValueError('Private input exceeds executor bound.')
            with self._open(self._path(item)) as file:
                data = bytearray()
                while len(data) <= max_bytes:
                    chunk = file.read(min(READ_BYTES, max_bytes+1-len(data)))
                    if not chunk: break
                    data.extend(chunk)
            if len(data) != item.byte_size or hashlib.sha256(data).hexdigest() != item.sha256:
                raise ValueError('Actual bytes do not match reservation.')
            return bytes(data)

    def delete_input(self, reservation):
        with self._locked(reservation) as item:
            for part in (False, True):
                self._path(item, part).unlink(missing_ok=True)
            item.delete()
            # Recompute candidate reservation after removing one source.
            remaining = list(item.batch.inputs.values('role'))
            item.batch.reserved_credits = (item.batch.reserved_credits if any(row['role']=='archive' for row in remaining) else sum(row['role']=='resume' for row in remaining))
            item.batch.save(update_fields=['reserved_credits'])


def accept_batch(batch_id, owner_id):
    from .notifications import queue_acknowledgement
    with state._locked_batch(batch_id, owner_id) as (profile, batch):
        if not batch: raise ValueError('Batch unavailable.')
        disk = PrivateDiskStorage(owner_id)
        for item in batch.inputs.all():
            disk.stat_input(item)
        accepted = state.seal_batch(batch_id, owner_id)
        queue_acknowledgement(accepted)
        return accepted


def cleanup_abandoned_uploads(*, expire=True, limit=100):
    """Sweep never-sealed staging only, including finalized abandoned sources.

    Accepted inputs belong to the separately gated retention lifecycle. Staging
    bytes are deleted before declarations release their disk allowance.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Invalid staging cleanup bound.')
    if expire: state.expire_batches()
    removed = 0
    ids = list(UploadReservation.objects.filter(batch__sealed_at__isnull=True,
        batch__stage__in=['expired','cancelled','failed']).values_list('pk', flat=True)[:limit])
    for pk in ids:
        with storage_admission():
            identity = UploadReservation.objects.filter(pk=pk).select_related('batch').first()
            if not identity: continue
            with state._locked_batch(identity.batch_id, identity.batch.owner_id) as (_, batch):
                item = batch.inputs.filter(pk=pk).first() if batch else None
                if not item or batch.sealed_at is not None or batch.stage not in ('expired','cancelled','failed'): continue
                expected = f'cloud/{batch.account_key}/{batch.pk}/inputs/{item.pk}'
                if item.storage_key != expected or item.storage_backend != 'private_vps': continue
                disk = PrivateDiskStorage(batch.owner_id)
                for part in (False, True): disk._path(item, part).unlink(missing_ok=True)
                item.delete(); removed += 1
    return removed
