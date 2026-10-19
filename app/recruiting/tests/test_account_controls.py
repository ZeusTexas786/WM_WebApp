from datetime import timedelta
from unittest.mock import patch
from django.test import TestCase,override_settings
from django.contrib.auth.models import User
from django.utils import timezone
from recruiting.models import Profile,HiringSession,Job,Candidate

class AccountControlTests(TestCase):
    def setUp(self):
        self.user=User.objects.create_user('controls@example.test')
        self.profile=Profile.objects.create(user=self.user,verified_at=timezone.now())
        self.client.force_login(self.user)

    def test_cancellation_has_recovery_window_and_blocks_new_sessions(self):
        self.assertEqual(self.client.post('/account/cancel/').status_code,302)
        self.profile.refresh_from_db(); self.assertIsNotNone(self.profile.cancelled_at)
        self.assertEqual(self.client.post('/sessions/new/',{'title':'Role','jd':'Python '*30,'mode':'sample'}).status_code,400)
        self.client.post('/account/reactivate/')
        self.profile.refresh_from_db(); self.assertIsNone(self.profile.cancelled_at)

    def test_archived_account_cannot_view_documents_or_count_as_active(self):
        earlier=timezone.now()-timedelta(days=200)
        Profile.objects.filter(pk=self.profile.pk).update(status='archived',last_activity_at=earlier)
        self.assertRedirects(self.client.get('/dashboard/'),'/account/',fetch_redirect_response=False)
        self.profile.refresh_from_db(); self.assertEqual(self.profile.last_activity_at,earlier)

    @override_settings(AI_API_KEY='synthetic-test-key')
    def test_expired_trial_cannot_request_cloud_role_analysis(self):
        Profile.objects.filter(pk=self.profile.pk).update(verified_at=timezone.now()-timedelta(days=4))
        response=self.client.post('/sessions/new/',{'title':'Role','jd':'Python '*30,'mode':'live'})
        self.assertEqual(response.status_code,400)
        self.assertEqual(HiringSession.objects.count(),0)
