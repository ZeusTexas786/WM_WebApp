"""Exact fictional catalog shared by intake and the fixture executor.

A sample mode flag or a candidate filename is never sufficient authority to
produce a synthetic assessment. The executor must independently recheck this
contract and the private bytes before using the built-in fictional outputs.
"""
import copy
import hashlib
from recruiting.samples import SAMPLES
from .contracts import digest_manifest

SAMPLE_TITLE = 'Python Backend Engineer'
SAMPLE_JD = 'We are hiring a Python backend engineer. Required skills: Python backend development; PostgreSQL and data modelling; automated testing and reliability; cloud deployment and operations.'
SAMPLE_RUBRIC = [
    {'id':'c1','name':'Python backend development','weight':3,'source_quote':'Python backend development'},
    {'id':'c2','name':'PostgreSQL and data modelling','weight':2,'source_quote':'PostgreSQL and data modelling'},
    {'id':'c3','name':'Automated testing and reliability','weight':2,'source_quote':'automated testing and reliability'},
    {'id':'c4','name':'Cloud deployment and operations','weight':1,'source_quote':'cloud deployment and operations'},
]


def sample_catalog():
    return [{'client_key':f'sample-{index}', 'filename':name.replace(' ','_')+'.txt',
             'role':'resume', 'byte_size':len(content), 'sha256':hashlib.sha256(content).hexdigest(),
             'content':content}
            for index, (name, _, paragraphs, _) in enumerate(SAMPLES)
            for content in ['\n\n'.join(paragraphs).encode('utf-8')]]


def sample_rubric():
    return copy.deepcopy(SAMPLE_RUBRIC)


def is_canonical_sample(batch):
    from recruiting.models import HiringSession
    from .models import CloudBatch
    # Reload so stale caller objects cannot hide an independently changed role.
    current = CloudBatch.objects.filter(pk=batch.pk).first()
    if not current: return False
    session = HiringSession.objects.filter(pk=current.session_id, owner_id=current.owner_id).first()
    if not session or session.mode != 'sample' or session.title != SAMPLE_TITLE or session.jd != SAMPLE_JD or session.rubric != SAMPLE_RUBRIC:
        return False
    if current.jd_text != SAMPLE_JD or current.rubric_snapshot != SAMPLE_RUBRIC or current.rubric_version != session.rubric_version or current.input_mode != 'files':
        return False
    expected = {row['client_key']:row for row in sample_catalog()}
    inputs = list(current.inputs.order_by('client_key'))
    if len(inputs) != 3 or not current.sealed_at:
        return False
    for item in inputs:
        spec = expected.get(item.client_key)
        if not spec or not item.uploaded_at or any(getattr(item,key) != spec[key] for key in ('filename','role','byte_size','sha256')):
            return False
        if item.storage_backend != 'private_vps' or item.storage_key != f'cloud/{current.account_key}/{current.pk}/inputs/{item.pk}':
            return False
    manifest = [{'id':str(item.pk), 'role':item.role, 'filename':item.filename,
        'byte_size':item.byte_size, 'sha256':item.sha256, 'storage_backend':item.storage_backend,
        'storage_key':item.storage_key} for item in inputs]
    return current.input_manifest == manifest and current.input_manifest_digest == digest_manifest({'inputs':manifest,'jd_text':SAMPLE_JD})


def verified_sample_inputs(batch):
    """Independently authorize the exact catalog and read its actual bytes.

    Labels alone never authorize fixture execution or a fixture validation
    attestation. This path does not parse or scan any customer document.
    """
    from django.conf import settings
    from .storage import PrivateDiskStorage
    if not settings.AUTOMATIC_WORKFLOW_ENABLED or settings.AUTOMATIC_TRANSPORT != 'fixtures' or not is_canonical_sample(batch):
        raise ValueError('Only the canonical fictional demonstration is supported.')
    expected = {row['client_key']:row for row in sample_catalog()}
    disk = PrivateDiskStorage(batch.owner_id)
    inputs = []
    for item in batch.inputs.order_by('client_key'):
        row = expected[item.client_key]
        content = disk.read_input(item, row['byte_size'])
        if content != row['content']:
            raise ValueError('Fictional source bytes changed.')
        inputs.append((item, content))
    return inputs
