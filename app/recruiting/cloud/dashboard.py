"""Bounded, owner-scoped submission and report views."""
from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Count, F, Prefetch, Sum
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from recruiting.accounts import verified_required
from recruiting.entitlements import require_analysis_access
from recruiting.models import Candidate, Job, Profile
from .contracts import TERMINAL_STAGES
from .models import CloudBatch, CloudResultReference

STAGE_LABELS = {'uploading':'Draft', 'validate':'Queued', 'role':'Reviewing the role',
    'analyze':'Preparing briefs', 'completed':'Ready', 'failed':'Needs attention',
    'cancelled':'Cancelled', 'expired':'Expired'}


def readable_results(owner, account_key):
    """A stored artifact key alone is not a readable, authorized candidate brief."""
    return CloudResultReference.objects.filter(batch__owner=owner,
        batch__account_key=account_key, batch__stage='completed',
        candidate__session__owner=owner, candidate__session_id=F('batch__session_id'),
        candidate__digest=F('input_sha256'),
        candidate__rubric_version=F('batch__rubric_version'),
        job__candidate_id=F('candidate_id'), job__status='completed'
    ).select_related('candidate').defer('candidate__blocks').order_by('created_at')


def dashboard_entry(request):
    if not settings.AUTOMATIC_WORKFLOW_ENABLED:
        from recruiting.views import dashboard
        return dashboard(request)
    return automatic_dashboard(request)


@verified_required
@never_cache
def automatic_dashboard(request):
    profile = Profile.objects.get(user=request.user)
    results = readable_results(request.user, profile.account_key)
    batches = CloudBatch.objects.filter(owner=request.user, session__owner=request.user, account_key=profile.account_key
    ).select_related('session').defer('jd_text','input_manifest','expanded_manifest',
        'rubric_snapshot','session__jd','session__rubric'
    ).annotate(input_count=Count('inputs',distinct=True)
    ).prefetch_related(Prefetch('result_references',queryset=results,to_attr='ready_briefs')
    ).order_by('-created_at')
    page = Paginator(batches,20).get_page(request.GET.get('page'))
    cards = []
    for batch in page:
        label = STAGE_LABELS.get(batch.stage,'Needs attention')
        if batch.stage == 'completed' and not batch.ready_briefs:
            label = 'Results unavailable'
        cards.append({'batch':batch, 'stage_label':label,
                      'ready_count':len(batch.ready_briefs)})
    reserved = (CloudBatch.objects.filter(owner=request.user).aggregate(total=Sum('reserved_credits'))['total'] or 0)
    reserved += Job.objects.filter(candidate__session__owner=request.user,reserved=True).count()
    available = max(0,30-profile.completed_credits-reserved)
    reason = ''
    try:
        require_analysis_access(profile)
        if settings.AUTOMATIC_TRANSPORT == 'fixtures' and available < 3:
            reason = 'The fictional sample needs three available briefs. Existing results remain available below.'
        elif not available:
            reason = 'Your remaining trial briefs are reserved for submissions already in progress.'
    except ValueError as exc:
        reason = str(exc)
    legacy = Candidate.objects.filter(session__owner=request.user,job__status='completed'
    ).exclude(cloud_results__isnull=False).only('id','filename','created_at')
    return render(request,'recruiting/automatic_dashboard.html',{
        'profile':profile, 'batch_cards':cards, 'page_obj':page,
        'submission_count':page.paginator.count,
        'active_count':batches.exclude(stage__in=('uploading',*TERMINAL_STAGES)).count(),
        'ready_count':results.count()+legacy.count(), 'reserved_credits':reserved,
        'available_credits':available, 'can_start':not reason, 'access_reason':reason,
        'synthetic_demo':settings.AUTOMATIC_TRANSPORT=='fixtures',
        'legacy_reports':list(legacy.order_by('-created_at')[:6])})
