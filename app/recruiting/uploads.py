import hashlib
import uuid
from pathlib import Path
from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from .models import Candidate,HiringSession

def store_upload(owner, session, upload):
    if session.owner_id != owner.pk or not session.confirmed_at:
        raise ValueError('A confirmed rubric in your own session is required.')
    suffix = Path(upload.name).suffix.lower()
    if suffix not in {'.txt', '.pdf', '.docx'} or not 0 < upload.size <= 10*1024*1024:
        raise ValueError('Use a nonempty PDF, DOCX, or TXT file up to 10 MB.')
    content = upload.read(10*1024*1024+1)
    if len(content) > 10*1024*1024:
        raise ValueError('File exceeds 10 MB.')
    if suffix == '.pdf' and not content.startswith(b'%PDF-'):
        raise ValueError('File contents do not match the PDF extension.')
    if suffix == '.docx' and not content.startswith(b'PK\x03\x04'):
        raise ValueError('File contents do not match the DOCX extension.')
    if suffix == '.txt':
        try:
            text = content.decode('utf-8-sig')
            if '\x00' in text or len(text.strip()) < 15:
                raise ValueError
        except (ValueError, UnicodeError) as exc:
            raise ValueError('Use a readable UTF-8 text resume.') from exc
    digest = hashlib.sha256(content).hexdigest()
    with transaction.atomic():
        from .coordination import lock_profile
        profile=lock_profile(owner.pk)
        session=HiringSession.objects.select_for_update().get(pk=session.pk,owner=owner)
        if profile.cancelled_at or profile.status!='active': raise ValueError('This account is not accepting uploads.')
        existing = Candidate.objects.filter(session=session, digest=digest, rubric_version=session.rubric_version).first()
        if existing:
            return existing
        retained=Candidate.objects.filter(session__owner=owner).aggregate(total=Sum('byte_size'))['total'] or 0
        if retained+len(content)>settings.MAX_ACCOUNT_STORAGE_BYTES:
            raise ValueError('Account storage limit reached. Remove documents or contact the operator.')
        root = Path(settings.PRIVATE_ROOT)
        folder = root / str(owner.pk) / str(session.pk)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (uuid.uuid4().hex+suffix)
        path.write_bytes(content)
        try:
            candidate=Candidate.objects.create(session=session, filename=Path(upload.name).name[:200], storage_name=path.relative_to(root).as_posix(), digest=digest, rubric_version=session.rubric_version,byte_size=len(content))
            candidate._new_upload=True
            return candidate
        except Exception:
            path.unlink(missing_ok=True)
            raise

def admit_upload(owner,session,upload):
    from .jobs import enqueue
    candidate=None
    try:
        with transaction.atomic():
            candidate=store_upload(owner,session,upload)
            enqueue(candidate,session.rubric_version)
        return candidate
    except Exception:
        if candidate is not None and getattr(candidate,'_new_upload',False):
            root=Path(settings.PRIVATE_ROOT).resolve()
            path=(root/candidate.storage_name).resolve()
            if path.is_relative_to(root): path.unlink(missing_ok=True)
        raise
