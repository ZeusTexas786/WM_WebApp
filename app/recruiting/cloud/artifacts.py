"""Private immutable attempt artifacts; server-assigned paths, no URL handles."""
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from django.conf import settings
from .storage import READ_BYTES, assert_disk_headroom

MAX_ARTIFACT_BYTES = 128*1024


def _sync_directory(path):
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try: os.fsync(fd)
        finally: os.close(fd)


def _durable_rename(source, destination):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        move = ctypes.WinDLL('kernel32', use_last_error=True).MoveFileExW
        move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move.restype = wintypes.BOOL
        # Write-through move, without REPLACE_EXISTING: published finals remain
        # immutable even if an unexpected existing file appears.
        if not move(str(source), str(destination), 0x8):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(source, destination)
        _sync_directory(destination.parent)


def encode(value):
    data = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(data) > MAX_ARTIFACT_BYTES:
        raise ValueError('Private artifact exceeds its bound.')
    return data


class AttemptArtifacts:
    def __init__(self, batch, attempt):
        if attempt.batch_id != batch.pk or attempt.account_key != batch.account_key:
            raise ValueError('Invalid private artifact owner.')
        self.prefix = f'cloud/{batch.account_key}/{batch.pk}/results/{attempt.pk}'

    def key(self, name):
        if name != 'receipt.json' and not re.fullmatch(r'brief-[0-2]\.json', name):
            raise ValueError('Invalid private artifact identity.')
        return f'{self.prefix}/{name}'

    def _path(self, name, create=False):
        key = self.key(name)
        root = Path(settings.PRIVATE_ROOT).absolute()
        for ancestor in [root, *root.parents]:
            if ancestor.is_symlink(): raise ValueError('Unsafe private artifact path.')
        if create and not root.is_dir(): raise ValueError('Private input root is unavailable.')
        current = root
        for component in key.split('/')[:-1]:
            current = current/component
            if current.is_symlink(): raise ValueError('Unsafe private artifact path.')
            if create and not current.exists():
                current.mkdir(mode=0o700)
                _sync_directory(current.parent)
        path = current/name
        if path.is_symlink(): raise ValueError('Unsafe private artifact path.')
        return path

    def _open(self, path, write=False):
        flags = (os.O_RDWR | os.O_CREAT if write else os.O_RDONLY)
        flags |= getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
        fd = os.open(path, flags, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            os.close(fd)
            raise ValueError('Private artifact is not a regular single-link file.')
        return os.fdopen(fd, 'r+b' if write else 'rb')

    def read_bytes(self, name):
        with self._open(self._path(name)) as file:
            if os.fstat(file.fileno()).st_size > MAX_ARTIFACT_BYTES:
                raise ValueError('Private artifact exceeds its bound.')
            data = bytearray()
            while len(data) <= MAX_ARTIFACT_BYTES:
                chunk = file.read(min(READ_BYTES, MAX_ARTIFACT_BYTES+1-len(data)))
                if not chunk: break
                data.extend(chunk)
        if len(data) > MAX_ARTIFACT_BYTES: raise ValueError('Private artifact exceeds its bound.')
        return bytes(data)

    def read(self, name):
        data = self.read_bytes(name)
        value = json.loads(data)
        if encode(value) != data: raise ValueError('Private artifact is not canonical JSON.')
        return value

    def publish(self, name, value):
        """Caller holds global storage admission through fsync and rename."""
        data = encode(value)
        path = self._path(name)
        if path.exists():
            if self.read_bytes(name) != data: raise ValueError('Immutable artifact differs.')
            return hashlib.sha256(data).hexdigest()
        assert_disk_headroom(len(data))
        path = self._path(name, create=True)
        part = path.with_name(path.name+'.part')
        if part.is_symlink(): raise ValueError('Unsafe private artifact path.')
        with self._open(part, write=True) as file:
            file.truncate(0)
            for offset in range(0, len(data), READ_BYTES):
                file.write(data[offset:offset+READ_BYTES])
            file.flush(); os.fsync(file.fileno())
        _durable_rename(part, path)
        return hashlib.sha256(data).hexdigest()
