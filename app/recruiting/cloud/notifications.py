"""Small, leased transactional-email outbox; no document contents enter mail.

Queue functions belong in the caller's acceptance/completion transaction.
SMTP acceptance is recorded, not proof of delivery or an exactly-once promise.
"""
import secrets
from datetime import timedelta
from urllib.parse import urlsplit
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils import timezone
from recruiting.models import DeletionTombstone, Profile
from .contracts import TERMINAL_STAGES
from .models import CloudBatch, CloudOutbox

SENDER = 'WebMonkie by AscendWorksNow <ascendworksnow@gmail.com>'
MAX_ATTEMPTS = 3
LEASE_SECONDS = 300


def _active(profile, batch):
    return bool(profile and profile.verified_at and not profile.cancelled_at
                and profile.status == 'active' and profile.account_key == batch.account_key
                and profile.account_epoch == batch.account_epoch
                and not DeletionTombstone.objects.filter(account_key=profile.account_key).exists())


def _queue(batch, stage):
    with transaction.atomic():
        profile = Profile.objects.select_for_update().filter(user_id=batch.owner_id).first()
        current = CloudBatch.objects.select_for_update().filter(pk=batch.pk).first()
        if not current or not _active(profile, current):
            return None
        if not current.sealed_at or current.stage == 'uploading':
            raise ValueError('Accept the complete batch before notifying.')
        if stage in TERMINAL_STAGES and current.stage != stage:
            return None
        event, _ = CloudOutbox.objects.get_or_create(batch=current,
            event_key=f'notification:{stage}', defaults={'kind': 'notification', 'stage': stage})
        return event


def queue_acknowledgement(batch):
    return _queue(batch, 'acknowledgement')


def queue_terminal_notice(batch):
    if batch.stage not in TERMINAL_STAGES:
        return None
    return _queue(batch, batch.stage)


def _claim():
    now = timezone.now()
    due = Q(state='pending') | Q(state='running', lease_until__lte=now)
    available = Q(lease_until__isnull=True) | Q(lease_until__lte=now)
    candidate = CloudOutbox.objects.filter(due, available, kind='notification').order_by('created_at').first()
    if candidate is None:
        return None
    with transaction.atomic():
        profile = Profile.objects.select_for_update().filter(user_id=candidate.batch.owner_id).first()
        batch = CloudBatch.objects.select_for_update().filter(pk=candidate.batch_id).first()
        event = CloudOutbox.objects.select_for_update().filter(pk=candidate.pk).first()
        if not event or not batch:
            return None
        if event.state not in ('pending', 'running') or (event.lease_until and event.lease_until > now):
            return None
        if not _active(profile, batch):
            event.state = 'cancelled'
            event.lease_token = ''
            event.lease_until = None
            event.save(update_fields=['state', 'lease_token', 'lease_until'])
            return None
        if event.attempts >= MAX_ATTEMPTS:
            event.state = 'failed'
            event.save(update_fields=['state'])
            return None
        event.state = 'running'
        event.attempts += 1
        event.lease_token = secrets.token_urlsafe(32)
        event.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        event.save(update_fields=['state', 'attempts', 'lease_token', 'lease_until'])
        return event


def _session_url(batch):
    origin = urlsplit(settings.PUBLIC_ORIGIN)
    permitted_schemes = ('http', 'https') if settings.LOCAL_DEVELOPMENT else ('https',)
    if (origin.scheme not in permitted_schemes or origin.username or origin.password
            or origin.path not in ('', '/') or origin.query or origin.fragment
            or origin.hostname not in settings.ALLOWED_HOSTS):
        raise ValueError('Invalid notification origin.')
    return f'{origin.scheme}://{origin.netloc}/batches/{batch.pk}/'


