"""Authenticated, bounded account archives. No archive paths come from users."""
import base64
import json
import os
import shutil
import struct
import tempfile
import zipfile
from datetime import timedelta
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.core import serializers
from django.db import transaction
from django.utils import timezone
from .models import Profile,HiringSession,Candidate,Job,Preference

LIMIT=512*1024*1024
METADATA_LIMIT=16*1024*1024
MAGIC=b'WMAR1'

def archive_key():
    try:
        key=base64.b64decode(settings.ARCHIVE_KEY,altchars=b'-_',validate=True)
        if len(key)!=32: raise ValueError
        return key
    except Exception as exc: raise ValueError('A valid 32-byte archive key is required.') from exc

def encrypt_stream(source,destination):
    aes=AESGCM(archive_key()); header=MAGIC+os.urandom(8)
    destination.write(header); counter=0
    while True:
        chunk=source.read(65536); final=not chunk
        nonce=header[5:]+counter.to_bytes(4,'big')
        sealed=aes.encrypt(nonce,chunk,header+counter.to_bytes(4,'big')+(b'F' if final else b'D'))
        destination.write(struct.pack('!I',0 if final else len(sealed))); destination.write(sealed)
        if final: break
        counter+=1

def decrypt_stream(source,destination):
    aes=AESGCM(archive_key()); header=source.read(13)
    if len(header)!=13 or not header.startswith(MAGIC): raise ValueError('Invalid archive.')
    counter=0; total=0
    try:
        while True:
            framed=source.read(4)
            if len(framed)!=4: raise ValueError
            size=struct.unpack('!I',framed)[0]
            if size and not 17<=size<=65552: raise ValueError
            ciphertext=source.read(size or 16)
            plaintext=aes.decrypt(header[5:]+counter.to_bytes(4,'big'),ciphertext,header+counter.to_bytes(4,'big')+(b'D' if size else b'F'))
            total+=len(plaintext)
            if total>LIMIT: raise ValueError
            destination.write(plaintext)
            if not size:
                if source.read(1): raise ValueError
                return
            counter+=1
    except Exception as exc: raise ValueError('Archive authentication failed.') from exc

def archive_path(profile):
    expected=f'{profile.account_key}.wmar'
    if profile.archive_name not in ('',expected): raise ValueError('Invalid archive name.')
    root=Path(settings.PRIVATE_ROOT).resolve()
    folder=root/'.archives'
    if folder.is_symlink(): raise ValueError('Unsafe archive directory.')
    return folder/expected

def temp_root(profile,clean=False):
    base=Path(settings.PRIVATE_ROOT).resolve()/'.staging'
    root=base/str(profile.account_key)
    if base.is_symlink() or root.is_symlink(): raise ValueError('Unsafe staging directory.')
    if clean and root.exists(): shutil.rmtree(root)
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    return root

def archive_account(profile_id):
    from .lifecycle_execution import account_folder,execution_allowed
    execution_allowed()
    # Commit the checkpoint before any document removal. A retry must reuse the
    # completed snapshot even when plaintext cleanup was only partly successful.
    with transaction.atomic():
        profile=Profile.objects.select_for_update().get(pk=profile_id)
        if profile.status!='archiving': raise ValueError('Account is not ready for archival.')
        previous_name=profile.archive_name
        destination=archive_path(profile); destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if not previous_name and destination.exists(): destination.unlink()
        profile.archive_name=f'{profile.account_key}.wmar'; profile.save(update_fields=['archive_name'])
    with transaction.atomic():
        profile=Profile.objects.select_for_update().get(pk=profile_id)
        if profile.status!='archiving': raise ValueError('Account archive was interrupted.')
        destination=archive_path(profile)
        folder=account_folder(profile)
        sessions=list(HiringSession.objects.filter(owner_id=profile.user_id))
        candidates=list(Candidate.objects.filter(session__owner_id=profile.user_id))
        jobs=list(Job.objects.filter(candidate__session__owner_id=profile.user_id))
        preferences=list(Preference.objects.filter(owner_id=profile.user_id))
        with tempfile.TemporaryDirectory(dir=temp_root(profile,clean=True)) as work:
            plain=Path(work)/'snapshot.zip'
            if not destination.exists():
                with zipfile.ZipFile(plain,'w',compression=zipfile.ZIP_STORED) as bundle:
                    metadata={'account':str(profile.account_key),'objects':serializers.serialize('json',sessions+candidates+jobs+preferences)}
                    encoded=json.dumps(metadata).encode()
                    if len(encoded)>METADATA_LIMIT: raise ValueError('Archive metadata exceeds safe restore bounds.')
                    bundle.writestr('metadata.json',encoded)
                    total=0
                    for item in candidates:
                        path=(Path(settings.PRIVATE_ROOT)/item.storage_name)
                        if not path.resolve().is_relative_to(folder) or path.is_symlink(): raise ValueError('Unsafe stored document path.')
                        if path.exists():
                            total+=path.stat().st_size
                            if total>settings.MAX_ACCOUNT_STORAGE_BYTES: raise ValueError('Archive storage limit exceeded.')
                            bundle.write(path,f'documents/{item.pk}')
                        elif item.byte_size or item.scan_status!='infected': raise ValueError('An expected source document is missing.')
                pending=Path(work)/'sealed'
                with plain.open('rb') as source,pending.open('wb') as target:
                    encrypt_stream(source,target); target.flush(); os.fsync(target.fileno())
                os.chmod(pending,0o600); os.replace(pending,destination)
                if os.name!='nt':
                    directory_fd=os.open(destination.parent,os.O_RDONLY|os.O_DIRECTORY)
                    try: os.fsync(directory_fd)
                    finally: os.close(directory_fd)
            # Verify a previous completed encrypted snapshot before resuming cleanup.
            with destination.open('rb') as source,plain.open('wb') as target: decrypt_stream(source,target)
            with zipfile.ZipFile(plain) as bundle:
                infos=bundle.infolist()
                if sum(i.file_size for i in infos)>LIMIT or bundle.getinfo('metadata.json').file_size>METADATA_LIMIT: raise ValueError('Unsafe snapshot bounds.')
                metadata=json.loads(bundle.read('metadata.json'))
                if metadata['account']!=str(profile.account_key): raise ValueError('Wrong snapshot account.')
                required={'metadata.json'}|{f'documents/{item.pk}' for item in candidates if item.byte_size or item.scan_status!='infected'}
                if len(infos)!=len(required) or {i.filename for i in infos}!=required: raise ValueError('Incomplete source document manifest.')
            if folder.exists(): shutil.rmtree(folder)
            HiringSession.objects.filter(owner_id=profile.user_id).update(title='Archived role',jd='',rubric=[])
            Candidate.objects.filter(session__owner_id=profile.user_id).update(filename='Archived document',blocks=[])
            Job.objects.filter(candidate__session__owner_id=profile.user_id).update(jd='',rubric=[],result={},interviews={},error='',interview_error='')
            Preference.objects.filter(owner_id=profile.user_id).update(text='Archived preference')
            profile.status='archived'; profile.save(update_fields=['status'])

