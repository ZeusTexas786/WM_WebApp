"""Authenticated, bounded account archives. No archive paths come from users."""
import base64
import json
import os
import shutil
import struct
import zipfile
from datetime import timedelta
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.core import serializers
from django.db import transaction
from django.utils import timezone
from .models import Profile,HiringSession,Candidate,Job,Preference,DeletionTombstone
from .cloud.models import CloudBatch,UploadReservation,StageAttempt,CloudOutbox,CloudResultReference
from .cloud.lifecycle import (BoundedPrivateFile,account_files,owned_path,
                             remove_account_trees,require_exports_closed,safe_path)
from .cloud.storage import storage_admission

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
    return safe_path(f'.archives/{expected}')

def temp_root(profile,clean=False):
    root=safe_path(f'.staging/{profile.account_key}')
    # Never clean another active export in this account's scratch directory.
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    return root

def owned_objects(profile):
    return {
        HiringSession:list(HiringSession.objects.filter(owner_id=profile.user_id)),
        Candidate:list(Candidate.objects.filter(session__owner_id=profile.user_id)),
        Job:list(Job.objects.filter(candidate__session__owner_id=profile.user_id)),
        Preference:list(Preference.objects.filter(owner_id=profile.user_id)),
        CloudBatch:list(CloudBatch.objects.filter(owner_id=profile.user_id)),
        UploadReservation:list(UploadReservation.objects.filter(batch__owner_id=profile.user_id)),
        StageAttempt:list(StageAttempt.objects.filter(batch__owner_id=profile.user_id)),
        CloudOutbox:list(CloudOutbox.objects.filter(batch__owner_id=profile.user_id)),
        CloudResultReference:list(CloudResultReference.objects.filter(batch__owner_id=profile.user_id)),
    }


def _serialized(rows):
    value=serializers.serialize('json',rows)
    if len(value.encode())>METADATA_LIMIT: raise ValueError('Metadata exceeds safe bounds.')
    return value


def export_bundle(profile,output):
    """Export safe reports and fictional sources; never expose quarantine bytes."""
    if DeletionTombstone.objects.filter(account_key=profile.account_key).exists(): raise ValueError('Account unavailable.')
    owned=owned_objects(profile)
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_STORED) as bundle:
        bundle.writestr('workspace.json',_serialized([row for model in (HiringSession,Candidate,Job,Preference) for row in owned[model]]))
        bundle.writestr('cloud.json',_serialized([row for model in (CloudBatch,UploadReservation,StageAttempt,CloudOutbox,CloudResultReference) for row in owned[model]]))
        allowed_sources={}
        from .cloud.sample_contract import is_canonical_sample,sample_catalog
        catalog={row['client_key']:row for row in sample_catalog()}
        for batch in owned[CloudBatch]:
            if batch.account_key!=profile.account_key: raise ValueError('Invalid batch account.')
            if not is_canonical_sample(batch): continue
            verified={}
            for item in batch.inputs.all():
                path=owned_path(profile,item.storage_key)
                expected=catalog[item.client_key]['content']
                if not path.exists() or path.stat().st_size!=len(expected): break
                with path.open('rb') as source:
                    if source.read(len(expected)+1)!=expected: break
                verified[item.storage_key]=path
            else: allowed_sources.update(verified)
        exported=set()
        for candidate in owned[Candidate]:
            path=owned_path(profile,candidate.storage_name)
            if candidate.storage_name.startswith('cloud/'):
                if candidate.storage_name not in allowed_sources: continue
            elif candidate.scan_status!='clean' and not settings.LOCAL_DEVELOPMENT:
                continue
            if path.exists():
                bundle.write(path,f'documents/{candidate.pk}{path.suffix}')
                exported.add(candidate.storage_name)
        for key,path in allowed_sources.items():
            if key not in exported: bundle.write(path,f'documents/{path.name}.txt')
        # Export only published result references and their attempt receipts.
        # Partial/orphan bytes remain private and belong only in encrypted recovery.
        artifacts={}
        for ref in owned[CloudResultReference]:
            path=owned_path(profile,ref.storage_key)
            if ref.storage_backend!='private_vps' or ref.batch.account_key!=profile.account_key or not ref.storage_key.startswith(f'cloud/{profile.account_key}/{ref.batch_id}/results/'):
                raise ValueError('Invalid result owner.')
            artifacts[ref.storage_key]=path
        for attempt in owned[StageAttempt]:
            if attempt.account_key!=profile.account_key: raise ValueError('Invalid attempt owner.')
            key=f'cloud/{profile.account_key}/{attempt.batch_id}/results/{attempt.pk}/receipt.json'
            path=owned_path(profile,key)
            if path.exists(): artifacts[key]=path
        for key,path in artifacts.items(): bundle.write(path,key)


