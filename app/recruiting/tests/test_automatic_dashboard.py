from datetime import timedelta
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone
from recruiting.models import Profile, HiringSession, Candidate, Job
from recruiting.cloud.models import CloudBatch, CloudResultReference


@override_settings(AUTOMATIC_WORKFLOW_ENABLED=True, AUTOMATIC_TRANSPORT='fixtures')
class AutomaticDashboardTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('dashboard-owner', email='owner@example.test')
        self.profile = Profile.objects.create(user=self.owner, verified_at=timezone.now(), completed_credits=7)
        self.other = User.objects.create_user('dashboard-other')
        Profile.objects.create(user=self.other, verified_at=timezone.now())
        self.session = HiringSession.objects.create(owner=self.owner, title='My sample role', mode='sample')
        self.other_session = HiringSession.objects.create(owner=self.other, title='OTHER PRIVATE ROLE', mode='sample')
        self.batch = self.make_batch(self.owner, self.session)
        self.client.force_login(self.owner)

    def make_batch(self, owner, session, stage='validate'):
        return CloudBatch.objects.create(owner=owner, session=session, account_key=owner.profile.account_key,
            account_epoch=owner.profile.account_epoch, input_mode='files', stage=stage,
            rubric_version=1, reserved_credits=3 if stage=='validate' else 0,
            expires_at=timezone.now()+timedelta(hours=24))

    def report(self, session, digest='a'*64, filename='Fictional candidate.txt', cloud_batch=None):
        candidate = Candidate.objects.create(session=session, digest=digest, filename=filename,
            storage_name='synthetic/'+digest, rubric_version=1)
        job = Job.objects.create(candidate=candidate, status='completed', reserved=False)
        if cloud_batch:
            CloudResultReference.objects.create(batch=cloud_batch, input_sha256=digest,
                storage_key='private-unrendered-artifact', candidate=candidate, job=job)
        return candidate, job

    def test_only_owner_submissions_and_consistent_completed_reports_are_visible(self):
        self.make_batch(self.other, self.other_session)
        self.batch.stage='completed'; self.batch.reserved_credits=0; self.batch.save()
        own, _ = self.report(self.session, cloud_batch=self.batch)
        foreign, foreign_job = self.report(self.other_session, digest='b'*64, filename='OTHER PRIVATE FILE')
        CloudResultReference.objects.create(batch=self.batch, input_sha256='b'*64,
            storage_key='https://untrusted.example/private', candidate=foreign, job=foreign_job)
        response = self.client.get('/dashboard/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'/batches/{self.batch.pk}/')
        self.assertContains(response, f'/candidates/{own.pk}/')
        self.assertContains(response, 'Synthetic demonstration')
        self.assertNotContains(response, 'OTHER PRIVATE')
        self.assertNotContains(response, 'untrusted.example')
        self.assertEqual(response.context.get('ready_count'), 1)
        self.assertEqual(response['Cache-Control'].find('no-store')>=0, True)

    def test_usage_includes_pending_cloud_and_legacy_reservations(self):
        for index in range(2):
            candidate = Candidate.objects.create(session=self.session, filename=f'{index}.txt',
                storage_name=f'synthetic/{index}', digest=str(index)*64, rubric_version=1)
            Job.objects.create(candidate=candidate, reserved=True)
        response = self.client.get('/dashboard/')
        self.assertEqual(response.context.get('reserved_credits'), 5)
        self.assertEqual(response.context.get('available_credits'), 18)

    def test_trial_expiry_blocks_start_but_keeps_existing_reports_visible(self):
        candidate, _ = self.report(self.session)
        self.profile.verified_at=timezone.now()-timedelta(days=4); self.profile.save()
        response = self.client.get('/dashboard/')
        self.assertIs(response.context.get('can_start'), False)
        self.assertContains(response, f'/candidates/{candidate.pk}/')
        self.assertContains(response, 'Your three-day trial has ended.')

    def test_status_endpoint_is_owner_only_and_contains_no_source_text(self):
        url=f'/batches/{self.batch.pk}/status/'
        response=self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['stage'], 'validate')
        self.assertNotIn('My sample role', response.content.decode())
        other=self.make_batch(self.other, self.other_session)
        self.assertEqual(self.client.get(f'/batches/{other.pk}/status/').status_code,404)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code,302)

    def test_completed_batch_links_readable_brief_and_pdf_but_not_raw_source(self):
        self.batch.stage='completed'; self.batch.reserved_credits=0; self.batch.save()
        candidate, _ = self.report(self.session, cloud_batch=self.batch,
                                    filename='<script>example</script>.txt')
        response = self.client.get(f'/batches/{self.batch.pk}/')
        self.assertContains(response, f'/candidates/{candidate.pk}/')
        self.assertContains(response, f'/candidates/{candidate.pk}/pdf/')
        self.assertNotContains(response, '/download/')
        self.assertContains(response, '&lt;script&gt;example&lt;/script&gt;.txt')
        self.assertNotContains(response, '<script>example</script>')
        status=self.client.get(f'/batches/{self.batch.pk}/status/').json()
        self.assertTrue(status['terminal'])
        self.assertEqual(status['ready_count'],1)

    def test_large_draft_history_is_paginated_and_status_reads_do_not_keep_account_active(self):
        for _ in range(24): self.make_batch(self.owner,self.session,stage='uploading')
        response=self.client.get('/dashboard/')
        self.assertEqual(len(response.context['batch_cards']),20)
        self.assertEqual(response.context['page_obj'].paginator.count,25)
        old=timezone.now()-timedelta(days=10)
        Profile.objects.filter(pk=self.profile.pk).update(last_activity_at=old)
        self.client.get(f'/batches/{self.batch.pk}/status/')
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.last_activity_at,old)

    def test_inconsistent_session_or_account_namespace_cannot_expose_private_role(self):
        wrong_session=self.make_batch(self.owner,self.other_session)
        wrong_key=self.make_batch(self.owner,self.session)
        wrong_key.account_key=self.other.profile.account_key; wrong_key.save()
        response=self.client.get('/dashboard/')
        self.assertNotContains(response,'OTHER PRIVATE ROLE')
        for batch in (wrong_session,wrong_key):
            self.assertEqual(self.client.get(f'/batches/{batch.pk}/').status_code,404)
            self.assertEqual(self.client.get(f'/batches/{batch.pk}/status/').status_code,404)

    def test_fixture_requires_three_available_briefs(self):
        self.profile.completed_credits=26; self.profile.save()
        response=self.client.get('/dashboard/')
        self.assertEqual(response.context['available_credits'],1)
        self.assertFalse(response.context['can_start'])
        self.assertContains(response,'three available briefs')

    def test_cloud_report_returns_to_submission_and_does_not_offer_unavailable_source(self):
        from recruiting.cloud.fixtures import evaluate_fixture
        from recruiting.cloud.sample_contract import sample_catalog, sample_rubric
        row=sample_catalog()[0]
        result=evaluate_fixture(row['filename'],row['content'],sample_rubric())
        self.batch.stage='completed'; self.batch.save()
        candidate,job=self.report(self.session,cloud_batch=self.batch)
        job.result=result['assessment']; job.interviews=result['interviews']; job.interview_requested=True; job.save()
        response=self.client.get(f'/candidates/{candidate.pk}/')
        self.assertEqual(response.status_code,200)
        self.assertContains(response,f'/batches/{self.batch.pk}/')
        self.assertNotContains(response,f'/sessions/{self.session.pk}/')
        self.assertNotContains(response,'Download resume')
        self.assertNotContains(response,'data-interview-state="pending"')
