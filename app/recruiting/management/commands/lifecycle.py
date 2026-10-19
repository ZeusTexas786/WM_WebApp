import time
from django.conf import settings
from django.core.management.base import BaseCommand,CommandError
from django.utils import timezone
from recruiting.models import LifecycleAction,ServiceHeartbeat
from recruiting.lifecycle_execution import schedule_lifecycle,execute_action

class Command(BaseCommand):
    help='Preview retention actions; --execute explicitly enables configured production execution.'
    def add_arguments(self,parser):
        parser.add_argument('--execute',action='store_true')
        parser.add_argument('--watch',action='store_true')
    def handle(self,*args,**options):
        while True:
            try: proposals=schedule_lifecycle(dry_run=not options['execute'])
            except ValueError as exc: raise CommandError(str(exc))
            self.stdout.write(f'{len(proposals)} lifecycle actions proposed.')
            if options['execute']:
                ServiceHeartbeat.objects.update_or_create(service='scheduler',defaults={'seen_at':timezone.now()})
                for key in LifecycleAction.objects.filter(state__in=['pending','failed','running']).values_list('pk',flat=True).iterator():
                    execute_action(key)
            if not options['watch']: break
            time.sleep(30)