def _manifest(bundle,profile):
    infos=bundle.infolist()
    if len(infos)>10001 or len({i.filename for i in infos})!=len(infos) or sum(i.file_size for i in infos)>LIMIT:
        raise ValueError('Invalid snapshot bounds.')
    if bundle.getinfo('metadata.json').file_size>METADATA_LIMIT: raise ValueError('Invalid metadata bounds.')
    metadata=json.loads(bundle.read('metadata.json'))
    if metadata['account']!=str(profile.account_key): raise ValueError('Wrong account snapshot.')
    files=metadata.get('files')
    if files is not None:
        if not isinstance(files,list) or len(set(files))!=len(files): raise ValueError('Invalid snapshot manifest.')
        targets={f'files/{key}':owned_path(profile,key) for key in files}
        if set(bundle.namelist())!={'metadata.json',*targets}: raise ValueError('Incomplete snapshot manifest.')
    else: targets=None  # Legacy encrypted snapshots retain document-ID entries.
    return metadata,targets


def archive_account(profile_id):
    from .lifecycle_execution import execution_allowed,fence_work
    execution_allowed()
    # Separate checkpoint transaction must survive a later plaintext cleanup error.
    with storage_admission():
        profile=Profile.objects.select_for_update().get(pk=profile_id)
        if profile.status!='archiving' or DeletionTombstone.objects.filter(account_key=profile.account_key).exists():
            raise ValueError('Account is not ready for archival.')
        require_exports_closed(profile)
        destination=archive_path(profile)
        destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if not profile.archive_name:
            destination.unlink(missing_ok=True)
            fence_work(profile)
        profile.archive_name=f'{profile.account_key}.wmar'; profile.save(update_fields=['archive_name'])
    with storage_admission():
        profile=Profile.objects.select_for_update().get(pk=profile_id)
        if profile.status!='archiving': raise ValueError('Account archive was interrupted.')
        destination=archive_path(profile)
        with BoundedPrivateFile(temp_root(profile),name='snapshot-',limit=LIMIT) as plain:
            if not destination.exists():
                owned=owned_objects(profile); files=account_files(profile)
                for candidate in owned[Candidate]:
                    path=owned_path(profile,candidate.storage_name)
                    if not path.exists() and (candidate.byte_size or candidate.scan_status!='infected'):
                        raise ValueError('Expected source is missing.')
                metadata={'account':str(profile.account_key),'objects':_serialized([row for rows in owned.values() for row in rows]),'files':list(files)}
                encoded=json.dumps(metadata).encode()
                if len(encoded)>METADATA_LIMIT: raise ValueError('Snapshot metadata exceeds bounds.')
                with zipfile.ZipFile(plain,'w',compression=zipfile.ZIP_STORED) as bundle:
                    bundle.writestr('metadata.json',encoded)
                    for key,path in files.items(): bundle.write(path,f'files/{key}')
                plain.seek(0)
                with BoundedPrivateFile(temp_root(profile),name='sealed-',limit=LIMIT+1024*1024) as sealed:
                    encrypt_stream(plain,sealed); sealed.flush(); os.fsync(sealed.fileno())
                    sealed.file.close()
                    os.replace(sealed.name,destination)
                    if os.name!='nt':
                        descriptor=os.open(destination.parent,os.O_RDONLY|os.O_DIRECTORY)
                        try: os.fsync(descriptor)
                        finally: os.close(descriptor)
            # Authenticate before removal, including when retrying a partial cleanup.
            plain.seek(0); plain.truncate(0)
            with destination.open('rb') as source: decrypt_stream(source,plain)
            plain.seek(0)
            with zipfile.ZipFile(plain) as bundle: _manifest(bundle,profile)
            remove_account_trees(profile)
            HiringSession.objects.filter(owner_id=profile.user_id).update(title='Archived role',jd='',rubric=[])
            Candidate.objects.filter(session__owner_id=profile.user_id).update(filename='Archived document',blocks=[])
            Job.objects.filter(candidate__session__owner_id=profile.user_id).update(jd='',rubric=[],result={},interviews={},error='',interview_error='')
            Preference.objects.filter(owner_id=profile.user_id).update(text='Archived preference')
            CloudBatch.objects.filter(owner_id=profile.user_id).update(jd_text='',rubric_snapshot=[],input_manifest=[],expanded_manifest=[])
            UploadReservation.objects.filter(batch__owner_id=profile.user_id).update(filename='Archived document')
            profile.status='archived'; profile.save(update_fields=['status'])


