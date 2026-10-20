"""Owner-verified control endpoints; raw bytes enter only bounded chunk routes."""
import io
import json
from functools import wraps
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.module_loading import import_string
from django.views.decorators.http import require_GET, require_POST
from recruiting.models import HiringSession, Profile
from recruiting.accounts import verified_required
from . import state
from .models import CloudBatch
from .storage import PrivateDiskStorage, accept_batch, reserve_uploads, storage_admission


def remote_ready():
    if not settings.AUTOMATIC_WORKFLOW_ENABLED or settings.AUTOMATIC_TRANSPORT != 'remote' or not settings.AUTOMATIC_REMOTE_EXECUTOR:
        return False
    try:
        # Operator-trusted adapter only. ready() checks local configuration;
        # never probe services or spend tokens while handling an intake request.
        executor = import_string(settings.AUTOMATIC_REMOTE_EXECUTOR)()
        return executor.ready() is True
    except Exception:
        return False


def _json(request, allowed):
    if request.content_type != 'application/json':
        raise ValueError('Use a JSON control request.')
    raw = request.read(65537)
    if len(raw) > 65536: raise ValueError('Control request is too large.')
    try: value = json.loads(raw)
    except (ValueError, UnicodeError) as exc: raise ValueError('Invalid JSON request.') from exc
    if not isinstance(value, dict) or set(value)-set(allowed):
        raise ValueError('Unexpected control fields.')
    return value


