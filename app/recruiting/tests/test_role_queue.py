from django.test import TransactionTestCase,override_settings
from django.contrib.auth.models import User
from django.utils import timezone
from recruiting.models import Profile,HiringSession

class RoleQueueTests(TransactionTestCase):
    @override_settings(AI_API_KEY='test-configured')
    def test_live_jd_request_queues_without_calling_cloud_on_web_thread(self):
        from unittest.mock import patch
        user=User.objects.create_user('role-queue'); Profile.objects.create(user=user,verified_at=timezone.now())
        self.client.force_login(user)
        with patch('recruiting.providers.propose_rubric',side_effect=AssertionError('No cloud call in web request')):
            response=self.client.post('/sessions/new/',{'title':'Engineer','jd':'Python backend engineering','mode':'live'})
        self.assertEqual(response.status_code,302)
        hiring=HiringSession.objects.get()
        self.assertEqual(hiring.rubric_status,'queued')

    def test_worker_failure_is_visible_and_retryable(self):
        from recruiting.roles import process_one_role
        user=User.objects.create_user('role-failure'); Profile.objects.create(user=user,verified_at=timezone.now())
        hiring=HiringSession.objects.create(owner=user,title='Engineer',jd='Python',mode='live',rubric_status='queued')
        with override_settings(AI_API_KEY=''): process_one_role()
        hiring.refresh_from_db()
        self.assertEqual(hiring.rubric_status,'failed')
        self.assertIn('not configured',hiring.rubric_error)
