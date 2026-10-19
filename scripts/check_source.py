"""Check the exact Git source set; report paths/rules, never matching values."""
import re
import subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
PATTERNS={
    'private-key':rb'-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----',
    'provider-key':rb'\bsk-(?:proj-)?[A-Za-z0-9_-]{30,}',
    'github-token':rb'\bgh[pousr]_[A-Za-z0-9]{30,}',
    'credential-url':rb"""https?://[^\s/:'"]+:[^\s/@'"]+@""",
}

def inspect(path,data):
    parts=Path(path).parts
    name=Path(path).name
    if any(part in ('.local','.venv','.git','private','backups','archives','quarantine','__pycache__') for part in parts): return 'private-path'
    if (name=='.env' or name.startswith('.env.')) and not name.endswith('.example'): return 'environment-file'
    if name=='.secret' or name.endswith(('.sqlite3','.db','.pem','.key','.p12','.pfx','.pyc')): return 'private-file'
    # Explicit synthetic SSRF regression fixture; no general test-file exemption.
    if Path(path).as_posix()=='app/recruiting/tests/test_interviews.py':
        fixture=b'https://'+b'user:pass'+b'@docs.python.org/'
        data=data.replace(fixture,b'https://synthetic-fixture.invalid/')
    for rule,pattern in PATTERNS.items():
        if re.search(pattern,data): return rule
    return None

def main():
    paths=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=ROOT).split(b'\0')
    failures=[]; seen=set()
    for raw in paths:
        if not raw: continue
        path=raw.decode('utf-8')
        if path in seen: continue
        seen.add(path)
        rule=inspect(path,(ROOT/path).read_bytes())
        if rule: failures.append((path,rule))
    for path,rule in failures: print(f'{rule}: {path}')
    if failures: raise SystemExit(1)
    print(f'Source check passed: {len(seen)} files; no excluded private paths or recognized secret patterns.')

if __name__=='__main__': main()
