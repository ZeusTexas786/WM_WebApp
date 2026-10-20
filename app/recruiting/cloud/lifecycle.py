"""Private-VPS lifecycle storage. No remote or provider adapter is enabled.

Call disk mutations under storage_admission, before acquiring Profile locks.
Physical footprint accounting includes named scratch even after failed cleanup.
"""
import os
import shutil
import stat
import tempfile
from pathlib import Path
from django.conf import settings
from .storage import assert_disk_headroom

MAX_FILES = 10000
MAX_SNAPSHOT_BYTES = 512*1024*1024


def safe_path(relative):
    if not isinstance(relative, str) or not relative or '\\' in relative or ':' in relative or relative.startswith('/') or any(p in ('', '.', '..') for p in relative.split('/')):
        raise ValueError('Unsafe private path.')
    root = Path(settings.PRIVATE_ROOT).absolute()
    for parent in [root, *root.parents]:
        if parent.is_symlink(): raise ValueError('Unsafe private root.')
    target = root
    for component in relative.split('/'):
        target = target/component
        if target.is_symlink(): raise ValueError('Unsafe private path.')
    if target.exists():
        info = target.stat()
        if not stat.S_ISDIR(info.st_mode) and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise ValueError('Unsafe private storage entry.')
    return target


def account_roots(profile):
    return (safe_path(str(profile.user_id)), safe_path(f'cloud/{profile.account_key}'))


def owned_path(profile, relative):
    path = safe_path(relative)
    if not any(path.is_relative_to(root) and path != root for root in account_roots(profile)):
        raise ValueError('Private storage is outside account.')
    return path


def _account_entries(profile):
    """Validate/yield one file at a time without building a snapshot manifest."""
    root = Path(settings.PRIVATE_ROOT).absolute()
    def walk(folder):
        with os.scandir(folder) as entries:
            for entry in entries:
                path=safe_path(Path(entry.path).relative_to(root).as_posix())
                if path.is_dir(): yield from walk(path)
                else: yield path.relative_to(root).as_posix(),path
    for folder in account_roots(profile):
        if not folder.exists(): continue
        yield from walk(folder)


def account_files(profile):
    result = {}; total = 0
    for key,path in _account_entries(profile):
        total += path.stat().st_size
        if total > MAX_SNAPSHOT_BYTES or len(result) >= MAX_FILES:
            raise ValueError('Account snapshot exceeds bounds.')
        result[key] = path
    return result


def remove_account_trees(profile):
    # Validate the entire tree first; never follow symlinks or remove another owner.
    # Erasure must work even for orphan trees too large to archive. Validate
    # every entry with bounded traversal memory, retaining all path/link checks.
    for _ in _account_entries(profile): pass
    for folder in account_roots(profile):
        if folder.exists(): shutil.rmtree(folder)


def require_exports_closed(profile):
    """Keep open export descriptors named/countable until response close.

    Caller holds admission and has already fenced the account into archiving or
    deleting. Thus no new export can start after this check. A failed cleanup
    remains visible and blocks maintenance rather than becoming anonymous bytes.
    """
    scratch=safe_path(f'.staging/{profile.account_key}')
    if scratch.exists() and any(scratch.glob('export-*')):
        raise ValueError('Account export cleanup is still pending.')


class BoundedPrivateFile:
    """Named disk scratch, with growth admitted before each bounded write."""
    def __init__(self, folder, name='workspace-', limit=MAX_SNAPSHOT_BYTES, cleanup=True):
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, self.name = tempfile.mkstemp(prefix=name, dir=folder)
        self.file = os.fdopen(descriptor, 'w+b')
        self.limit, self.cleanup = limit, cleanup

    def write(self, data):
        end = self.file.tell()+len(data)
        old = os.fstat(self.file.fileno()).st_size
        if end > self.limit: raise ValueError('Private output exceeds bound.')
        assert_disk_headroom(max(0, end-old))
        value = self.file.write(data)
        self.file.flush()
        return value

    def __getattr__(self, name): return getattr(self.file, name)

    def close(self):
        self.file.close()
        if self.cleanup: Path(self.name).unlink(missing_ok=True)

    def __enter__(self): return self

    def __exit__(self, *args): self.close()