def _message(event):
    batch = CloudBatch.objects.select_related('owner', 'session').get(pk=event.batch_id)
    if not batch.sealed_at or event.stage not in ('acknowledgement', *TERMINAL_STAGES):
        raise ValueError('Invalid notification event.')
    if event.stage != 'acknowledgement' and event.stage != batch.stage:
        raise ValueError('Notification stage is stale.')
    recipient = batch.owner.email
    validate_email(recipient)
    url = _session_url(batch)
    if event.stage == 'acknowledgement':
        subject = 'The WebMonkies have your brief'
        if batch.stage == 'completed':
            paragraphs = ['Your job description and resumes were safely received.',
                'The WebMonkies have put their heads together. Your review is ready.',
                'Open your session to see the results.']
        elif batch.stage in TERMINAL_STAGES:
            paragraphs = ['Your submission was received.',
                'Your review needs attention. Open your session for the current status.']
        else:
            paragraphs = ['Your job description and resumes are safely received.',
                'The WebMonkies have your brief. Serious work and spirited debate are on the agenda.',
                "Leave it with us. We'll email you when your results are ready."]
    elif batch.stage == 'completed':
        subject = 'Your WebMonkie results are ready'
        total, successful = len(batch.expanded_manifest), batch.settled_credits
        paragraphs = ['The WebMonkies have finished their review.',
                      f'{successful} of {total} candidate briefs are ready.']
        if successful < total:
            paragraphs.append('Some files could not be completed. Open your session for details and next steps.')
        paragraphs.append('Your results are ready to explore in your dashboard.')
    elif batch.stage in TERMINAL_STAGES:
        subject = 'Your WebMonkie review needs attention'
        paragraphs = ['Your review could not be completed.',
                      'Open your session for the current status and next steps.']
    else:
        raise ValueError('Results are not ready.')
    if batch.session.mode == 'sample':
        subject = f'Sample · {subject}'
        paragraphs.insert(0, 'This is a synthetic demonstration using fictional candidates, not a live AI assessment.')
    context = {'subject': subject, 'paragraphs': paragraphs, 'session_url': url,
               'logo_url': settings.PUBLIC_ORIGIN.rstrip('/')+'/static/recruiting/webmonkie-logo.png'}
    message = EmailMultiAlternatives(subject,
        render_to_string('recruiting/email/batch_notice.txt', context), SENDER, [recipient],
        reply_to=['ascendworksnow@gmail.com'])
    message.attach_alternative(render_to_string('recruiting/email/batch_notice.html', context), 'text/html')
    return message


def _settle(event, accepted, permanent=False):
    with transaction.atomic():
        owner = CloudBatch.objects.filter(pk=event.batch_id).values_list('owner_id', flat=True).first()
        if owner is None:
            return
        profile = Profile.objects.select_for_update().filter(user_id=owner).first()
        batch = CloudBatch.objects.select_for_update().filter(pk=event.batch_id).first()
        current = CloudOutbox.objects.select_for_update().filter(pk=event.pk, state='running',
                                                               lease_token=event.lease_token).first()
        if not current or not batch:
            return
        if not _active(profile, batch):
            current.state = 'cancelled'
        elif accepted:
            current.state = 'delivered'
            current.delivered_at = timezone.now()
        elif permanent or current.attempts >= MAX_ATTEMPTS:
            current.state = 'failed'
        else:
            current.state = 'pending'
        current.lease_token = ''
        current.lease_until = (timezone.now()+timedelta(seconds=60*2**current.attempts)
                               if current.state == 'pending' else None)
        current.save(update_fields=['state', 'delivered_at', 'lease_token', 'lease_until'])


def deliver_notifications(limit=1):
    """At most ten attempts per tick; network calls happen outside claim locks."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
        raise ValueError('Use a notification tick limit from 1 to 10.')
    if settings.EMAIL_BACKEND == 'django.core.mail.backends.smtp.EmailBackend' and not settings.EMAIL_HOST:
        return 0
    accepted = 0
    for _ in range(limit):
        event = _claim()
        if event is None:
            break
        sent = permanent = False
        try:
            message = _message(event)
            sent = message.send(fail_silently=False) == 1
        except (ValueError, ValidationError):
            permanent = True
        except Exception:
            # SMTP exceptions may include credentials/recipient details. Never persist/log them.
            pass
        _settle(event, sent, permanent)
        accepted += int(sent)
    return accepted