def _restored_objects(metadata,profile):
    owned={model:{row.pk for row in rows} for model,rows in owned_objects(profile).items()}
    records=list(serializers.deserialize('json',metadata['objects']))
    seen=set()
    for record in records:
        obj=record.object
        identity=(type(obj),obj.pk)
        if type(obj) not in owned or obj.pk not in owned[type(obj)] or identity in seen: raise ValueError('Snapshot object outside account.')
        seen.add(identity)
        if isinstance(obj,(HiringSession,Preference,CloudBatch)) and obj.owner_id!=profile.user_id: raise ValueError('Wrong snapshot owner.')
        if isinstance(obj,(Candidate,CloudBatch)) and obj.session_id not in owned[HiringSession]: raise ValueError('Wrong snapshot session.')
        if isinstance(obj,Job) and obj.candidate_id not in owned[Candidate]: raise ValueError('Wrong snapshot candidate.')
        if isinstance(obj,CloudBatch) and obj.account_key!=profile.account_key: raise ValueError('Wrong snapshot account.')
        if isinstance(obj,(UploadReservation,StageAttempt,CloudOutbox,CloudResultReference)) and obj.batch_id not in owned[CloudBatch]: raise ValueError('Wrong snapshot batch.')
        if isinstance(obj,StageAttempt) and obj.account_key!=profile.account_key: raise ValueError('Wrong attempt account.')
        if isinstance(obj,UploadReservation):
            if obj.storage_backend!='private_vps' or obj.storage_key!=f'cloud/{profile.account_key}/{obj.batch_id}/inputs/{obj.pk}': raise ValueError('Wrong snapshot input.')
        if isinstance(obj,CloudResultReference):
            if obj.storage_backend!='private_vps' or not obj.storage_key.startswith(f'cloud/{profile.account_key}/{obj.batch_id}/results/') or (obj.candidate_id and obj.candidate_id not in owned[Candidate]) or (obj.job_id and obj.job_id not in owned[Job]): raise ValueError('Wrong snapshot result.')
            owned_path(profile,obj.storage_key)
        if isinstance(obj,Candidate): owned_path(profile,obj.storage_name)
        # Snapshots never restore active work, leases, quota promises or mail.
        if isinstance(obj,CloudBatch):
            if obj.stage not in ('completed','failed','cancelled','expired'): obj.stage='cancelled'
            obj.reserved_credits=0
        if isinstance(obj,StageAttempt):
            if obj.state=='running': obj.state='cancelled'
            obj.lease_token=''
        if isinstance(obj,CloudOutbox):
            if obj.state!='delivered': obj.state='cancelled'
            obj.lease_token=''; obj.lease_until=None
        if isinstance(obj,Job):
            if obj.status in ('queued','processing'): obj.status='cancelled'
            obj.reserved=False; obj.lease_token=''; obj.lease_until=None
            obj.interview_requested=False; obj.interview_lease_token=''; obj.interview_lease_until=None
        if isinstance(obj,HiringSession):
            if obj.rubric_status in ('queued','processing'): obj.rubric_status='failed'
            obj.rubric_lease=''; obj.rubric_lease_until=None
    return records


def restore_account(profile_id,allow_cancelled=False):
    from .lifecycle_execution import execution_allowed,fence_work
    execution_allowed()
    with storage_admission():
        profile=Profile.objects.select_for_update().get(pk=profile_id)
        if DeletionTombstone.objects.filter(account_key=profile.account_key).exists() or profile.status!='archived' or (profile.cancelled_at and (not allow_cancelled or timezone.now()>=profile.cancelled_at+timedelta(days=3))):
            raise ValueError('Account cannot be restored in its current state.')
        destination=archive_path(profile)
        with BoundedPrivateFile(temp_root(profile),name='restore-',limit=LIMIT) as plain:
            with destination.open('rb') as source: decrypt_stream(source,plain)
            plain.seek(0)
            with zipfile.ZipFile(plain) as bundle:
                metadata,targets=_manifest(bundle,profile)
                records=_restored_objects(metadata,profile)
                if targets is None:
                    targets={f'documents/{r.object.pk}':owned_path(profile,r.object.storage_name) for r in records if isinstance(r.object,Candidate) and f'documents/{r.object.pk}' in bundle.namelist()}
                    if set(bundle.namelist())!={'metadata.json',*targets}: raise ValueError('Unexpected snapshot entry.')
                # Targets derive from validated account-relative manifest, never ZIP paths.
                for name,path in targets.items():
                    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                    with bundle.open(name) as source,BoundedPrivateFile(path.parent,name='restore-part-',limit=LIMIT) as target:
                        shutil.copyfileobj(source,target,65536)
                        target.flush(); os.fsync(target.fileno())
                        target.file.close()
                        os.replace(target.name,path)
                for record in records: record.save()
            fence_work(profile)
            profile.status='active'; profile.last_activity_at=timezone.now(); profile.notice_at=None
            profile.archive_name=''
            profile.save(update_fields=['status','last_activity_at','notice_at','archive_name'])
        def remove_snapshot():
            try: destination.unlink(missing_ok=True)
            except OSError: pass  # Leftover encrypted bytes remain counted and erasable.
        transaction.on_commit(remove_snapshot)
