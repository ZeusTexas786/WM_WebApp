from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.http import HttpResponseBadRequest, FileResponse, JsonResponse, HttpResponse
from django.conf import settings
from pathlib import Path
from django.db import transaction
from django.utils import timezone
from django.views.decorators.http import require_POST
from .models import HiringSession, Profile, Candidate, Job, Preference
from .forms import SessionForm
from .accounts import verified_required

@verified_required
def dashboard(request):
    sessions = HiringSession.objects.filter(owner=request.user).order_by('-created_at')
    profile = Profile.objects.get(user=request.user)
    return render(request, 'recruiting/dashboard.html', {'sessions': sessions, 'profile': profile, 'session_count': sessions.count(), 'credits_left': max(0, 30-profile.completed_credits)})

@verified_required
def session_new(request):
    form = SessionForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        session = form.save(commit=False)
        session.owner = request.user
        # Sample mode is deliberately a fixed synthetic workflow, not a false AI claim.
        if session.mode == 'sample':
            session.rubric = [{'id': 'c1', 'name': 'Python backend development', 'weight': 3}, {'id': 'c2', 'name': 'PostgreSQL and data modelling', 'weight': 2}, {'id': 'c3', 'name': 'Automated testing and reliability', 'weight': 2}, {'id': 'c4', 'name': 'Cloud deployment and operations', 'weight': 1}]
        else:
            if not settings.AI_API_KEY:
                form.add_error(None,'Cloud AI is not configured. Use sample mode or ask the operator to configure live analysis.')
                return render(request, 'recruiting/session_new.html', {'form': form})
            session.rubric_status='queued'
        with transaction.atomic():
            from .coordination import lock_profile
            profile=lock_profile(request.user.pk)
            if profile.cancelled_at or profile.status!='active': return HttpResponseBadRequest('Account is not accepting new sessions.')
            if session.mode=='live':
                from .entitlements import require_analysis_access
                try: require_analysis_access(profile)
                except ValueError as exc: return HttpResponseBadRequest(str(exc))
                if HiringSession.objects.filter(owner=request.user,mode='live').count()>=30:
                    return HttpResponseBadRequest('The trial allows up to 30 live role analyses.')
            if HiringSession.objects.filter(owner=request.user,rubric_status__in=['queued','processing']).count()>=4:
                form.add_error(None,'Four roles are already queued. Wait for their analysis to complete.')
                return render(request,'recruiting/session_new.html',{'form':form})
            session.save()
        return redirect(f'/sessions/{session.pk}/')
    return render(request, 'recruiting/session_new.html', {'form': form})

@verified_required
def session_detail(request, pk):
    session = get_object_or_404(HiringSession, pk=pk, owner=request.user)
    all_jobs = list(Job.objects.filter(candidate__session=session).select_related('candidate').order_by('created_at'))
    jobs=[job for job in all_jobs if job.candidate.rubric_version==session.rubric_version]
    older_jobs=[job for job in all_jobs if job.candidate.rubric_version!=session.rubric_version]
    jobs.sort(key=lambda job: (job.status != 'completed', -job.result.get('alignment',0)))
    return render(request, 'recruiting/session_detail.html', {'hiring': session, 'jobs': jobs,'older_jobs':older_jobs, 'pending_jobs':any(job.status in ('queued','processing') for job in all_jobs)})

@verified_required
@require_POST
def confirm(request, pk):
    session = get_object_or_404(HiringSession, pk=pk, owner=request.user)
    names, weights = request.POST.getlist('names'), request.POST.getlist('weights')
    try:
        if not 1 <= len(names) <= 12 or len(names) != len(weights):
            raise ValueError
        rubric = []
        for i, (name, weight) in enumerate(zip(names, weights)):
            value = int(weight)
            if not name.strip() or len(name) > 200 or not 1 <= value <= 10:
                raise ValueError
            rubric.append({'id': f'c{i+1}', 'name': name.strip(), 'weight': value})
        if len({item['name'].lower() for item in rubric}) != len(rubric):
            raise ValueError
        from .assessment import validate_criteria
        validate_criteria(rubric)
    except (ValueError, TypeError):
        return HttpResponseBadRequest('Use 1–12 unique criteria, each with a weight from 1 to 10.')
    with transaction.atomic():
        session=HiringSession.objects.select_for_update().get(pk=session.pk,owner=request.user)
        session.rubric = rubric
        session.rubric_version += 1
        session.confirmed_at = timezone.now()
        session.save()
    messages.success(request, 'Rubric confirmed. You can now add resumes. Existing reports keep their original rubric.')
    return redirect(f'/sessions/{session.pk}/')

@verified_required
@require_POST
def upload(request, pk):
    session = get_object_or_404(HiringSession, pk=pk, owner=request.user)
    if session.mode == 'sample':
        return HttpResponseBadRequest('Real resumes require a live session. Sample mode accepts only the built-in synthetic examples.')
    files = request.FILES.getlist('files')
    if not 1 <= len(files) <= 30:
        return HttpResponseBadRequest('Choose between 1 and 30 resumes.')
    from .uploads import admit_upload
    for file in files:
        try:
            candidate = admit_upload(request.user, session, file)
            messages.success(request, f'{candidate.filename}: queued for analysis.')
        except ValueError as exc:
            messages.error(request, f'{file.name[:100]}: {exc}')
    return redirect(f'/sessions/{session.pk}/')

@verified_required
def download(request, pk):
    candidate = get_object_or_404(Candidate, pk=pk, session__owner=request.user)
    if not settings.LOCAL_DEVELOPMENT and candidate.scan_status!='clean':
        return HttpResponse('This document is quarantined until a successful security scan.',status=403)
    root = Path(settings.PRIVATE_ROOT).resolve()
    path = (root/candidate.storage_name).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        from django.http import Http404
        raise Http404
    response = FileResponse(path.open('rb'), as_attachment=True, filename=candidate.filename)
    response['Content-Type'] = 'application/octet-stream'
    return response

