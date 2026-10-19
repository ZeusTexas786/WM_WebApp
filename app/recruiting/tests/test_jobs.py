from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from django.test import TestCase, TransactionTestCase
from django.contrib.auth.models import User
from django.db import close_old_connections
from django.utils import timezone
from recruiting.models import Profile, HiringSession, Candidate

def fixture():
    user = User.objects.create_user('queue@example.com')
    profile = Profile.objects.create(user=user, verified_at=timezone.now())
    session = HiringSession.objects.create(owner=user, title='Role', jd='Python', confirmed_at=timezone.now(), rubric_version=1, rubric=[{'id':'c1','name':'Python','weight':1}])
    return user, profile, session

def candidate(session, digest='a'):
    return Candidate.objects.create(session=session, filename='resume.txt', storage_name='unused.txt', digest=digest, rubric_version=1)

class JobTests(TestCase):
    def setUp(self): self.user, self.profile, self.session = fixture()

    def test_interview_lease_recovery_and_fencing(self):
        from recruiting.jobs import claim_interview,complete_interview,recover_stale
        from recruiting.models import Job
        item=candidate(self.session)
        job=Job.objects.create(candidate=item,jd='JD',status='completed',reserved=False,interview_requested=True)
        claim=claim_interview()
        self.assertIsNotNone(claim)
        self.assertIsNone(claim_interview())
        Job.objects.filter(pk=job.pk).update(interview_lease_until=timezone.now()-timedelta(seconds=1))
        recover_stale()
        complete_interview(job.pk,{'questions':[]},claim.interview_lease_token)
        job.refresh_from_db()
        self.assertEqual(job.interviews,{})
        self.assertIn('interrupted',job.interview_error.lower())

    def test_unconfirmed_and_expired_cannot_enqueue(self):
        from recruiting.jobs import enqueue
        item = candidate(self.session)
        self.session.confirmed_at = None; self.session.save()
        with self.assertRaises(ValueError): enqueue(item, 1)
        self.session.confirmed_at = timezone.now(); self.session.save()
        self.profile.verified_at = timezone.now()-timedelta(hours=72); self.profile.save()
        with self.assertRaises(ValueError): enqueue(item, 1)

    def test_quota_and_duplicate_admission(self):
        from recruiting.jobs import enqueue
        self.profile.completed_credits = 29; self.profile.save()
        first = candidate(self.session)
        job = enqueue(first, 1)
        self.assertEqual(enqueue(first, 1).pk, job.pk)
        with self.assertRaises(ValueError): enqueue(candidate(self.session, 'b'), 1)

    def test_success_settles_once_after_expiry(self):
        from recruiting.jobs import enqueue, claim_job, complete_job
        job = enqueue(candidate(self.session), 1)
        claimed = claim_job('worker-1')
        self.profile.verified_at = timezone.now()-timedelta(days=4); self.profile.save()
        complete_job(job.pk, {'alignment': 50}, claimed.lease_token)
        complete_job(job.pk, {'alignment': 100}, claimed.lease_token)
        self.profile.refresh_from_db(); job.refresh_from_db()
        self.assertEqual(self.profile.completed_credits, 1)
        self.assertEqual(job.result['alignment'], 50)

    def test_cancel_and_failure_release_reservations(self):
        from recruiting.jobs import enqueue, claim_job, fail_job, cancel_job
        from recruiting.models import Job
        job = enqueue(candidate(self.session), 1)
        cancel_job(job.pk, self.user)
        self.assertEqual(Job.objects.filter(reserved=True).count(), 0)
        job = enqueue(job.candidate, 1)
        claim = claim_job('w')
        fail_job(job.pk, 'Provider unavailable', claim.lease_token)
        self.assertEqual(Job.objects.filter(reserved=True).count(), 0)
        self.assertEqual(self.profile.completed_credits, 0)

    def test_stale_worker_cannot_complete_recovered_job(self):
        from recruiting.jobs import enqueue, claim_job, recover_stale, complete_job
        from recruiting.models import Job
        job = enqueue(candidate(self.session), 1)
        claimed = claim_job('w')
        Job.objects.filter(pk=job.pk).update(lease_until=timezone.now()-timedelta(seconds=1))
        recover_stale()
        complete_job(job.pk, {'alignment': 100}, claimed.lease_token)
        job.refresh_from_db()
        self.assertEqual(job.status, 'failed')
        self.assertFalse(job.reserved)

class ConcurrencyTests(TransactionTestCase):
    def test_concurrent_admission_does_not_overbook(self):
        from recruiting.jobs import enqueue
        from recruiting.models import Job
        user, profile, session = fixture()
        profile.completed_credits = 29; profile.save()
        ids = [candidate(session, str(i)).pk for i in range(2)]
        def admit(pk):
            close_old_connections()
            try:
                enqueue(Candidate.objects.get(pk=pk), 1)
                return True
            except ValueError:
                return False
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(admit, ids))
        self.assertEqual(sum(results), 1)
        self.assertEqual(Job.objects.filter(reserved=True).count(), 1)
