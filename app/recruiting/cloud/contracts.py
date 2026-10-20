"""Metadata-only wire contracts. Upload confirmation follows trusted byte verification.

The VPS accepts/stores opaque bytes only. An isolated, authenticated cloud worker
scans and parses them, returning a fenced validation or analysis manifest.
No URLs, session credentials, candidate text, or provider secrets belong here.
"""
import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import TypedDict

MAX_CANDIDATES = 30
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_BATCH_BYTES = 300 * 1024 * 1024
MAX_JD_CHARACTERS = 30000
TERMINAL_STAGES = ('completed', 'failed', 'cancelled', 'expired')
WORKER_STAGES = ('validate', 'role', 'analyze')


class InputSpec(TypedDict):
    client_key: str
    filename: str
    role: str  # resume | archive | jd
    byte_size: int
    sha256: str


def digest_manifest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def checked_input(value):
    if not isinstance(value, dict):
        raise ValueError('Invalid input metadata.')
    result = {key: value.get(key) for key in ('client_key', 'filename', 'role', 'byte_size', 'sha256')}
    if not isinstance(result['client_key'], str) or not 1 <= len(result['client_key']) <= 80:
        raise ValueError('Invalid input identity.')
    check_file(result)
    if result['role'] not in ('resume', 'archive', 'jd'):
        raise ValueError('Invalid input role.')
    limit = MAX_BATCH_BYTES if result['role'] == 'archive' else MAX_FILE_BYTES
    if result['byte_size'] > limit:
        raise ValueError('Input exceeds size limit.')
    return result


def check_file(value):
    name, size, digest = value.get('filename'), value.get('byte_size'), value.get('sha256')
    if not isinstance(name, str) or not 1 <= len(name) <= 200 or '/' in name or '\\' in name or any(ord(c) < 32 for c in name) or name in ('.', '..'):
        raise ValueError('Invalid file name.')
    if type(size) is not int or size <= 0:
        raise ValueError('Invalid byte size.')
    if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
        raise ValueError('Invalid SHA-256 digest.')


def private_result_key(batch, value):
    prefix = f'cloud/{batch.account_key}/{batch.pk}/results/'
    if not isinstance(value, str) or len(value) > 250 or not value.startswith(prefix):
        raise ValueError('Result must use this batch private storage namespace.')
    tail = value[len(prefix):]
    if not tail or '\\' in value or any(part in ('', '.', '..') for part in tail.split('/')) or str(PurePosixPath(value)) != value or not re.fullmatch('[a-zA-Z0-9_./-]+', value):
        raise ValueError('Invalid private result key.')
    return value