@verified_required
@require_POST
def sample(request,pk):
    session=get_object_or_404(HiringSession,pk=pk,owner=request.user,mode='sample')
    if not session.confirmed_at: return HttpResponseBadRequest('Confirm the rubric first.')
    from .samples import SAMPLES
    from .uploads import admit_upload
    from django.core.files.uploadedfile import SimpleUploadedFile
    for name,headline,paragraphs,states in SAMPLES:
        try:
            file=SimpleUploadedFile(name.replace(' ','_')+'.txt','\n\n'.join(paragraphs).encode())
            candidate=admit_upload(request.user,session,file)
        except ValueError as exc: messages.error(request,str(exc))
    messages.success(request,'Synthetic candidates added. The worker will process each resume individually.')
    return redirect(f'/sessions/{pk}/')

@verified_required(track_activity=False)
def status(request,pk):
    session=get_object_or_404(HiringSession,pk=pk,owner=request.user)
    jobs=Job.objects.filter(candidate__session=session)
    return JsonResponse({'rubric_status':session.rubric_status,'rubric_error':session.rubric_error,'jobs':[{'id':job.pk,'status':job.status,'name':job.candidate.filename,'interview_pending':job.interview_requested or job.interview_error=='Generating interview pack…','interview_ready':bool(job.interviews)} for job in jobs.select_related('candidate')]})

@verified_required
@require_POST
def retry_role(request,pk):
    with transaction.atomic():
        from .coordination import lock_profile
        profile=lock_profile(request.user.pk)
        hiring=get_object_or_404(HiringSession.objects.select_for_update(),pk=pk,owner=request.user)
        if hiring.rubric_status=='failed' and not profile.cancelled_at and profile.status=='active':
            from .entitlements import require_analysis_access
            try: require_analysis_access(profile)
            except ValueError as exc: return HttpResponseBadRequest(str(exc))
            hiring.rubric_status='queued'; hiring.rubric_error=''; hiring.save(update_fields=['rubric_status','rubric_error'])
    return redirect(f'/sessions/{pk}/')

@verified_required
@require_POST
def retry(request,pk):
    candidate=get_object_or_404(Candidate,pk=pk,session__owner=request.user)
    from .jobs import enqueue
    try: enqueue(candidate,candidate.rubric_version)
    except ValueError as exc: messages.error(request,str(exc))
    return redirect(f'/sessions/{candidate.session_id}/')

@verified_required
@require_POST
def cancel(request,pk):
    candidate=get_object_or_404(Candidate,pk=pk,session__owner=request.user)
    from .jobs import cancel_job
    job=get_object_or_404(Job,candidate=candidate)
    cancel_job(job.pk,request.user)
    return redirect(f'/sessions/{candidate.session_id}/')

@verified_required
def report(request,pk):
    candidate=get_object_or_404(Candidate,pk=pk,session__owner=request.user)
    job=get_object_or_404(Job,candidate=candidate,status='completed')
    result=job.result
    return render(request,'recruiting/report.html',{'candidate':candidate,'job':job,'result':result,'strengths':[e for e in result['evaluations'] if e['status']=='met'],'gaps':[e for e in result['evaluations'] if e['status']!='met'],'brief_criteria':result['evaluations'][:4]})

@verified_required
def evidence(request,pk):
    candidate=get_object_or_404(Candidate,pk=pk,session__owner=request.user)
    job=get_object_or_404(Job,candidate=candidate,status='completed')
    return render(request,'recruiting/evidence.html',{'candidate':candidate,'job':job,'result':job.result})

@verified_required
def pdf(request,pk):
    candidate=get_object_or_404(Candidate,pk=pk,session__owner=request.user)
    job=get_object_or_404(Job,candidate=candidate,status='completed')
    from .brief_pdf import create_brief_pdf
    try: data=create_brief_pdf(candidate,job)
    except ValueError as exc: return HttpResponseBadRequest(str(exc))
    response=HttpResponse(data,content_type='application/pdf')
    response['Content-Disposition']='attachment; filename="webmonkie-candidate-brief.pdf"'
    return response

@verified_required
@require_POST
def interview_request(request,pk):
    candidate=get_object_or_404(Candidate,pk=pk,session__owner=request.user)
    job=get_object_or_404(Job,candidate=candidate,status='completed')
    if not request.user.profile.cancelled_at and not job.interviews and not job.interview_lease_until:
        Job.objects.filter(pk=job.pk,interview_requested=False,interview_lease_until__isnull=True).update(interview_requested=True,interview_error='')
        messages.success(request,'Interview pack queued. No additional brief credit is used.')
    return redirect(f'/candidates/{pk}/')

@verified_required
def account(request):
    profile=Profile.objects.get(user=request.user)
    from .lifecycle import evaluate_lifecycle
    from datetime import timedelta
    return render(request,'recruiting/account.html',{'profile':profile,'trial_end':profile.verified_at+timedelta(hours=72),'preferences':Preference.objects.filter(owner=request.user).order_by('-created_at'),'lifecycle':evaluate_lifecycle(profile,timezone.now())})

@verified_required
@require_POST
def preference(request):
    from .interviews import save_approved_preference
    try:
        save_approved_preference(request.user,request.POST.get('preference',''))
        messages.success(request,'Preference saved for this account’s interview packs. Scores and other accounts are unchanged.')
    except ValueError as exc: messages.error(request,str(exc))
    return redirect('/account/')

@verified_required
@require_POST
def preference_delete(request,pk):
    get_object_or_404(Preference,pk=pk,owner=request.user).delete()
    return redirect('/account/')
