"""Zero-spend dispatch, durable publication, fencing and owner-only reports."""
import io
import hashlib
import json
import os
import stat
from types import SimpleNamespace
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from importlib import import_module
from pathlib import Path
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import close_old_connections, connection, IntegrityError
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from recruiting.models import Candidate, Job, Profile, ServiceHeartbeat
from recruiting.cloud.models import CloudBatch, CloudOutbox, StageAttempt


class DispatchTests(TransactionTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[3]/'.local')
        self.addCleanup(self.tmp.cleanup)
        self.config = override_settings(PRIVATE_ROOT=Path(self.tmp.name),
            AUTOMATIC_WORKFLOW_ENABLED=True, AUTOMATIC_TRANSPORT='fixtures',
            AUTOMATIC_MIN_FREE_BYTES=0, EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
        self.config.enable(); self.addCleanup(self.config.disable)
        self.user = User.objects.create_user('dispatch-owner', email='owner@example.test')
        Profile.objects.create(user=self.user, verified_at=timezone.now())
        self.client.force_login(self.user)
        response = self.client.post('/batches/sample/', data='{}', content_type='application/json')
        self.assertEqual(response.status_code, 201)
        self.batch = CloudBatch.objects.get(pk=response.json()['batch_id'])
        try:
            self.dispatch = import_module('recruiting.cloud.dispatch')
        except ModuleNotFoundError:
            self.fail('Bounded fixture dispatch is not implemented.')

    def finish(self):
        self.assertEqual(self.dispatch.dispatch_pending(), 1)
        self.assertEqual(self.dispatch.dispatch_pending(), 1)
        self.batch.refresh_from_db()

    def test_one_stage_per_tick_publishes_grounded_private_results_once(self):
        self.assertEqual(self.dispatch.dispatch_pending(), 1)
        self.batch.refresh_from_db(); self.assertEqual(self.batch.stage, 'analyze')
        self.assertEqual(Candidate.objects.count(), 0)
        self.assertEqual(self.dispatch.dispatch_pending(), 1)
        self.batch.refresh_from_db(); self.assertEqual(self.batch.stage, 'completed')
        self.assertEqual(self.batch.settled_credits, 3)
        self.assertEqual(Profile.objects.get(user=self.user).completed_credits, 3)
        self.assertEqual(Candidate.objects.count(), 3)
        self.assertEqual(Job.objects.filter(status='completed', reserved=False).count(), 3)
        self.assertEqual(sorted(j.result['alignment'] for j in Job.objects.all()), [18.75, 50, 100])
        for ref in self.batch.result_references.select_related('candidate','job'):
            self.assertEqual(ref.candidate.session.owner_id, self.user.pk)
            self.assertEqual(ref.candidate.scan_status, 'trusted_fixture')
            self.assertTrue(ref.job.interviews['sample'])
            artifact = json.loads((Path(self.tmp.name)/ref.storage_key).read_bytes())
            self.assertTrue(artifact['synthetic'])
            self.assertEqual(artifact['input_sha256'], ref.input_sha256)
        self.assertEqual(self.dispatch.dispatch_pending(), 0)
        self.assertEqual(StageAttempt.objects.count(), 2)
        self.assertEqual(self.batch.result_references.count(), 3)

    def test_changed_bytes_rubric_and_catalog_reject_without_candidates_or_credits(self):
        item = self.batch.inputs.first()
        (Path(self.tmp.name)/item.storage_key).write_bytes(b'changed source')
        self.dispatch.dispatch_pending()
        self.batch.refresh_from_db(); self.assertEqual(self.batch.stage, 'failed')
        self.assertEqual(Candidate.objects.count(), 0)
        self.assertEqual(Profile.objects.get(user=self.user).completed_credits, 0)

    def test_changed_role_rejects_before_synthetic_output(self):
        self.batch.session.jd += ' altered'
        self.batch.session.save(update_fields=['jd'])
        self.dispatch.dispatch_pending()
        self.batch.refresh_from_db(); self.assertEqual(self.batch.stage, 'failed')
        self.assertFalse(self.batch.result_references.exists())

    def test_changed_catalog_and_rubric_reject_before_synthetic_results(self):
        item=self.batch.inputs.first()
        item.filename='forged.txt'; item.save(update_fields=['filename'])
        self.dispatch.dispatch_pending()
        self.batch.refresh_from_db(); self.assertEqual(self.batch.stage,'failed')
        self.assertEqual(Candidate.objects.count(),0)

    def test_changed_rubric_weight_rejects_before_synthetic_results(self):
        session=self.batch.session
        session.rubric[0]['weight']=10; session.save(update_fields=['rubric'])
        self.dispatch.dispatch_pending()
        self.batch.refresh_from_db(); self.assertEqual(self.batch.stage,'failed')
        self.assertEqual(Candidate.objects.count(),0)

    def test_mutated_rubric_version_after_receipt_rejects_completion(self):
        self.dispatch.dispatch_pending()
        original=self.dispatch._apply_receipt
        def change(*args):
            session=self.batch.session
            session.rubric_version+=1; session.save(update_fields=['rubric_version'])
            return original(*args)
        with patch.object(self.dispatch,'_apply_receipt',side_effect=change):
            self.dispatch.dispatch_pending()
        self.assertEqual(Candidate.objects.count(),0)
        self.assertEqual(Profile.objects.get(user=self.user).completed_credits,0)

    def test_live_or_disabled_transport_cannot_forge_fixture_scan_attestation(self):
        from recruiting.cloud import state
        attempt=state.claim_stage(self.batch.pk)
        payload={**self.dispatch._fence(self.batch,attempt), 'synthetic':True,
            'validation_method':'canonical_fixture', 'expanded_inputs':[
                {'source_id':str(item.pk),'filename':item.filename,'byte_size':item.byte_size,
                 'sha256':item.sha256,'scan_status':'trusted_fixture'} for item in self.batch.inputs.order_by('client_key')]}
        self.batch.session.mode='live'; self.batch.session.save(update_fields=['mode'])
        self.assertFalse(state.apply_transition(self.batch.pk,attempt.pk,'validate',payload))
        self.batch.refresh_from_db(); self.assertEqual(self.batch.stage,'failed')
        self.assertEqual(Profile.objects.get(user=self.user).completed_credits,0)

    def test_disabled_transport_cannot_forge_fixture_scan_attestation(self):
        from recruiting.cloud import state
        attempt=state.claim_stage(self.batch.pk)
        payload={**self.dispatch._fence(self.batch,attempt), 'synthetic':True,
            'validation_method':'canonical_fixture', 'expanded_inputs':[
                {'source_id':str(item.pk),'filename':item.filename,'byte_size':item.byte_size,
                 'sha256':item.sha256,'scan_status':'trusted_fixture'} for item in self.batch.inputs.order_by('client_key')]}
        with override_settings(AUTOMATIC_TRANSPORT='remote'):
            self.assertFalse(state.apply_transition(self.batch.pk,attempt.pk,'validate',payload))
        self.batch.refresh_from_db(); self.assertEqual(self.batch.stage,'failed')

    def test_disabled_unknown_and_remote_never_execute(self):
        for transport in ['disabled', 'unknown', 'remote']:
            with override_settings(AUTOMATIC_TRANSPORT=transport):
                self.assertEqual(self.dispatch.dispatch_pending(), 0)
        with override_settings(AUTOMATIC_WORKFLOW_ENABLED=False):
            self.assertEqual(self.dispatch.dispatch_pending(), 0)
        self.assertEqual(StageAttempt.objects.count(), 0)

    def test_receipt_survives_database_failure_and_reconciles_without_second_evaluation(self):
        self.dispatch.dispatch_pending()
        with patch.object(self.dispatch, '_apply_receipt', side_effect=RuntimeError('simulated database outage')):
            self.dispatch.dispatch_pending()
        self.assertEqual(Candidate.objects.count(), 0)
        self.assertEqual(Profile.objects.get(user=self.user).completed_credits, 0)
        self.assertTrue(list(Path(self.tmp.name).rglob('receipt.json')))
        CloudOutbox.objects.filter(kind='dispatch', stage='analyze').update(lease_until=timezone.now()-timedelta(seconds=1))
        StageAttempt.objects.filter(stage='analyze').update(lease_until=timezone.now()-timedelta(seconds=1))
        with patch('recruiting.cloud.fixtures.evaluate_fixture', side_effect=AssertionError('must reconcile receipt')):
            self.dispatch.dispatch_pending()
        self.batch.refresh_from_db(); self.assertEqual(self.batch.stage, 'completed')
        self.assertEqual(Candidate.objects.count(), 3)
        self.assertEqual(Profile.objects.get(user=self.user).completed_credits, 3)

    def test_cancelled_epoch_and_version_fence_never_publish_candidates(self):
        self.dispatch.dispatch_pending()
        original = self.dispatch._apply_receipt
        def cancel(*args, **kwargs):
            Profile.objects.filter(user=self.user).update(account_epoch=2)
            return original(*args, **kwargs)
        with patch.object(self.dispatch, '_apply_receipt', side_effect=cancel):
            self.dispatch.dispatch_pending()
        self.assertEqual(Candidate.objects.count(), 0)
        self.assertEqual(Profile.objects.get(user=self.user).completed_credits, 0)

    def test_stale_outbox_token_cannot_complete_published_attempt(self):
        self.dispatch.dispatch_pending()
        original=self.dispatch._apply_receipt
        def steal(event,*args):
            CloudOutbox.objects.filter(pk=event.pk).update(lease_token='new-dispatcher-token')
            return original(event,*args)
        with patch.object(self.dispatch,'_apply_receipt',side_effect=steal):
            self.dispatch.dispatch_pending()
        self.assertEqual(Candidate.objects.count(),0)
        self.assertEqual(Profile.objects.get(user=self.user).completed_credits,0)
        self.batch.refresh_from_db(); self.assertEqual(self.batch.stage,'analyze')

    def test_report_projection_failure_rolls_back_credit_and_reference_then_reconciles(self):
        self.dispatch.dispatch_pending()
        with patch.object(Job.objects,'create',side_effect=IntegrityError('simulated projection failure')):
            self.dispatch.dispatch_pending()
        self.batch.refresh_from_db(); self.assertEqual(self.batch.stage,'analyze')
        self.assertEqual(Candidate.objects.count(),0)
        self.assertFalse(self.batch.result_references.exists())
        self.assertEqual(Profile.objects.get(user=self.user).completed_credits,0)
        CloudOutbox.objects.filter(kind='dispatch',stage='analyze').update(lease_until=timezone.now()-timedelta(seconds=1))
        self.dispatch.dispatch_pending()
        self.batch.refresh_from_db(); self.assertEqual(self.batch.stage,'completed')
        self.assertEqual(Candidate.objects.count(),3)
        self.assertEqual(Profile.objects.get(user=self.user).completed_credits,3)

    def test_artifact_generation_substitution_and_ungrounded_evidence_reject(self):
        from recruiting.cloud.artifacts import encode
        self.dispatch.dispatch_pending()
        original=self.dispatch._apply_receipt
        def forge(event,attempt,receipt):
            ref=receipt['artifacts'][0]
            path=Path(self.tmp.name)/ref['storage_key']
            result=json.loads(path.read_bytes())
            result['assessment']['evaluations'][0]['evidence'][0]['quote']='Invented evidence'
            data=encode(result); path.write_bytes(data)
            ref['sha256']=hashlib.sha256(data).hexdigest()
            from recruiting.cloud.artifacts import AttemptArtifacts
            AttemptArtifacts(event.batch,attempt)._path('receipt.json').write_bytes(encode(receipt))
            return original(event,attempt,receipt)
        with patch.object(self.dispatch,'_apply_receipt',side_effect=forge):
            self.dispatch.dispatch_pending()
        self.batch.refresh_from_db(); self.assertEqual(self.batch.stage,'failed')
        self.assertEqual(Candidate.objects.count(),0)
        self.assertEqual(Profile.objects.get(user=self.user).completed_credits,0)

    def test_result_reference_cannot_substitute_another_attempt_or_url(self):
        from recruiting.cloud.artifacts import AttemptArtifacts, encode
        self.dispatch.dispatch_pending()
        original=self.dispatch._apply_receipt
        def substitute(event,attempt,receipt):
            receipt['artifacts'][0]['storage_key']='https://forged.invalid/result.json'
            AttemptArtifacts(event.batch,attempt)._path('receipt.json').write_bytes(encode(receipt))
            return original(event,attempt,receipt)
        with patch.object(self.dispatch,'_apply_receipt',side_effect=substitute):
            self.dispatch.dispatch_pending()
        self.batch.refresh_from_db(); self.assertEqual(self.batch.stage,'failed')
        self.assertEqual(Candidate.objects.count(),0)

    def test_interrupted_before_receipt_retries_same_attempt_without_duplicate_results(self):
        from recruiting.cloud.artifacts import AttemptArtifacts
        self.dispatch.dispatch_pending()
        publish=AttemptArtifacts.publish
        def interrupt(artifacts,name,value):
            if name=='receipt.json': raise OSError('simulated disk interruption')
            return publish(artifacts,name,value)
        with patch.object(AttemptArtifacts,'publish',interrupt):
            self.dispatch.dispatch_pending()
        self.assertEqual(Candidate.objects.count(),0)
        CloudOutbox.objects.filter(kind='dispatch',stage='analyze').update(lease_until=timezone.now()-timedelta(seconds=1))
        self.dispatch.dispatch_pending()
        self.assertEqual(StageAttempt.objects.filter(stage='analyze').count(),1)
        self.assertEqual(Candidate.objects.count(),3)
        self.assertEqual(Profile.objects.get(user=self.user).completed_credits,3)

    def test_missing_receipt_does_not_renew_expired_attempt_or_make_result_visible(self):
        from recruiting.cloud import state
        attempt=state.claim_stage(self.batch.pk)
        StageAttempt.objects.filter(pk=attempt.pk).update(lease_until=timezone.now()-timedelta(seconds=1))
        self.dispatch.dispatch_pending()
        self.batch.refresh_from_db(); self.assertEqual(self.batch.stage,'expired')
        self.assertEqual(Candidate.objects.count(),0)

    def test_completion_requires_the_exact_durable_receipt_not_an_in_memory_result(self):
        publish=self.dispatch._publish
        def lose(event,attempt):
            receipt=publish(event,attempt)
            from recruiting.cloud.artifacts import AttemptArtifacts
            AttemptArtifacts(event.batch,attempt)._path('receipt.json').unlink()
            return receipt
        with patch.object(self.dispatch,'_publish',side_effect=lose):
            self.dispatch.dispatch_pending()
        self.batch.refresh_from_db(); self.assertEqual(self.batch.stage,'validate')
        self.assertEqual(Candidate.objects.count(),0)

    def test_tick_bounds_and_missing_smtp_hold_notices(self):
        for limit in [0,11,True]:
            with self.assertRaises(ValueError): self.dispatch.dispatch_pending(limit)
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError): call_command('cloud_dispatcher',once=True,sleep=0)
        with override_settings(EMAIL_BACKEND='django.core.mail.backends.smtp.EmailBackend',EMAIL_HOST=''):
            call_command('cloud_dispatcher',once=True,stdout=io.StringIO())
        self.assertEqual(CloudOutbox.objects.get(event_key='notification:acknowledgement').state,'pending')

    def test_fixture_execution_never_calls_provider_research_parser_or_scanner(self):
        from contextlib import ExitStack
        targets=['recruiting.providers.request_json','recruiting.providers.analyze_candidate',
            'recruiting.interviews.fetch_documentation','recruiting.interviews.generate_interview_pack',
            'recruiting.parser.parse_bounded','recruiting.parser.parse_remote','recruiting.scanning.scan_path']
        with ExitStack() as stack:
            for target in targets: stack.enter_context(patch(target,side_effect=AssertionError('outbound/real processing forbidden')))
            self.finish()
        self.assertEqual(self.batch.stage,'completed')

    def test_artifact_scope_immutability_bound_and_single_link_enforced(self):
        from recruiting.cloud import state
        from recruiting.cloud.artifacts import AttemptArtifacts
        from recruiting.cloud.storage import storage_admission
        attempt=state.claim_stage(self.batch.pk)
        artifacts=AttemptArtifacts(self.batch,attempt)
        with storage_admission():
            for name in ['../receipt.json','https://forged.invalid/x','brief-9.json']:
                with self.assertRaises(ValueError): artifacts.publish(name,{})
            with self.assertRaises(ValueError): artifacts.publish('receipt.json',{'data':'x'*131072})
            artifacts.publish('receipt.json',{'immutable':True})
            with self.assertRaises(ValueError): artifacts.publish('receipt.json',{'immutable':False})
            with override_settings(AUTOMATIC_PROJECT_STORAGE_BYTES=1):
                with self.assertRaises(ValueError): artifacts.publish('brief-0.json',{})
        path=artifacts._path('receipt.json')
        link=Path(self.tmp.name)/'hardlink'
        try:
            os.link(path,link)
        except PermissionError:
            # Restricted Windows tokens forbid creating hardlinks. Exercise the
            # OS descriptor boundary while still opening the real private file.
            info=SimpleNamespace(st_mode=stat.S_IFREG|0o600,st_nlink=2,st_size=path.stat().st_size)
            with patch('recruiting.cloud.artifacts.os.fstat',return_value=info):
                with self.assertRaises(ValueError): artifacts.read('receipt.json')
        else:
            with self.assertRaises(ValueError): artifacts.read('receipt.json')
            link.unlink()
        self.assertEqual(artifacts.read('receipt.json'),{'immutable':True})

    def test_storage_failure_backs_off_without_duplicate_claim_or_candidate(self):
        with patch.object(self.dispatch,'_publish',side_effect=OSError('simulated unavailable disk')):
            self.assertEqual(self.dispatch.dispatch_pending(),0)
        event=CloudOutbox.objects.get(kind='dispatch',stage='validate')
        self.assertEqual(event.state,'pending')
        self.assertGreater(event.lease_until,timezone.now())
        self.assertEqual(event.attempts,1)
        self.assertEqual(self.dispatch.dispatch_pending(),0)
        self.assertEqual(StageAttempt.objects.count(),1)
        self.assertEqual(Candidate.objects.count(),0)

    def test_repeated_publisher_reuses_durable_receipt_without_evaluating_again(self):
        from recruiting.cloud.artifacts import AttemptArtifacts
        self.dispatch.dispatch_pending()
        event=CloudOutbox.objects.get(kind='dispatch',stage='analyze')
        event,attempt=self.dispatch._claim(event.pk)
        artifacts=AttemptArtifacts(self.batch,attempt)
        with self.assertRaises(FileNotFoundError): artifacts.read('receipt.json')
        receipt=self.dispatch._publish(event,attempt)
        CloudOutbox.objects.filter(pk=event.pk).update(lease_until=timezone.now()-timedelta(seconds=1))
        successor,reclaimed=self.dispatch._claim(event.pk)
        with patch('recruiting.cloud.fixtures.evaluate_fixture',side_effect=AssertionError('duplicate evaluation')):
            replay=self.dispatch._publish(successor,reclaimed)
            self.assertEqual(self.dispatch._publish(successor,reclaimed),receipt)
        self.assertEqual(replay,receipt)
        self.assertFalse(self.dispatch._apply_receipt(event,attempt,receipt))
        self.assertTrue(self.dispatch._apply_receipt(successor,reclaimed,replay))
        self.assertFalse(self.dispatch._apply_receipt(event,attempt,receipt))
        self.assertEqual(Candidate.objects.count(),3)
        self.assertEqual(Profile.objects.get(user=self.user).completed_credits,3)

    def test_lease_overlap_old_publisher_cannot_publish_or_fail_successor(self):
        from recruiting.cloud.artifacts import AttemptArtifacts
        self.dispatch.dispatch_pending()
        old,attempt=self.dispatch._claim(CloudOutbox.objects.get(kind='dispatch',stage='analyze').pk)
        artifacts=AttemptArtifacts(self.batch,attempt)
        with self.assertRaises(FileNotFoundError): artifacts.read('receipt.json')
        CloudOutbox.objects.filter(pk=old.pk).update(lease_until=timezone.now()-timedelta(seconds=1))
        successor,reclaimed=self.dispatch._claim(old.pk)
        self.assertEqual(reclaimed.pk,attempt.pk)
        with self.assertRaises(FileNotFoundError): artifacts.read('receipt.json')
        self.assertIsNone(self.dispatch._publish(old,attempt))
        self.assertFalse(artifacts._path('receipt.json').exists())
        self.dispatch._fail(old,attempt)
        self.batch.refresh_from_db(); self.assertEqual(self.batch.stage,'analyze')
        receipt=self.dispatch._publish(successor,reclaimed)
        self.assertFalse(self.dispatch._apply_receipt(old,attempt,receipt))
        self.assertTrue(self.dispatch._apply_receipt(successor,reclaimed,receipt))
        self.assertEqual(Candidate.objects.count(),3)
        self.assertEqual(Profile.objects.get(user=self.user).completed_credits,3)

    def test_command_cleans_abandoned_draft_bytes(self):
        from recruiting.cloud import state
        from recruiting.cloud.storage import reserve_uploads, PrivateDiskStorage
        draft=state.create_batch(self.user.pk,self.batch.session_id,'files')
        content=b'unaccepted source'
        item=reserve_uploads(draft.pk,self.user.pk,[{'client_key':'draft','filename':'draft.txt',
            'role':'resume','byte_size':len(content),'sha256':hashlib.sha256(content).hexdigest()}])[0]
        disk=PrivateDiskStorage(self.user.pk)
        disk.write_chunk(item,0,io.BytesIO(content),len(content))
        disk.finalize_upload(item)
        CloudBatch.objects.filter(pk=draft.pk).update(expires_at=timezone.now()-timedelta(seconds=1))
        call_command('cloud_dispatcher',once=True,stdout=io.StringIO())
        self.assertFalse((Path(self.tmp.name)/item.storage_key).exists())
        self.assertFalse(draft.inputs.exists())

    def test_reports_pdf_and_evidence_require_owner_and_cloud_sources_never_download(self):
        self.finish(); candidate = Candidate.objects.first()
        for suffix in ['', 'evidence/', 'pdf/']:
            response = self.client.get(f'/candidates/{candidate.pk}/{suffix}')
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(f'/candidates/{candidate.pk}/download/').status_code, 403)
        stranger=User.objects.create_user('dispatch-stranger')
        Profile.objects.create(user=stranger, verified_at=timezone.now())
        self.client.force_login(stranger)
        for suffix in ['', 'evidence/', 'pdf/', 'download/']:
            self.assertEqual(self.client.get(f'/candidates/{candidate.pk}/{suffix}').status_code,404)

    def test_once_command_heartbeats_and_delivers_outside_stage_transactions(self):
        from recruiting.cloud.notifications import deliver_notifications
        def delivery(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            return deliver_notifications(*args, **kwargs)
        with patch('recruiting.cloud.notifications.deliver_notifications', side_effect=delivery):
            call_command('cloud_dispatcher', once=True, stdout=io.StringIO())
        self.batch.refresh_from_db(); self.assertEqual(self.batch.stage,'analyze')
        self.assertTrue(ServiceHeartbeat.objects.filter(service='cloud_dispatcher').exists())
        self.assertEqual(CloudOutbox.objects.get(event_key='notification:acknowledgement').state,'delivered')

    @skipUnless(connection.vendor=='postgresql', 'Real dispatcher contention requires PostgreSQL.')
    def test_two_dispatchers_only_claim_one_durable_stage(self):
        outcomes=self.race_dispatchers()
        self.assertEqual(sum(outcomes),1)
        self.assertEqual(StageAttempt.objects.count(),1)
        self.assertEqual(CloudOutbox.objects.filter(event_key='dispatch:validate',state='delivered').count(),1)

    @skipUnless(connection.vendor=='postgresql', 'Real dispatcher contention requires PostgreSQL.')
    def test_two_analysis_dispatchers_create_one_set_of_reports_and_credits(self):
        self.dispatch.dispatch_pending()
        outcomes=self.race_dispatchers()
        self.assertEqual(sum(outcomes),1)
        self.assertEqual(StageAttempt.objects.filter(stage='analyze').count(),1)
        self.assertEqual(Candidate.objects.count(),3)
        self.assertEqual(Job.objects.count(),3)
        self.assertEqual(Profile.objects.get(user=self.user).completed_credits,3)

    def race_dispatchers(self):
        gate=Barrier(2)
        pending=self.dispatch._pending_ids
        def pending_together(limit):
            ids=pending(limit); gate.wait(); return ids
        def worker():
            close_old_connections()
            try:
                return self.dispatch.dispatch_pending()
            finally: close_old_connections()
        with patch.object(self.dispatch,'_pending_ids',side_effect=pending_together):
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes=list(pool.map(lambda _:worker(), range(2)))
        return outcomes
