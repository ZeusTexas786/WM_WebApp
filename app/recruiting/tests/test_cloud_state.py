"""Durable admission and worker fencing; no parsers, SDKs, or paid calls."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from importlib import import_module
from threading import Barrier
from unittest import skipUnless
from django.contrib.auth.models import User
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from recruiting.models import Candidate, HiringSession, Job, Profile

MB = 1024 * 1024


class CloudFixtures:
    def setUp(self):
        try:
            self.state = import_module('recruiting.cloud.state')
            self.models = import_module('recruiting.cloud.models')
        except ModuleNotFoundError:
            self.fail('Durable cloud state contracts are missing.')
        self.user = User.objects.create_user('cloud-owner')
        self.profile = Profile.objects.create(user=self.user, verified_at=timezone.now())
        self.session = HiringSession.objects.create(owner=self.user, title='Role', jd='Python',
            rubric_version=1, rubric=[{'id': 'python', 'name': 'Python', 'weight': 1}], confirmed_at=timezone.now())

    def spec(self, index=0, role='resume', size=100):
        return {'client_key': str(index), 'filename': f'{index}.pdf', 'role': role,
                'byte_size': size, 'sha256': f'{index + 1:064x}'}

    def batch(self, mode='files'):
        return self.state.create_batch(self.user.pk, self.session.pk, mode)

    def reserve(self, batch, specs=None):
        return self.state.reserve_inputs(batch.pk, self.user.pk, specs or [self.spec()])

    def ready(self, mode='files', specs=None):
        batch = self.batch(mode)
        inputs = self.reserve(batch, specs)
        for item in inputs:
            self.state.confirm_upload(batch.pk, self.user.pk, item.pk, item.byte_size, item.sha256)
        return self.state.seal_batch(batch.pk, self.user.pk), inputs

    def envelope(self, batch, attempt, **fields):
        return {'account_key': str(batch.account_key), 'account_epoch': batch.account_epoch,
                'input_manifest_digest': batch.input_manifest_digest,
                'rubric_version': batch.rubric_version, 'lease_token': attempt.lease_token, **fields}

    def expanded(self, items):
        return [{'source_id': str(item.pk), 'filename': item.filename,
                 'byte_size': item.byte_size, 'sha256': item.sha256, 'scan_status': 'clean'} for item in items if item.role == 'resume']

    def analyzed(self):
        batch, items = self.ready()
        attempt = self.state.claim_stage(batch.pk)
        self.assertTrue(self.state.apply_transition(batch.pk, attempt.pk, 'validate',
            self.envelope(batch, attempt, expanded_inputs=self.expanded(items))))
        batch.refresh_from_db()
        return batch, self.state.claim_stage(batch.pk)

    def results(self, batch):
        return [{'input_sha256': batch.expanded_manifest[0]['sha256'],
                 'storage_key': f'cloud/{batch.account_key}/{batch.pk}/results/brief.json'}]

    def assert_no_work(self, completed=0):
        self.assertFalse(self.models.CloudOutbox.objects.exists())
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.completed_credits, completed)


class CloudStateTests(CloudFixtures, TestCase):
    def test_cross_account_cannot_create_or_reserve_or_seal(self):
        stranger = User.objects.create_user('stranger')
        Profile.objects.create(user=stranger, verified_at=timezone.now())
        with self.assertRaises(ValueError): self.state.create_batch(stranger.pk, self.session.pk, 'files')
        batch = self.batch()
        with self.assertRaises(ValueError): self.state.reserve_inputs(batch.pk, stranger.pk, [self.spec()])
        with self.assertRaises(ValueError): self.state.seal_batch(batch.pk, stranger.pk)
        self.assert_no_work()

    def test_missing_owner_never_uses_internal_worker_access(self):
        batch = self.batch(); item = self.reserve(batch)[0]
        with self.assertRaises(ValueError): self.state.reserve_inputs(batch.pk, None, [self.spec(1)])
        with self.assertRaises(ValueError): self.state.confirm_upload(batch.pk, None, item.pk, item.byte_size, item.sha256)
        self.state.confirm_upload(batch.pk, self.user.pk, item.pk, item.byte_size, item.sha256)
        with self.assertRaises(ValueError): self.state.seal_batch(batch.pk, None)
        self.assert_no_work()

    def test_duplicate_input_submission_is_idempotent(self):
        batch = self.batch()
        first = self.reserve(batch)
        self.assertEqual(self.reserve(batch)[0].pk, first[0].pk)
        batch.refresh_from_db()
        self.assertEqual(batch.reserved_credits, 1)
        with self.assertRaises(ValueError): self.reserve(batch, [self.spec(size=101)])
        self.assert_no_work()

    def test_31st_resume_rejected_atomically(self):
        batch = self.batch()
        self.reserve(batch, [self.spec(i) for i in range(30)])
        with self.assertRaises(ValueError): self.reserve(batch, [self.spec(30)])
        self.assertEqual(batch.inputs.count(), 30)
        self.assert_no_work()

    def test_10mb_plus_one_resume_rejected(self):
        with self.assertRaises(ValueError): self.reserve(self.batch(), [self.spec(size=10*MB+1)])
        self.assert_no_work()

    def test_300mb_plus_one_total_rejected(self):
        batch = self.batch()
        self.reserve(batch, [self.spec(i, size=10*MB) for i in range(30)])
        with self.assertRaises(ValueError): self.reserve(batch, [self.spec(31, role='jd', size=1)])
        self.assert_no_work()

    def test_archive_300mb_plus_one_rejected(self):
        with self.assertRaises(ValueError): self.reserve(self.batch('zip'), [self.spec(role='archive', size=300*MB+1)])
        self.assert_no_work()

    def test_incomplete_seal_never_dispatches(self):
        batch = self.batch()
        self.reserve(batch)
        with self.assertRaises(ValueError): self.state.seal_batch(batch.pk, self.user.pk)
        self.assert_no_work()

    def test_confirmation_requires_matching_size_and_digest(self):
        batch = self.batch(); item = self.reserve(batch)[0]
        with self.assertRaises(ValueError): self.state.confirm_upload(batch.pk, self.user.pk, item.pk, 101, item.sha256)
        item.refresh_from_db(); self.assertIsNone(item.uploaded_at)
        self.assert_no_work()

    def test_duplicate_seal_dispatches_once_and_freezes_inputs(self):
        batch, _ = self.ready()
        self.state.seal_batch(batch.pk, self.user.pk)
        self.assertEqual(self.models.CloudOutbox.objects.filter(kind='dispatch').count(), 1)
        with self.assertRaises(ValueError): self.reserve(batch, [self.spec(1)])
        self.assertTrue(batch.input_manifest_digest)
        self.assertEqual(batch.jd_text, 'Python')
        self.assertTrue(batch.inputs.first().storage_key.startswith(f'cloud/{self.profile.account_key}/{batch.pk}/inputs/'))

    def test_cancelled_account_rejects_admission(self):
        self.profile.cancelled_at = timezone.now(); self.profile.save()
        with self.assertRaises(ValueError): self.batch()
        self.assert_no_work()

    def test_deleted_account_cannot_dispatch(self):
        batch = self.batch(); self.reserve(batch)
        self.user.delete()
        with self.assertRaises(ValueError): self.state.seal_batch(batch.pk, self.user.pk)
        self.assertFalse(self.models.CloudOutbox.objects.exists())

    def test_tombstoned_account_rejects_admission(self):
        from recruiting.models import DeletionTombstone
        DeletionTombstone.objects.create(account_key=self.profile.account_key)
        with self.assertRaises(ValueError): self.batch()
        self.assert_no_work()

    def test_jd_pasted_text_limit_and_uploaded_jd_limit(self):
        self.session.jd = 'x' * 30001; self.session.save()
        with self.assertRaises(ValueError): self.batch()
        self.session.jd = ''; self.session.save()
        batch = self.batch()
        with self.assertRaises(ValueError): self.reserve(batch, [self.spec(role='jd', size=10*MB+1)])
        self.assert_no_work()

    def test_empty_role_requires_jd_before_seal(self):
        self.session.jd = ''; self.session.save()
        batch = self.batch(); item = self.reserve(batch)[0]
        self.state.confirm_upload(batch.pk, self.user.pk, item.pk, item.byte_size, item.sha256)
        with self.assertRaises(ValueError): self.state.seal_batch(batch.pk, self.user.pk)
        self.assert_no_work()

    def test_legacy_reservation_excludes_cloud_admission(self):
        self.profile.completed_credits = 29; self.profile.save()
        candidate = Candidate.objects.create(session=self.session, filename='a', storage_name='a', digest='a', rubric_version=1)
        from recruiting.jobs import enqueue
        enqueue(candidate, 1)
        with self.assertRaises(ValueError): self.reserve(self.batch())
        self.assert_no_work(completed=29)

    def test_cloud_reservation_excludes_legacy_admission(self):
        self.profile.completed_credits = 29; self.profile.save()
        self.reserve(self.batch())
        candidate = Candidate.objects.create(session=self.session, filename='a', storage_name='a', digest='a', rubric_version=1)
        from recruiting.jobs import enqueue
        with self.assertRaises(ValueError): enqueue(candidate, 1)
        self.assertFalse(Job.objects.exists())

    def test_zip_reserves_remaining_capacity_including_individuals(self):
        self.profile.completed_credits = 25; self.profile.save()
        batch = self.batch('zip')
        self.reserve(batch, [self.spec(), self.spec(1, 'archive', 20*MB)])
        batch.refresh_from_db(); self.assertEqual(batch.reserved_credits, 5)
        with self.assertRaises(ValueError): self.reserve(self.batch())
        self.assert_no_work(completed=25)

    def test_validated_zip_releases_unused_credits(self):
        batch, items = self.ready('zip', [self.spec(role='archive', size=20*MB)])
        attempt = self.state.claim_stage(batch.pk)
        expanded = [{'source_id': str(items[0].pk), 'filename': 'a.pdf', 'byte_size': 100, 'sha256': 'a'*64, 'scan_status': 'clean'}]
        self.assertTrue(self.state.apply_transition(batch.pk, attempt.pk, 'validate', self.envelope(batch, attempt, expanded_inputs=expanded)))
        batch.refresh_from_db(); self.assertEqual(batch.reserved_credits, 1)
        self.assertEqual(self.reserve(self.batch())[0].role, 'resume')

    def test_excess_zip_members_fail_visibly_before_analysis(self):
        self.profile.completed_credits = 29; self.profile.save()
        batch, items = self.ready('zip', [self.spec(role='archive')]); attempt = self.state.claim_stage(batch.pk)
        expanded = [{'source_id': str(items[0].pk), 'filename': f'{i}.pdf', 'byte_size': 100, 'sha256': str(i)*64} for i in range(2)]
        self.assertFalse(self.state.apply_transition(batch.pk, attempt.pk, 'validate', self.envelope(batch, attempt, expanded_inputs=expanded)))
        batch.refresh_from_db(); self.assertEqual(batch.stage, 'failed'); self.assertEqual(batch.reserved_credits, 0)
        self.assertTrue(batch.error_code)
        self.assertFalse(self.models.CloudOutbox.objects.filter(stage='analyze').exists())

    def test_validation_rejects_oversized_member_or_extracted_jd(self):
        for fields in ({'expanded_inputs': [{'source_id': 'missing', 'filename': 'a.pdf', 'byte_size': 10*MB+1, 'sha256': 'a'*64}]},
                       {'jd_text': 'x'*30001}):
            with self.subTest(fields=list(fields)):
                batch, items = self.ready(); attempt = self.state.claim_stage(batch.pk)
                data = {'expanded_inputs': self.expanded(items), **fields}
                self.assertFalse(self.state.apply_transition(batch.pk, attempt.pk, 'validate', self.envelope(batch, attempt, **data)))
                batch.refresh_from_db(); self.assertEqual(batch.stage, 'failed'); self.assertEqual(batch.reserved_credits, 0)

    def test_stale_attempt_cannot_transition_or_settle(self):
        batch, attempt = self.analyzed()
        self.models.StageAttempt.objects.filter(pk=attempt.pk).update(lease_until=timezone.now()-timedelta(seconds=1))
        self.assertFalse(self.state.apply_transition(batch.pk, attempt.pk, 'analyze', self.envelope(batch, attempt, results=self.results(batch))))
        self.assertFalse(self.models.CloudResultReference.objects.exists())
        self.profile.refresh_from_db(); self.assertEqual(self.profile.completed_credits, 0)

    def test_wrong_tenant_epoch_inputs_rubric_or_lease_cannot_transition(self):
        batch, items = self.ready(); attempt = self.state.claim_stage(batch.pk)
        for key, value in [('account_key', 'other'), ('account_epoch', 999), ('input_manifest_digest', '0'*64), ('rubric_version', 999), ('lease_token', 'wrong')]:
            payload = self.envelope(batch, attempt, expanded_inputs=self.expanded(items)); payload[key] = value
            self.assertFalse(self.state.apply_transition(batch.pk, attempt.pk, 'validate', payload))
        batch.refresh_from_db(); self.assertEqual(batch.stage, 'validate')
        self.assertEqual(self.models.CloudOutbox.objects.count(), 1)

    def test_profile_epoch_advance_invalidates_inflight_completion(self):
        batch, attempt = self.analyzed()
        self.profile.account_epoch += 1; self.profile.save()
        self.assertFalse(self.state.apply_transition(batch.pk, attempt.pk, 'analyze', self.envelope(batch, attempt, results=self.results(batch))))
        batch.refresh_from_db(); self.assertEqual(batch.reserved_credits, 0)
        self.profile.refresh_from_db(); self.assertEqual(self.profile.completed_credits, 0)

    def test_cancelled_account_completion_releases_reservation(self):
        batch, attempt = self.analyzed()
        self.profile.cancelled_at = timezone.now(); self.profile.save()
        self.assertFalse(self.state.apply_transition(batch.pk, attempt.pk, 'analyze', self.envelope(batch, attempt, results=self.results(batch))))
        batch.refresh_from_db(); self.assertEqual(batch.stage, 'cancelled'); self.assertEqual(batch.reserved_credits, 0)
        self.profile.refresh_from_db(); self.assertEqual(self.profile.completed_credits, 0)

    def test_success_settles_once_and_persists_private_result_reference(self):
        batch, attempt = self.analyzed(); payload = self.envelope(batch, attempt, results=self.results(batch))
        self.assertTrue(self.state.apply_transition(batch.pk, attempt.pk, 'analyze', payload))
        self.assertFalse(self.state.apply_transition(batch.pk, attempt.pk, 'analyze', payload))
        self.profile.refresh_from_db(); self.assertEqual(self.profile.completed_credits, 1)
        batch.refresh_from_db(); self.assertEqual(batch.reserved_credits, 0)
        result = self.models.CloudResultReference.objects.get(batch=batch)
        self.assertEqual(result.storage_backend, 'private_vps')
        self.assertIsNone(result.candidate_id); self.assertIsNone(result.job_id)
        self.assertEqual(self.models.CloudOutbox.objects.filter(kind='notification').count(), 1)
        self.assertFalse(Candidate.objects.exists())

    def test_remote_result_path_cannot_be_persisted_or_charged(self):
        batch, attempt = self.analyzed(); results = self.results(batch); results[0]['storage_key'] = 'https://public.example/resume'
        self.assertFalse(self.state.apply_transition(batch.pk, attempt.pk, 'analyze', self.envelope(batch, attempt, results=results)))
        self.profile.refresh_from_db(); self.assertEqual(self.profile.completed_credits, 0)
        self.assertFalse(self.models.CloudResultReference.objects.exists())

    def test_failed_stage_and_expired_batch_release_capacity(self):
        batch, _ = self.ready(); attempt = self.state.claim_stage(batch.pk)
        self.assertTrue(self.state.apply_transition(batch.pk, attempt.pk, 'validate', self.envelope(batch, attempt, failed=True)))
        batch.refresh_from_db(); self.assertEqual(batch.stage, 'failed'); self.assertEqual(batch.reserved_credits, 0)
        other = self.batch(); self.reserve(other)
        self.models.CloudBatch.objects.filter(pk=other.pk).update(expires_at=timezone.now()-timedelta(seconds=1))
        self.state.expire_batches()
        other.refresh_from_db(); self.assertEqual(other.stage, 'expired'); self.assertEqual(other.reserved_credits, 0)

    def test_rubric_changed_during_upload_cannot_seal(self):
        batch = self.batch(); item = self.reserve(batch)[0]
        self.state.confirm_upload(batch.pk, self.user.pk, item.pk, item.byte_size, item.sha256)
        self.session.rubric_version = 2; self.session.save()
        with self.assertRaises(ValueError): self.state.seal_batch(batch.pk, self.user.pk)
        self.assert_no_work()

    def test_pasted_jd_cannot_change_during_validation(self):
        batch, items = self.ready(); attempt = self.state.claim_stage(batch.pk)
        payload = self.envelope(batch, attempt, expanded_inputs=self.expanded(items), jd_text='Replace Python with something else')
        self.assertFalse(self.state.apply_transition(batch.pk, attempt.pk, 'validate', payload))
        batch.refresh_from_db(); self.assertEqual(batch.stage, 'failed')
        self.assertEqual(batch.reserved_credits, 0)

    def test_uploaded_jd_requires_clean_scan_attestation(self):
        self.session.jd = ''; self.session.save()
        batch, items = self.ready(specs=[self.spec(), self.spec(1, role='jd')])
        attempt = self.state.claim_stage(batch.pk)
        payload = self.envelope(batch, attempt, expanded_inputs=self.expanded(items), jd_text='Python')
        self.assertFalse(self.state.apply_transition(batch.pk, attempt.pk, 'validate', payload))
        batch.refresh_from_db(); self.assertEqual(batch.stage, 'failed')

    def test_unscanned_resume_cannot_reach_analysis(self):
        batch, items = self.ready(); attempt = self.state.claim_stage(batch.pk)
        expanded = self.expanded(items); expanded[0]['scan_status'] = 'infected'
        self.assertFalse(self.state.apply_transition(batch.pk, attempt.pk, 'validate', self.envelope(batch, attempt, expanded_inputs=expanded)))
        batch.refresh_from_db(); self.assertEqual(batch.stage, 'failed')
        self.assertFalse(self.models.CloudOutbox.objects.filter(stage='analyze').exists())

    def test_claim_is_exclusive_and_deleted_account_completion_is_ignored(self):
        batch, items = self.ready(); attempt = self.state.claim_stage(batch.pk)
        self.assertIsNone(self.state.claim_stage(batch.pk))
        payload = self.envelope(batch, attempt, expanded_inputs=self.expanded(items))
        self.user.delete()
        self.assertFalse(self.state.apply_transition(batch.pk, attempt.pk, 'validate', payload))
        self.assertFalse(self.models.CloudOutbox.objects.exists())

    def test_jd_only_upload_does_not_reserve_candidate_credit(self):
        batch = self.batch(); self.reserve(batch, [self.spec(role='jd')])
        batch.refresh_from_db(); self.assertEqual(batch.reserved_credits, 0)
        self.assert_no_work()

    def test_expired_attempt_cleanup_releases_capacity(self):
        batch, _ = self.ready(); attempt = self.state.claim_stage(batch.pk)
        self.models.StageAttempt.objects.filter(pk=attempt.pk).update(lease_until=timezone.now()-timedelta(seconds=1))
        self.assertEqual(self.state.expire_batches(), 1)
        batch.refresh_from_db(); self.assertEqual(batch.stage, 'expired'); self.assertEqual(batch.reserved_credits, 0)
        self.assertFalse(self.models.CloudOutbox.objects.filter(state='pending').exists())

    def test_jd_first_batch_automatically_builds_role_without_confirmation(self):
        self.session.confirmed_at = None
        self.session.rubric = []
        self.session.rubric_version = 0
        self.session.rubric_status = 'queued'
        self.session.save()
        batch, items = self.ready(); attempt = self.state.claim_stage(batch.pk)
        self.assertTrue(self.state.apply_transition(batch.pk, attempt.pk, 'validate', self.envelope(batch, attempt, expanded_inputs=self.expanded(items))))
        batch.refresh_from_db(); self.assertEqual(batch.stage, 'role')
        self.assertEqual(self.models.CloudOutbox.objects.filter(stage='role', kind='dispatch').count(), 1)
        attempt = self.state.claim_stage(batch.pk)
        role_payload = self.envelope(batch, attempt, criteria=[{'id': 'c1', 'name': 'Python', 'weight': 1, 'source_quote': 'Python'}])
        self.assertTrue(self.state.apply_transition(batch.pk, attempt.pk, 'role', role_payload))
        batch.refresh_from_db(); self.assertEqual(batch.stage, 'analyze')
        self.assertEqual(batch.rubric_version, 1)
        self.assertEqual(batch.rubric_snapshot[0]['name'], 'Python')
        self.session.refresh_from_db(); self.assertEqual(self.session.rubric_version, 1)
        self.assertEqual(self.session.rubric_status, 'ready')
        attempt = self.state.claim_stage(batch.pk)
        self.assertTrue(self.state.apply_transition(batch.pk, attempt.pk, 'analyze', self.envelope(batch, attempt, results=self.results(batch))))
        self.profile.refresh_from_db(); self.assertEqual(self.profile.completed_credits, 1)

    def test_unsupported_role_criteria_cannot_reach_analysis(self):
        self.session.confirmed_at = None; self.session.rubric = []; self.session.rubric_version = 0; self.session.save()
        batch, items = self.ready(); attempt = self.state.claim_stage(batch.pk)
        self.state.apply_transition(batch.pk, attempt.pk, 'validate', self.envelope(batch, attempt, expanded_inputs=self.expanded(items)))
        batch.refresh_from_db(); attempt = self.state.claim_stage(batch.pk)
        payload = self.envelope(batch, attempt, criteria=[{'id': 'c1', 'name': 'Java', 'weight': 1, 'source_quote': 'Java'}])
        self.assertFalse(self.state.apply_transition(batch.pk, attempt.pk, 'role', payload))
        batch.refresh_from_db(); self.assertEqual(batch.stage, 'failed'); self.assertEqual(batch.reserved_credits, 0)
        self.assertFalse(self.models.CloudOutbox.objects.filter(stage='analyze').exists())

    def test_uploaded_replacement_jd_always_requires_fresh_role(self):
        batch, items = self.ready(specs=[self.spec(), self.spec(1, role='jd')])
        original_digest = batch.input_manifest_digest
        attempt = self.state.claim_stage(batch.pk)
        payload = self.envelope(batch, attempt, expanded_inputs=self.expanded(items), jd_text='Java', jd_scan_status='clean')
        self.assertTrue(self.state.apply_transition(batch.pk, attempt.pk, 'validate', payload))
        batch.refresh_from_db()
        self.assertEqual(batch.stage, 'role')
        self.assertEqual(batch.jd_text, 'Java')
        self.assertEqual(batch.rubric_snapshot, [])
        self.assertEqual(batch.input_manifest_digest, original_digest)
        self.assertFalse(self.models.CloudOutbox.objects.filter(stage='analyze').exists())
        attempt = self.state.claim_stage(batch.pk)
        payload = self.envelope(batch, attempt, criteria=[{'id': 'java', 'name': 'Java', 'weight': 1, 'source_quote': 'Java'}])
        self.assertTrue(self.state.apply_transition(batch.pk, attempt.pk, 'role', payload))
        batch.refresh_from_db(); self.session.refresh_from_db()
        self.assertEqual(batch.stage, 'analyze')
        self.assertEqual(batch.rubric_version, 2)
        self.assertEqual(self.session.jd, 'Java')
        self.assertEqual(self.session.rubric, batch.rubric_snapshot)

    def test_uploaded_only_jd_promotion_supports_next_resume_only_batch(self):
        self.session.jd = ''; self.session.rubric = []; self.session.rubric_version = 0
        self.session.confirmed_at = None; self.session.save()
        batch, items = self.ready(specs=[self.spec(), self.spec(1, role='jd')])
        attempt = self.state.claim_stage(batch.pk)
        self.assertTrue(self.state.apply_transition(batch.pk, attempt.pk, 'validate', self.envelope(batch, attempt,
            expanded_inputs=self.expanded(items), jd_text='Java', jd_scan_status='clean')))
        batch.refresh_from_db(); attempt = self.state.claim_stage(batch.pk)
        self.assertTrue(self.state.apply_transition(batch.pk, attempt.pk, 'role', self.envelope(batch, attempt,
            criteria=[{'id': 'java', 'name': 'Java', 'weight': 1, 'source_quote': 'Java'}])))
        self.session.refresh_from_db(); batch.refresh_from_db()
        self.assertEqual(self.session.jd, 'Java')
        self.assertEqual(self.session.rubric, batch.rubric_snapshot)
        next_batch, next_items = self.ready()
        self.assertEqual(next_batch.jd_text, 'Java')
        self.assertEqual(next_batch.rubric_snapshot, batch.rubric_snapshot)
        attempt = self.state.claim_stage(next_batch.pk)
        self.assertTrue(self.state.apply_transition(next_batch.pk, attempt.pk, 'validate', self.envelope(next_batch, attempt,
            expanded_inputs=self.expanded(next_items))))
        next_batch.refresh_from_db(); self.assertEqual(next_batch.stage, 'analyze')
        self.assertEqual(next_batch.rubric_version, 1)

    def test_concurrent_jd_edit_cannot_be_overwritten_by_role_promotion(self):
        self.session.jd = ''; self.session.rubric = []; self.session.rubric_version = 0
        self.session.confirmed_at = None; self.session.save()
        batch, items = self.ready(specs=[self.spec(), self.spec(1, role='jd')])
        attempt = self.state.claim_stage(batch.pk)
        self.state.apply_transition(batch.pk, attempt.pk, 'validate', self.envelope(batch, attempt,
            expanded_inputs=self.expanded(items), jd_text='Java', jd_scan_status='clean'))
        batch.refresh_from_db(); attempt = self.state.claim_stage(batch.pk)
        # Even an edit without a version bump must not be silently overwritten.
        self.session.jd = 'Rust'; self.session.save(update_fields=['jd'])
        self.assertFalse(self.state.apply_transition(batch.pk, attempt.pk, 'role', self.envelope(batch, attempt,
            criteria=[{'id': 'java', 'name': 'Java', 'weight': 1, 'source_quote': 'Java'}])))
        self.session.refresh_from_db(); batch.refresh_from_db()
        self.assertEqual(self.session.jd, 'Rust')
        self.assertEqual(self.session.rubric_version, 0)
        self.assertEqual(self.session.rubric, [])
        self.assertEqual(batch.stage, 'failed'); self.assertEqual(batch.reserved_credits, 0)
        self.assertFalse(self.models.CloudOutbox.objects.filter(stage='analyze').exists())
        self.profile.refresh_from_db(); self.assertEqual(self.profile.completed_credits, 0)

    def test_jd_edit_during_upload_cannot_seal_against_previous_rubric(self):
        batch = self.batch(); item = self.reserve(batch)[0]
        self.state.confirm_upload(batch.pk, self.user.pk, item.pk, item.byte_size, item.sha256)
        self.session.jd = 'Java'; self.session.save(update_fields=['jd'])
        with self.assertRaises(ValueError): self.state.seal_batch(batch.pk, self.user.pk)
        self.assert_no_work()

    def test_uploaded_jd_validation_requires_explicit_extracted_text(self):
        batch, items = self.ready(specs=[self.spec(), self.spec(1, role='jd')])
        attempt = self.state.claim_stage(batch.pk)
        self.assertFalse(self.state.apply_transition(batch.pk, attempt.pk, 'validate', self.envelope(batch, attempt,
            expanded_inputs=self.expanded(items), jd_scan_status='clean')))
        batch.refresh_from_db(); self.assertEqual(batch.stage, 'failed')
        self.assertFalse(self.models.CloudOutbox.objects.filter(stage='analyze').exists())


class CloudConcurrencyTests(CloudFixtures, TransactionTestCase):
    def parallel(self, function, values):
        barrier = Barrier(len(values))
        def invoke(value):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return function(value)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=len(values)) as pool:
            return list(pool.map(invoke, values))

    def test_simultaneous_batches_cannot_share_last_credit(self):
        self.profile.completed_credits = 29; self.profile.save()
        batches = [self.batch(), self.batch()]
        def admit(batch):
            close_old_connections()
            try:
                self.reserve(batch)
                return True
            except ValueError:
                return False
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(admit, batches))
        self.assertEqual(sum(results), 1)
        self.assertEqual(sum(self.models.CloudBatch.objects.values_list('reserved_credits', flat=True)), 1)

    def test_mixed_legacy_and_cloud_race_for_last_credit(self):
        self.profile.completed_credits = 29; self.profile.save()
        batch = self.batch()
        candidate = Candidate.objects.create(session=self.session, filename='a', storage_name='a', digest='a', rubric_version=1)
        from recruiting.jobs import enqueue
        def admit(cloud):
            try:
                self.reserve(batch) if cloud else enqueue(candidate, 1)
                return True
            except ValueError:
                return False
        self.assertEqual(sum(self.parallel(admit, [True, False])), 1)
        batch.refresh_from_db()
        self.assertEqual(batch.reserved_credits + Job.objects.filter(reserved=True).count(), 1)

    def test_same_completion_settles_once_under_race(self):
        batch, attempt = self.analyzed()
        payload = self.envelope(batch, attempt, results=self.results(batch))
        outcomes = self.parallel(lambda _: self.state.apply_transition(batch.pk, attempt.pk, 'analyze', payload), [1, 2])
        self.assertEqual(sum(outcomes), 1)
        self.profile.refresh_from_db(); self.assertEqual(self.profile.completed_credits, 1)
        self.assertEqual(self.models.CloudResultReference.objects.count(), 1)
        self.assertEqual(self.models.CloudOutbox.objects.filter(kind='notification').count(), 1)

    def test_two_batches_complete_without_lost_credit_update(self):
        work = [self.analyzed(), self.analyzed()]
        outcomes = self.parallel(lambda pair: self.state.apply_transition(pair[0].pk, pair[1].pk, 'analyze', self.envelope(pair[0], pair[1], results=self.results(pair[0]))), work)
        self.assertEqual(sum(outcomes), 2)
        self.profile.refresh_from_db(); self.assertEqual(self.profile.completed_credits, 2)

    def test_parallel_claims_produce_one_attempt(self):
        batch, _ = self.ready()
        outcomes = self.parallel(lambda _: self.state.claim_stage(batch.pk), [1, 2])
        self.assertEqual(sum(attempt is not None for attempt in outcomes), 1)
        self.assertEqual(self.models.StageAttempt.objects.count(), 1)

    @skipUnless(connection.vendor == 'postgresql', 'Lock ordering requires real PostgreSQL.')
    def test_profile_lock_precedes_batch_and_attempt_locks(self):
        batch, attempt = self.analyzed()
        locks = []
        def record(execute, sql, params, many, context):
            if 'FOR UPDATE' in sql.upper():
                locks.append(sql)
            return execute(sql, params, many, context)
        with connection.execute_wrapper(record):
            self.assertTrue(self.state.apply_transition(batch.pk, attempt.pk, 'analyze', self.envelope(batch, attempt, results=self.results(batch))))
        self.assertIn('FROM "recruiting_profile"', locks[0])
        self.assertIn('FROM "recruiting_hiringsession"', locks[1])
        self.assertIn('FROM "recruiting_cloudbatch"', locks[2])
        self.assertIn('FROM "recruiting_stageattempt"', locks[3])
