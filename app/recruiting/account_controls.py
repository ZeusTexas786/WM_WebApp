import tempfile
import zipfile
from datetime import timedelta
from pathlib import Path
from django.conf import settings
from django.core import serializers
from django.db import transaction
from django.http import HttpResponse,FileResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.views.decorators.http import require_POST
from .accounts import verified_required
from .models import Profile,HiringSession,Candidate,Job,Preference

@verified_required
@require_POST
def cancel_account(request):
    from .lifecycle_execution import fence_work
    with transaction.atomic():
        profile=Profile.objects.select_for_update().get(user=request.user)
        if not profile.cancelled_at:
            profile.cancelled_at=timezone.now(); profile.save(update_fields=['cancelled_at']); fence_work(profile)
    return redirect('/account/')

@verified_required
@require_POST
def reactivate(request):
    with transaction.atomic():
        profile=Profile.objects.select_for_update().get(user=request.user)
        if profile.status in ('active','archived') and profile.cancelled_at and timezone.now()<profile.cancelled_at+timedelta(days=3):
            profile.cancelled_at=None; profile.notice_at=None
            profile.save(update_fields=['cancelled_at','notice_at'])
    return redirect('/account/')

@verified_required
@require_POST
def restore(request):
    from .archives import restore_account
    try: restore_account(request.user.profile.pk,allow_cancelled=True)
    except ValueError: return HttpResponse('Restore is unavailable. Contact support.',status=409)
    return redirect('/dashboard/')

@verified_required
def export_account(request):
    profile=Profile.objects.get(user=request.user)
    if profile.status=='archived':
        from .archives import restore_account
        try: restore_account(profile.pk,allow_cancelled=True)
        except ValueError: return HttpResponse('Workspace restore is unavailable.',status=409)
    elif profile.status!='active': return HttpResponse('Restore the workspace before exporting.',status=409)
    from .lifecycle_execution import account_folder
    # Use disk-backed temporary output; FileResponse closes it after transfer.
    output=tempfile.TemporaryFile()
    try:
        with transaction.atomic():
            profile=Profile.objects.select_for_update().get(user=request.user)
            if profile.status!='active': raise ValueError
            sessions=list(HiringSession.objects.filter(owner=request.user))
            candidates=list(Candidate.objects.filter(session__owner=request.user))
            jobs=list(Job.objects.filter(candidate__session__owner=request.user))
            preferences=list(Preference.objects.filter(owner=request.user))
            with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_STORED) as bundle:
                bundle.writestr('workspace.json',serializers.serialize('json',sessions+candidates+jobs+preferences))
                root=account_folder(profile)
                for candidate in candidates:
                    if not settings.LOCAL_DEVELOPMENT and candidate.scan_status!='clean': continue
                    path=Path(settings.PRIVATE_ROOT)/candidate.storage_name
                    if not path.resolve().is_relative_to(root) or path.is_symlink(): raise ValueError
                    if path.exists(): bundle.write(path,f'documents/{candidate.pk}{path.suffix}')
        output.seek(0)
        return FileResponse(output,as_attachment=True,filename='webmonkie-workspace.zip')
    except Exception:
        output.close()
        return HttpResponse('Export could not be prepared. Please try again.',status=409)
