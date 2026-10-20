"""Strict route and readiness boundaries for the fictional demonstration."""
import hashlib
import os
import re
from datetime import timedelta
from pathlib import Path
from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone

UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
_EXACT = frozenset(('/', '/dashboard/', '/login/', '/logout/', '/register/',
    '/verify-pending/', '/resend/', '/recover/', '/recover/done/',
    '/account/', '/account/cancel/', '/account/reactivate/',
    '/health/live/', '/health/ready/', '/batches/new/', '/batches/sample/'))
_RESULT = re.compile(rf'^/(?:batches/{UUID}/(?:status/)?|candidates/{UUID}/(?:evidence/|pdf/)?)$')
_AUTH = re.compile(r'^/(?:verify/[^/]{1,2048}|reset/[^/]{1,128}/[^/]{1,128})/$')


class FixturePilotFence:
    """Run after HTTPS enforcement, before sessions/CSRF can read a body.

    This profile has no customer-file upload, source download, legacy execution,
    preference or internal worker endpoint. Allowed handlers still authenticate
    and authorize normally; matching a path never grants access to a result.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (not settings.FIXTURE_PILOT or request.path in _EXACT
                or _RESULT.fullmatch(request.path) or _AUTH.fullmatch(request.path)):
            return self.get_response(request)
        response = JsonResponse({'error':'This route is unavailable in the synthetic pilot.'}, status=403)
        response['Cache-Control'] = 'no-store'
        response['X-Content-Type-Options'] = 'nosniff'
        response['X-Frame-Options'] = 'DENY'
        response['Content-Security-Policy'] = "default-src 'none'; frame-ancestors 'none'"
        response['Referrer-Policy'] = 'same-origin'
        return response


def assert_fixture_ready():
    """Local checks only: never call a provider, scanner, parser or SMTP server."""
    if (settings.LOCAL_DEVELOPMENT or not settings.AUTOMATIC_WORKFLOW_ENABLED
            or settings.AUTOMATIC_TRANSPORT != 'fixtures' or settings.AUTOMATIC_REMOTE_EXECUTOR
            or type(settings.AI_MAX_DAILY_REQUESTS) is not int or settings.AI_MAX_DAILY_REQUESTS != 0
            or settings.AI_API_KEY or settings.LIFECYCLE_EXECUTION_ENABLED
            or not all((settings.EMAIL_HOST, settings.EMAIL_HOST_USER, settings.EMAIL_HOST_PASSWORD))):
        raise RuntimeError('Fixture configuration is not ready.')
    root = Path(settings.PRIVATE_ROOT)
    if not root.is_dir() or not os.access(root, os.W_OK | os.X_OK):
        raise RuntimeError('Private storage is not ready.')
    from .cloud.sample_contract import SAMPLE_JD, sample_catalog, sample_rubric
    from .assessment import validate_criteria
    catalog = sample_catalog()
    if len(catalog) != 3 or len({row['sha256'] for row in catalog}) != 3:
        raise RuntimeError('Fictional catalog is not ready.')
    for row in catalog:
        if len(row['content']) != row['byte_size'] or hashlib.sha256(row['content']).hexdigest() != row['sha256']:
            raise RuntimeError('Fictional catalog is not ready.')
    criteria = validate_criteria(sample_rubric())
    if any(item.get('source_quote') not in SAMPLE_JD for item in criteria):
        raise RuntimeError('Fictional role is not ready.')
    from .models import ServiceHeartbeat
    if not ServiceHeartbeat.objects.filter(service='cloud_dispatcher',
            seen_at__gte=timezone.now()-timedelta(seconds=60)).exists():
        raise RuntimeError('Dispatcher is not ready.')
    from .cloud.storage import storage_admission, assert_disk_headroom
    with storage_admission():
        assert_disk_headroom()
