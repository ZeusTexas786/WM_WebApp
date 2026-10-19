import tempfile
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.contrib.auth.models import User
from django.utils import timezone
from recruiting.models import Profile

class LifecycleExecutionTests(TestCase):
    def setUp(self):
        self.now=timezone.now()
        self.user=User.objects.create_user('lifecycle@example.test',email='lifecycle@example.test')
        self.profile=Profile.objects.create(user=self.user,verified_at=self.now,last_activity_at=self.now-timedelta(days=40))
        self.folder=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[3]/'.local')
        self.addCleanup(self.folder.cleanup)
        self.settings=override_settings(PRIVATE_ROOT=Path(self.folder.name),LOCAL_DEVELOPMENT=False,LIFECYCLE_EXECUTION_ENABLED=True,ARCHIVE_KEY='test')
        self.settings.enable(); self.addCleanup(self.settings.disable)

    def test_notice_outbox_is_idempotent_and_failure_does_not_start_clock(self):
        from recruiting.lifecycle_execution import schedule_lifecycle,execute_action
        from recruiting.models import LifecycleAction
        schedule_lifecycle(self.now,dry_run=False); schedule_lifecycle(self.now,dry_run=False)
        action=LifecycleAction.objects.get()
        with patch('recruiting.lifecycle_execution.send_mail',side_effect=OSError): execute_action(action.pk,self.now)
        self.profile.refresh_from_db(); self.assertIsNone(self.profile.notice_at)
        with patch('recruiting.lifecycle_execution.send_mail',return_value=1): execute_action(action.pk,self.now)
        self.profile.refresh_from_db(); self.assertEqual(self.profile.notice_at,self.now)

    def test_return_before_dispatch_invalidates_notice(self):
        from recruiting.lifecycle_execution import schedule_lifecycle,execute_action
        from recruiting.models import LifecycleAction
        schedule_lifecycle(self.now,dry_run=False)
        Profile.objects.filter(pk=self.profile.pk).update(last_activity_at=self.now)
        with patch('recruiting.lifecycle_execution.send_mail') as mail: execute_action(LifecycleAction.objects.get().pk,self.now)
        mail.assert_not_called()

    def test_deletion_scopes_files_and_records_tombstone(self):
        from recruiting.lifecycle_execution import schedule_lifecycle,execute_action
        from recruiting.models import LifecycleAction,DeletionTombstone
        self.profile.cancelled_at=self.now-timedelta(days=3); self.profile.save()
        own=Path(self.folder.name)/str(self.user.pk); own.mkdir(); (own/'resume.txt').write_text('synthetic')
        other=Path(self.folder.name)/'other'; other.mkdir(); (other/'keep').write_text('keep')
        scratch=Path(self.folder.name)/'.staging'/str(self.profile.account_key)
        scratch.mkdir(parents=True); (scratch/'interrupted-plain.zip').write_text('private scratch')
        schedule_lifecycle(self.now,dry_run=False)
        execute_action(LifecycleAction.objects.get().pk,self.now)
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())
        self.assertFalse(own.exists()); self.assertTrue((other/'keep').exists())
        self.assertFalse(scratch.exists())
        self.assertTrue(DeletionTombstone.objects.filter(account_key=self.profile.account_key).exists())

    def test_preview_cannot_execute_and_dry_run_writes_nothing(self):
        from recruiting.lifecycle_execution import schedule_lifecycle
        from recruiting.models import LifecycleAction
        schedule_lifecycle(self.now)
        self.assertEqual(LifecycleAction.objects.count(),0)
        with override_settings(LOCAL_DEVELOPMENT=True):
            with self.assertRaises(ValueError): schedule_lifecycle(self.now,dry_run=False)

    def test_stale_notice_attempt_cannot_overwrite_done_action(self):
        from recruiting.lifecycle_execution import schedule_lifecycle,execute_action
        from recruiting.models import LifecycleAction
        schedule_lifecycle(self.now,dry_run=False); action=LifecycleAction.objects.get()
        def stale_sender(*args,**kwargs):
            LifecycleAction.objects.filter(pk=action.pk).update(state='done')
            raise OSError('Old sender fails after another attempt settles')
        with patch('recruiting.lifecycle_execution.send_mail',side_effect=stale_sender): execute_action(action.pk,self.now)
        action.refresh_from_db(); self.assertEqual(action.state,'done')

    def test_tombstone_replay_reports_failed_deletion(self):
        from recruiting.lifecycle_execution import replay_tombstones
        with patch('recruiting.lifecycle_execution.execute_action',return_value='failed'):
            with self.assertRaises(ValueError): replay_tombstones([self.profile.account_key],self.now)
