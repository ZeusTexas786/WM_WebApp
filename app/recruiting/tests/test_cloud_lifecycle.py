"""Isolated private-VPS lifecycle drills; no live mail or provider operations."""
import base64
import io
import os
import tempfile
import zipfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from recruiting.models import Candidate, DeletionTombstone, LifecycleAction, Profile
from recruiting.cloud.models import CloudBatch, CloudOutbox


class PrivateLifecycleTests(TransactionTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[3]/'.local')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = override_settings(PRIVATE_ROOT=self.root, AUTOMATIC_WORKFLOW_ENABLED=True,
            AUTOMATIC_TRANSPORT='fixtures', AUTOMATIC_MIN_FREE_BYTES=0,
            LOCAL_DEVELOPMENT=False, LIFECYCLE_EXECUTION_ENABLED=True,
            ARCHIVE_KEY=base64.urlsafe_b64encode(b'z'*32).decode())
        self.config.enable(); self.addCleanup(self.config.disable)
        self.user = User.objects.create_user('lifecycle-cloud', email='owner@example.test')
        self.profile = Profile.objects.create(user=self.user, verified_at=timezone.now())
        self.client.force_login(self.user)
        result = self.client.post('/batches/sample/', data='{}', content_type='application/json')
        self.assertEqual(result.status_code, 201)
        self.batch = CloudBatch.objects.get(pk=result.json()['batch_id'])

    def finish(self):
        from recruiting.cloud.dispatch import dispatch_pending
        self.assertEqual(dispatch_pending(), 1)
        self.assertEqual(dispatch_pending(), 1)

    def test_cancel_reactivate_fences_claimed_receipt_and_credit_reservations(self):
        from recruiting.cloud.dispatch import _claim, _publish, _apply_receipt
        event, attempt = _claim(self.batch.outbox.get(kind='dispatch').pk)
        receipt = _publish(event, attempt)
        self.client.post('/account/cancel/')
        self.client.post('/account/reactivate/')
        self.profile.refresh_from_db(); self.batch.refresh_from_db()
        self.assertEqual(self.profile.account_epoch, 3)
        self.assertEqual(self.batch.stage, 'cancelled')
        self.assertEqual(self.batch.reserved_credits, 0)
        self.assertFalse(_apply_receipt(event, attempt, receipt))
        self.assertEqual(Candidate.objects.count(), 0)
        self.assertEqual(self.profile.completed_credits, 0)
        self.assertFalse(CloudOutbox.objects.filter(batch=self.batch, state__in=['running','pending']).exists())

    def test_export_has_owned_results_and_canonical_sources_and_named_cleanup(self):
        self.finish()
        result = self.client.get('/account/export/')
        self.addCleanup(result.close)
        self.assertEqual(result.status_code, 200)
        name = next((self.root/'.staging'/str(self.profile.account_key)).glob('export-*'))
        self.assertTrue(name.is_relative_to(self.root/'.staging'/str(self.profile.account_key)))
        self.assertTrue(name.exists())
        raw = b''.join(result.streaming_content)
        with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
            self.assertIn('cloud.json', bundle.namelist())
            self.assertEqual(len([n for n in bundle.namelist() if n.startswith('documents/')]), 3)
            self.assertEqual(len([n for n in bundle.namelist() if n.endswith('/receipt.json')]), 2)
        result.close()
        self.assertFalse(name.exists())

    def test_export_does_not_download_quarantined_customer_bytes(self):
        item = self.batch.inputs.first()
        (self.root/item.storage_key).write_bytes(b'quarantined customer content')
        result = self.client.get('/account/export/')
        self.assertEqual(result.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(b''.join(result.streaming_content))) as bundle:
            self.assertFalse(any(n.startswith('documents/') for n in bundle.namelist()))
            self.assertNotIn(b'quarantined customer content', b''.join(bundle.read(n) for n in bundle.namelist()))
        result.close()

    def test_export_pending_fictional_sources_are_available_without_report_projection(self):
        result=self.client.get('/account/export/')
        self.addCleanup(result.close)
        self.assertEqual(result.status_code,200)
        with zipfile.ZipFile(io.BytesIO(b''.join(result.streaming_content))) as bundle:
            self.assertEqual(len([name for name in bundle.namelist() if name.startswith('documents/')]),3)

    def test_archive_restore_covers_sources_receipts_partials_without_restarting_work(self):
        from recruiting.archives import archive_account, restore_account, archive_path
        from recruiting.lifecycle_execution import fence_work
        self.finish()
        orphan = self.root/'cloud'/str(self.profile.account_key)/'orphan'/'unfinished.part'
        orphan.parent.mkdir(); orphan.write_bytes(b'opaque partial')
        expected = {p.relative_to(self.root).as_posix():p.read_bytes() for p in (self.root/'cloud').rglob('*') if p.is_file()}
        self.profile.status = 'archiving'; self.profile.save(update_fields=['status'])
        fence_work(self.profile)
        archive_account(self.profile.pk)
        self.profile.refresh_from_db()
        self.assertFalse((self.root/'cloud'/str(self.profile.account_key)).exists())
        self.assertNotIn(b'opaque partial', archive_path(self.profile).read_bytes())
        restore_account(self.profile.pk)
        self.profile.refresh_from_db(); self.batch.refresh_from_db()
        self.assertEqual(self.profile.completed_credits, 3)
        self.assertEqual(self.batch.settled_credits, 3)
        self.assertEqual(self.batch.stage, 'completed')
        for key, value in expected.items(): self.assertEqual((self.root/key).read_bytes(), value)
        self.assertFalse(CloudOutbox.objects.filter(state__in=['pending','running']).exists())

    def test_delete_removes_all_account_cloud_bytes_and_preserves_another_account(self):
        from recruiting.lifecycle_execution import schedule_lifecycle, execute_action
        owned = self.root/'cloud'/str(self.profile.account_key)
        other = self.root/'cloud'/'a866479d-6933-4579-9b09-662dc49ed223'
        other.mkdir(); (other/'keep').write_bytes(b'other owner')
        orphan = owned/'orphan.part'; orphan.write_bytes(b'partial')
        Profile.objects.filter(pk=self.profile.pk).update(cancelled_at=timezone.now()-timedelta(days=4))
        schedule_lifecycle(dry_run=False)
        self.assertEqual(execute_action(LifecycleAction.objects.get().pk), 'done')
        self.assertFalse(owned.exists())
        self.assertEqual((other/'keep').read_bytes(), b'other owner')
        self.assertTrue(DeletionTombstone.objects.filter(account_key=self.profile.account_key).exists())

    def test_delete_failure_keeps_account_and_storage_declarations_for_retry(self):
        from recruiting.lifecycle_execution import schedule_lifecycle, execute_action
        owned = self.root/'cloud'/str(self.profile.account_key)
        Profile.objects.filter(pk=self.profile.pk).update(cancelled_at=timezone.now()-timedelta(days=4))
        schedule_lifecycle(dry_run=False)
        action = LifecycleAction.objects.get()
        import shutil
        real = shutil.rmtree
        def deny(target, *args, **kwargs):
            if Path(target) == owned: raise OSError('synthetic locked file')
            return real(target, *args, **kwargs)
        with patch('shutil.rmtree', side_effect=deny):
            self.assertEqual(execute_action(action.pk), 'failed')
        self.assertTrue(Profile.objects.filter(pk=self.profile.pk).exists())
        self.assertTrue(self.batch.inputs.exists())
        self.assertTrue(owned.exists())
        self.assertEqual(execute_action(action.pk), 'done')

    def test_deletion_is_not_limited_by_snapshot_manifest_budget(self):
        from recruiting.lifecycle_execution import schedule_lifecycle,execute_action
        owned=self.root/'cloud'/str(self.profile.account_key)
        Profile.objects.filter(pk=self.profile.pk).update(cancelled_at=timezone.now()-timedelta(days=4))
        schedule_lifecycle(dry_run=False)
        # Real private sources exceed these archive-only budgets. Erasure must
        # still validate and remove them rather than permanently retaining data.
        with patch('recruiting.cloud.lifecycle.MAX_FILES',1),patch('recruiting.cloud.lifecycle.MAX_SNAPSHOT_BYTES',1):
            self.assertEqual(execute_action(LifecycleAction.objects.get().pk),'done')
        self.assertFalse(owned.exists())
        self.assertFalse(Profile.objects.filter(pk=self.profile.pk).exists())

    def test_tombstone_blocks_old_archive_restore_before_writing_bytes(self):
        from recruiting.archives import archive_account, restore_account
        self.finish()
        self.profile.status='archiving'; self.profile.save(update_fields=['status'])
        archive_account(self.profile.pk)
        DeletionTombstone.objects.create(account_key=self.profile.account_key)
        with self.assertRaises(ValueError): restore_account(self.profile.pk)
        self.assertFalse((self.root/'cloud'/str(self.profile.account_key)).exists())

    def test_tombstoned_restored_profile_cannot_read_authenticated_reports(self):
        self.finish()
        candidate=Candidate.objects.first()
        DeletionTombstone.objects.create(account_key=self.profile.account_key)
        for path in ('/dashboard/', f'/candidates/{candidate.pk}/', f'/batches/{self.batch.pk}/status/'):
            with self.subTest(path=path): self.assertEqual(self.client.get(path).status_code, 409)

    def test_export_rejects_foreign_result_path_without_leaking_bytes(self):
        self.finish()
        other=self.root/'cloud'/'a866479d-6933-4579-9b09-662dc49ed223'/'secret.json'
        other.parent.mkdir(); other.write_bytes(b'foreign customer secret')
        ref=self.batch.result_references.first()
        ref.storage_key=other.relative_to(self.root).as_posix(); ref.save(update_fields=['storage_key'])
        response=self.client.get('/account/export/')
        self.assertEqual(response.status_code,409)
        self.assertNotIn(b'foreign customer secret',response.content)
        self.assertFalse(list((self.root/'.staging'/str(self.profile.account_key)).glob('export-*')))

    def test_export_budget_rejection_cleans_scratch_and_preserves_sources(self):
        self.finish()
        from recruiting.cloud.storage import _private_footprint
        before=_private_footprint()
        with override_settings(AUTOMATIC_PROJECT_STORAGE_BYTES=before+10):
            response=self.client.get('/account/export/')
        self.assertEqual(response.status_code,409)
        self.assertEqual(_private_footprint(),before)
        self.assertTrue(all((self.root/item.storage_key).exists() for item in self.batch.inputs.all()))

    def test_export_output_bound_cleans_failed_response(self):
        with patch('recruiting.account_controls.EXPORT_LIMIT',256,create=True):
            result=self.client.get('/account/export/')
        self.addCleanup(result.close)
        self.assertEqual(result.status_code,409)
        self.assertFalse(list((self.root/'.staging'/str(self.profile.account_key)).glob('export-*')))

    def test_export_close_unlink_failure_keeps_physical_bytes_counted(self):
        from recruiting.cloud.storage import _private_footprint
        before=_private_footprint()
        response=self.client.get('/account/export/')
        self.addCleanup(response.close)
        self.assertEqual(response.status_code,200)
        name=next((self.root/'.staging'/str(self.profile.account_key)).glob('export-*'))
        size=name.stat().st_size
        unlink=Path.unlink
        def deny(path,*args,**kwargs):
            if path==name: raise OSError('synthetic locked scratch')
            return unlink(path,*args,**kwargs)
        with patch.object(Path,'unlink',deny): response.close()
        self.assertTrue(name.exists())
        self.assertEqual(_private_footprint(),before+size)
        name.unlink()

    def test_archive_waits_for_streaming_export_before_removing_plaintext(self):
        from recruiting.archives import archive_account
        response=self.client.get('/account/export/')
        self.addCleanup(response.close)
        self.assertEqual(response.status_code,200)
        Profile.objects.filter(pk=self.profile.pk).update(status='archiving')
        with self.assertRaises(ValueError): archive_account(self.profile.pk)
        self.assertTrue((self.root/'cloud'/str(self.profile.account_key)).exists())
        response.close()
        archive_account(self.profile.pk)
        self.profile.refresh_from_db(); self.assertEqual(self.profile.status,'archived')

    def test_deletion_waits_for_streaming_export_then_retries_without_anonymous_bytes(self):
        from recruiting.lifecycle_execution import schedule_lifecycle,execute_action
        response=self.client.get('/account/export/')
        self.addCleanup(response.close)
        self.assertEqual(response.status_code,200)
        name=next((self.root/'.staging'/str(self.profile.account_key)).glob('export-*'))
        Profile.objects.filter(pk=self.profile.pk).update(cancelled_at=timezone.now()-timedelta(days=4))
        schedule_lifecycle(dry_run=False); action=LifecycleAction.objects.get()
        self.assertEqual(execute_action(action.pk),'failed')
        self.assertTrue(name.exists())
        self.assertTrue((self.root/'cloud'/str(self.profile.account_key)).exists())
        self.assertTrue(Profile.objects.filter(pk=self.profile.pk).exists())
        response.close()
        self.assertEqual(execute_action(action.pk),'done')
        self.assertFalse(name.exists())

    def test_cloud_tree_symlink_rejected_without_touching_foreign_target(self):
        from recruiting.archives import archive_account
        foreign=self.root/'foreign'; foreign.mkdir(); (foreign/'keep').write_bytes(b'keep')
        link=self.root/'cloud'/str(self.profile.account_key)/'foreign-link'
        try: os.symlink(foreign,link,target_is_directory=True)
        except OSError: self.skipTest('Windows user cannot create symbolic links.')
        self.profile.status='archiving'; self.profile.save(update_fields=['status'])
        with self.assertRaises(ValueError): archive_account(self.profile.pk)
        self.assertEqual((foreign/'keep').read_bytes(),b'keep')

    def test_restore_failure_preserves_encrypted_snapshot_and_archived_state(self):
        from recruiting.archives import archive_account,restore_account,archive_path
        self.finish()
        self.profile.status='archiving'; self.profile.save(update_fields=['status'])
        archive_account(self.profile.pk); self.profile.refresh_from_db()
        snapshot=archive_path(self.profile); sealed=snapshot.read_bytes()
        replace=os.replace
        def deny(source,target):
            if Path(target).is_relative_to(self.root/'cloud'): raise OSError('synthetic restore write failure')
            return replace(source,target)
        with patch('os.replace',side_effect=deny):
            with self.assertRaises(OSError): restore_account(self.profile.pk)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.status,'archived')
        self.assertEqual(snapshot.read_bytes(),sealed)
        restore_account(self.profile.pk)
        self.profile.refresh_from_db(); self.assertEqual(self.profile.status,'active')
        self.assertEqual(self.profile.completed_credits,3)

    def test_archive_of_pending_work_restores_cancelled_without_old_receipt_acceptance(self):
        from recruiting.archives import archive_account,restore_account
        from recruiting.cloud.dispatch import _claim,_publish,_apply_receipt,dispatch_pending
        event,attempt=_claim(self.batch.outbox.get(kind='dispatch').pk)
        receipt=_publish(event,attempt)
        self.profile.status='archiving'; self.profile.save(update_fields=['status'])
        archive_account(self.profile.pk)
        restore_account(self.profile.pk)
        self.batch.refresh_from_db(); self.profile.refresh_from_db()
        self.assertEqual(self.batch.stage,'cancelled')
        self.assertFalse(_apply_receipt(event,attempt,receipt))
        self.assertEqual(dispatch_pending(),0)
        self.assertEqual(self.profile.completed_credits,0)

    def test_old_database_tombstone_replay_removes_snapshot_cloud_and_work(self):
        from recruiting.archives import archive_account,archive_path
        from recruiting.lifecycle_execution import replay_tombstones
        self.finish()
        self.profile.status='archiving'; self.profile.save(update_fields=['status'])
        archive_account(self.profile.pk)
        self.profile.refresh_from_db(); snapshot=archive_path(self.profile)
        # Model an old DB/backup reintroducing a previously erased account.
        Profile.objects.filter(pk=self.profile.pk).update(status='active')
        old=self.root/'cloud'/str(self.profile.account_key)/'old-receipt.part'
        old.parent.mkdir(parents=True); old.write_bytes(b'old backup bytes')
        self.assertEqual(replay_tombstones([self.profile.account_key]),{'status':'complete'})
        self.assertFalse(snapshot.exists()); self.assertFalse(old.exists())
        self.assertFalse(Profile.objects.filter(pk=self.profile.pk).exists())
        self.assertFalse(CloudBatch.objects.filter(pk=self.batch.pk).exists())
        self.assertEqual(replay_tombstones([self.profile.account_key]),{'status':'complete'})
        self.assertEqual(DeletionTombstone.objects.filter(account_key=self.profile.account_key).count(),1)