def restore_account(profile_id,allow_cancelled=False):
    from .lifecycle_execution import account_folder,execution_allowed
    execution_allowed()
    with transaction.atomic():
        profile=Profile.objects.select_for_update().get(pk=profile_id)
        if profile.status!='archived' or (profile.cancelled_at and (not allow_cancelled or timezone.now()>=profile.cancelled_at+timedelta(days=3))): raise ValueError('Account cannot be restored in its current state.')
        destination=archive_path(profile); folder=account_folder(profile)
        with tempfile.TemporaryDirectory(dir=temp_root(profile,clean=True)) as work:
            plain=Path(work)/'snapshot.zip'
            with destination.open('rb') as source,plain.open('wb') as target: decrypt_stream(source,target)
            with zipfile.ZipFile(plain) as bundle:
                infos=bundle.infolist()
                if len({i.filename for i in infos})!=len(infos) or sum(i.file_size for i in infos)>LIMIT: raise ValueError('Invalid archive bounds.')
                if bundle.getinfo('metadata.json').file_size>METADATA_LIMIT: raise ValueError('Invalid metadata bounds.')
                metadata=json.loads(bundle.read('metadata.json'))
                if metadata['account']!=str(profile.account_key): raise ValueError('Wrong account archive.')
                owned={HiringSession:set(HiringSession.objects.filter(owner_id=profile.user_id).values_list('pk',flat=True)),Candidate:set(Candidate.objects.filter(session__owner_id=profile.user_id).values_list('pk',flat=True)),Job:set(Job.objects.filter(candidate__session__owner_id=profile.user_id).values_list('pk',flat=True)),Preference:set(Preference.objects.filter(owner_id=profile.user_id).values_list('pk',flat=True))}
                objects=list(serializers.deserialize('json',metadata['objects']))
                documents={}
                for record in objects:
                    obj=record.object
                    if type(obj) not in owned or obj.pk not in owned[type(obj)]: raise ValueError('Archive object outside account.')
                    if isinstance(obj,HiringSession) and obj.owner_id!=profile.user_id: raise ValueError('Wrong session owner.')
                    if isinstance(obj,Preference) and obj.owner_id!=profile.user_id: raise ValueError('Wrong preference owner.')
                    if isinstance(obj,Candidate):
                        if obj.session_id not in owned[HiringSession]: raise ValueError('Wrong candidate session.')
                        path=Path(settings.PRIVATE_ROOT)/obj.storage_name
                        if not path.resolve().is_relative_to(folder): raise ValueError('Invalid restored document path.')
                        documents[f'documents/{obj.pk}']=path
                    if isinstance(obj,Job) and obj.candidate_id not in owned[Candidate]: raise ValueError('Wrong job owner.')
                if any(i.filename!='metadata.json' and i.filename not in documents for i in infos): raise ValueError('Unexpected archive entry.')
                # Never extract paths from ZIP entries; destinations come from validated metadata.
                for name,path in documents.items():
                    if name not in bundle.namelist(): continue
                    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                    with bundle.open(name) as source,path.open('wb') as target: shutil.copyfileobj(source,target,65536)
                for record in objects: record.save()
            profile.status='active'; profile.last_activity_at=timezone.now(); profile.notice_at=None
            profile.save(update_fields=['status','last_activity_at','notice_at'])
        profile.archive_name=''; profile.save(update_fields=['archive_name'])
        # Never destroy the recovery snapshot before the database commit succeeds.
        def remove_snapshot():
            try: destination.unlink(missing_ok=True)
            except OSError: pass  # Deterministic account path is retried by deletion/re-archival.
        transaction.on_commit(remove_snapshot)
