from datetime import timedelta
from django.db import transaction
from django.http import HttpResponse,FileResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.views.decorators.http import require_POST
from .accounts import verified_required
from .models import Profile
from .cloud.storage import admission_view

EXPORT_LIMIT=512*1024*1024

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
    from .lifecycle_execution import fence_work
    with transaction.atomic():
        profile=Profile.objects.select_for_update().get(user=request.user)
        if profile.status in ('active','archived') and profile.cancelled_at and timezone.now()<profile.cancelled_at+timedelta(days=3):
            profile.cancelled_at=None; profile.notice_at=None
            profile.save(update_fields=['cancelled_at','notice_at'])
            fence_work(profile)
    return redirect('/account/')

@admission_view
@verified_required
@require_POST
def restore(request):
    from .archives import restore_account
    try: restore_account(request.user.profile.pk,allow_cancelled=True)
    except ValueError: return HttpResponse('Restore is unavailable. Contact support.',status=409)
    return redirect('/dashboard/')

@admission_view
@verified_required
def export_account(request):
    profile=Profile.objects.get(user=request.user)
    if profile.status=='archived':
        from .archives import restore_account
        try: restore_account(profile.pk,allow_cancelled=True)
        except ValueError: return HttpResponse('Workspace restore is unavailable.',status=409)
    elif profile.status!='active': return HttpResponse('Restore the workspace before exporting.',status=409)
    from .archives import temp_root,export_bundle
    from .cloud.lifecycle import BoundedPrivateFile
    output=None
    try:
        with transaction.atomic():
            profile=Profile.objects.select_for_update().get(user=request.user)
            if profile.status!='active': raise ValueError
            output=BoundedPrivateFile(temp_root(profile),name='export-',limit=EXPORT_LIMIT)
            export_bundle(profile,output)
        output.seek(0)
        return FileResponse(output,as_attachment=True,filename='webmonkie-workspace.zip')
    except Exception:
        if output: output.close()
        return HttpResponse('Export could not be prepared. Please try again.',status=409)