def _error_boundary(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        try:
            origin = request.headers.get('Origin')
            if origin and origin != settings.PUBLIC_ORIGIN.rstrip('/'):
                return JsonResponse({'error':'Upload origin is not approved.'}, status=403)
            response = view(request, *args, **kwargs)
        except (ValueError, TypeError, OverflowError) as exc:
            response = JsonResponse({'error':str(exc) or 'Invalid upload request.'},status=400)
        except OSError:
            # File paths, user contents and credentials must not reach logs or UI.
            response = JsonResponse({'error':'Private storage is unavailable. Resume or retry this file later.'}, status=503)
        response['Cache-Control'] = 'no-store'
        return response
    return wrapped


def _live_gate():
    if not remote_ready():
        return JsonResponse({'error':'Live uploads are unavailable until remote execution is configured. Try the synthetic demonstration.'},status=503)


def _owned(request, batch_id):
    key=Profile.objects.filter(user=request.user).values_list('account_key',flat=True).first()
    return get_object_or_404(CloudBatch, pk=batch_id, owner=request.user,
                            session__owner=request.user, account_key=key)


def _input(request, batch_id, reservation_id):
    batch = _owned(request,batch_id)
    if batch.session.mode == 'sample': raise ValueError('Synthetic inputs are fixed by the server.')
    return get_object_or_404(batch.inputs,pk=reservation_id)


@login_required
@_error_boundary
def new_batch(request):
    if request.method == 'GET':
        return render(request,'recruiting/cloud_session.html',{'live_available':remote_ready(), 'new':True,
            'sample_available':settings.AUTOMATIC_WORKFLOW_ENABLED and settings.AUTOMATIC_TRANSPORT=='fixtures'})
    if request.method != 'POST': return JsonResponse({'error':'POST required.'},status=405)
    denied = _live_gate()
    if denied is not None: return denied
    data = _json(request, ('title','jd','input_mode'))
    title, jd = data.get('title'), data.get('jd','')
    if not isinstance(title,str) or not title.strip() or len(title)>150 or not isinstance(jd,str) or len(jd)>30000:
        raise ValueError('Provide a title and a job description up to 30,000 characters.')
    with transaction.atomic():
        profile=Profile.objects.select_for_update().filter(user=request.user).first()
        if profile is None: raise ValueError('Verify your account before creating a batch.')
        state.require_analysis_access(profile)
        session=HiringSession.objects.create(owner=request.user,title=title.strip(),jd=jd,mode='live',rubric_status='queued')
        batch=state.create_batch(request.user.pk, session.pk, data.get('input_mode','files'))
    return JsonResponse({'batch_id':str(batch.pk), 'url':f'/batches/{batch.pk}/'},status=201)


@login_required
@require_POST
@_error_boundary
def sample_batch(request):
    if not settings.AUTOMATIC_WORKFLOW_ENABLED or settings.AUTOMATIC_TRANSPORT != 'fixtures':
        return JsonResponse({'error':'Synthetic demonstration is unavailable.'},status=503)
    _json(request, ())
    from .sample_contract import SAMPLE_TITLE, SAMPLE_JD, sample_rubric, sample_catalog
    with storage_admission():
        profile=Profile.objects.select_for_update().filter(user=request.user).first()
        if profile is None: raise ValueError('Verify your account before creating a batch.')
        state.require_analysis_access(profile)
        session=HiringSession.objects.create(owner=request.user,title=SAMPLE_TITLE,jd=SAMPLE_JD, mode='sample',
            rubric=sample_rubric(),rubric_version=1,confirmed_at=timezone.now())
        batch=state.create_batch(request.user.pk,session.pk,'files')
        catalog=sample_catalog()
        specs=[{key:value for key,value in row.items() if key!='content'} for row in catalog]
        items=reserve_uploads(batch.pk,request.user.pk,specs)
        disk=PrivateDiskStorage(request.user.pk)
        # Trusted source bytes only: no parser/scanner bypass for customer files.
        try:
            for item, row in zip(items,catalog):
                disk.write_chunk(item,0,io.BytesIO(row['content']),len(row['content']))
                disk.finalize_upload(item)
            accept_batch(batch.pk,request.user.pk)
        except Exception:
            # Files are not transactional; remove only known server-generated IDs.
            for item in items:
                for part in (False,True): disk._path(item,part).unlink(missing_ok=True)
            raise
    return JsonResponse({'batch_id':str(batch.pk),'url':f'/batches/{batch.pk}/','synthetic':True},status=201)


@verified_required
@require_GET
@_error_boundary
def batch_page(request,batch_id):
    batch=_owned(request,batch_id)
    from .dashboard import STAGE_LABELS, readable_results
    profile=Profile.objects.get(user=request.user)
    results=list(readable_results(request.user,profile.account_key).filter(batch=batch))
    return render(request,'recruiting/cloud_session.html',{'batch':batch,'stage_label':STAGE_LABELS.get(batch.stage,'Needs attention'),
        'input_count':batch.inputs.filter(role='resume').count(),'synthetic':batch.session.mode=='sample',
        'live_available':remote_ready(),'result_references':results,
        'processing':batch.stage in ('validate','role','analyze')})


@verified_required(track_activity=False)
@require_GET
@_error_boundary
def batch_status(request,batch_id):
    from .contracts import TERMINAL_STAGES
    from .dashboard import STAGE_LABELS, readable_results
    batch=_owned(request,batch_id)
    profile=Profile.objects.get(user=request.user)
    return JsonResponse({'stage':batch.stage,'label':STAGE_LABELS.get(batch.stage,'Needs attention'),
        'terminal':batch.stage in TERMINAL_STAGES,
        'ready_count':readable_results(request.user,profile.account_key).filter(batch=batch).count()})


@login_required
@require_POST
@_error_boundary
def reserve(request,batch_id):
    denied=_live_gate()
    if denied is not None: return denied
    batch=_owned(request,batch_id)
    if batch.session.mode=='sample': raise ValueError('Synthetic inputs are fixed by the server.')
    data=_json(request,('files',))
    items=reserve_uploads(batch.pk,request.user.pk,data.get('files'))
    disk=PrivateDiskStorage(request.user.pk)
    grants=[disk.initiate_upload(item) for item in items]
    return JsonResponse({'uploads':[{'id':grant.reservation_id,'offset':grant.offset,'chunk_bytes':grant.chunk_bytes,
        'expires_at':grant.expires_at.isoformat()} for grant in grants]},status=201)


@login_required
@require_GET
@_error_boundary
def resume(request,batch_id,reservation_id):
    denied=_live_gate()
    if denied is not None: return denied
    item=_input(request,batch_id,reservation_id)
    grant=PrivateDiskStorage(request.user.pk).initiate_upload(item)
    return JsonResponse({'id':grant.reservation_id,'offset':grant.offset,'chunk_bytes':grant.chunk_bytes})


@login_required
@require_POST
@_error_boundary
def chunk(request,batch_id,reservation_id):
    denied=_live_gate()
    if denied is not None: return denied
    item=_input(request,batch_id,reservation_id)
    if request.content_type!='application/octet-stream' or set(request.GET)!= {'offset'}:
        raise ValueError('Use a sequential binary chunk with an offset.')
    try: offset=int(request.GET['offset']); length=int(request.META.get('CONTENT_LENGTH',''))
    except ValueError as exc: raise ValueError('Chunk length and offset are required.') from exc
    committed=PrivateDiskStorage(request.user.pk).write_chunk(item,offset,request,length)
    return JsonResponse({'offset':committed})


@login_required
@require_POST
@_error_boundary
def finish(request,batch_id,reservation_id):
    denied=_live_gate()
    if denied is not None: return denied
    _json(request,())
    item=_input(request,batch_id,reservation_id)
    result=PrivateDiskStorage(request.user.pk).finalize_upload(item)
    return JsonResponse({'id':result.reservation_id,'byte_size':result.byte_size,'complete':True})


@login_required
@require_POST
@_error_boundary
def cancel_upload(request,batch_id,reservation_id):
    denied=_live_gate()
    if denied is not None: return denied
    _json(request,())
    batch=_owned(request,batch_id)
    if batch.session.mode=='sample': raise ValueError('Synthetic inputs are fixed by the server.')
    with state._locked_batch(batch.pk,request.user.pk) as (profile,current):
        state._require_owned(profile,current)
        if current.stage!='uploading' or current.sealed_at is not None:
            raise ValueError('Inputs are sealed.')
        item=current.inputs.filter(pk=reservation_id).first()
        # A lost cancellation reply may be retried after its row was removed.
        # The owned, still-unsealed batch remains the authorization boundary.
        if item is not None: PrivateDiskStorage(request.user.pk).delete_input(item)
    return JsonResponse({'cancelled':True})


@login_required
@require_POST
@_error_boundary
def submit(request,batch_id):
    denied=_live_gate()
    if denied is not None: return denied
    _json(request,())
    batch=_owned(request,batch_id)
    if batch.session.mode=='sample': raise ValueError('Synthetic inputs are fixed by the server.')
    accepted=accept_batch(batch.pk,request.user.pk)
    return JsonResponse({'stage':accepted.stage,'url':f'/batches/{batch.pk}/'})
