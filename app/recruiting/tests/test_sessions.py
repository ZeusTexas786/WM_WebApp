from django.test import TestCase
from django.contrib.auth.models import User
from django.utils import timezone
from recruiting.models import Profile

class SessionTests(TestCase):
    def test_polling_and_failed_access_do_not_change_activity(self):
        from datetime import timedelta
        self.create_session()
        profile=Profile.objects.get(user=self.user)
        old=timezone.now()-timedelta(days=35)
        Profile.objects.filter(pk=profile.pk).update(last_activity_at=old,notice_at=old)
        self.client.get(f'/sessions/{self.hiring_id}/status/')
        self.client.get('/sessions/99999/')
        profile.refresh_from_db()
        self.assertEqual(profile.last_activity_at,old)
        self.assertEqual(profile.notice_at,old)

    def test_rankings_separate_rubric_versions(self):
        from recruiting.models import HiringSession,Candidate,Job
        self.create_session()
        hiring=HiringSession.objects.get(); hiring.rubric_version=2; hiring.save()
        old=Candidate.objects.create(session=hiring,filename='old.txt',storage_name='old',digest='old',rubric_version=1)
        Job.objects.create(candidate=old,jd='old',status='completed',reserved=False,result={'alignment':100,'name':'Old Person'})
        response=self.client.get(f'/sessions/{hiring.pk}/')
        self.assertContains(response,'Earlier rubric versions')
        self.assertEqual(response.context['jobs'],[])
        self.assertEqual(len(response.context['older_jobs']),1)
    def setUp(self):
        self.user = User.objects.create_user('owner@example.com', password='strong-test-password')
        Profile.objects.create(user=self.user, verified_at=timezone.now())
        self.client.force_login(self.user)

    def create_session(self):
        response = self.client.post('/sessions/new/', {'title': 'Senior Python Engineer', 'jd': 'Build Python services with PostgreSQL and reliable automated tests.', 'mode': 'sample'})
        from recruiting.models import HiringSession
        self.hiring_id = HiringSession.objects.get(owner=self.user).pk
        return response

    def test_session_is_owner_scoped(self):
        self.create_session()
        other = User.objects.create_user('other@example.com')
        Profile.objects.create(user=other, verified_at=timezone.now())
        self.client.force_login(other)
        self.assertEqual(self.client.get(f'/sessions/{self.hiring_id}/').status_code, 404)

    def test_weights_must_be_positive(self):
        self.create_session()
        response = self.client.post(f'/sessions/{self.hiring_id}/confirm/', {'names': ['Python'], 'weights': ['0']})
        self.assertEqual(response.status_code, 400)

    def test_blank_jd_is_rejected(self):
        response = self.client.post('/sessions/new/', {'title': 'Role', 'jd': '', 'mode': 'sample'})
        self.assertContains(response, 'This field is required.')

    def test_edit_creates_new_rubric_version(self):
        from recruiting.models import HiringSession
        self.create_session()
        for _ in range(2):
            self.client.post(f'/sessions/{self.hiring_id}/confirm/', {'names': ['Python', 'SQL'], 'weights': ['2', '1']})
        session = HiringSession.objects.get()
        self.assertEqual(session.rubric_version, 2)
        self.assertEqual(session.rubric[0]['weight'], 2)

    def test_unconfirmed_rubric_cannot_enqueue(self):
        from recruiting.models import HiringSession
        self.create_session()
        session = HiringSession.objects.get()
        self.assertIsNone(session.confirmed_at)
        self.assertContains(self.client.get(f'/sessions/{self.hiring_id}/'), 'Confirm rubric')
