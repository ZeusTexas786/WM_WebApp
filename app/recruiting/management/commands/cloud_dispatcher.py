"""Bounded fixture stage ticks and transactional notice delivery."""
import time
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from django.utils import timezone
from recruiting.models import ServiceHeartbeat


class Command(BaseCommand):
    help = 'Run bounded synthetic workflow ticks; real remote execution is unavailable.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')
        parser.add_argument('--limit', type=int, default=1)
        parser.add_argument('--sleep', type=int, default=5)

    def handle(self, *args, **options):
        if not 1 <= options['limit'] <= 10: raise CommandError('Tick limit must be from 1 to 10.')
        if not 1 <= options['sleep'] <= 30: raise CommandError('Sleep must be from 1 to 30 seconds.')
        from recruiting.cloud.dispatch import cleanup_pending, dispatch_pending
        from recruiting.cloud.notifications import deliver_notifications
        while True:
            close_old_connections()
            ServiceHeartbeat.objects.update_or_create(service='cloud_dispatcher', defaults={'seen_at':timezone.now()})
            completed = dispatch_pending(options['limit'])
            cleanup_pending(options['limit'])
            notices = deliver_notifications(options['limit'])
            self.stdout.write(f'Workflow tick: {completed} stages completed; {notices} notices accepted.')
            if options['once']: break
            time.sleep(options['sleep'])
