from datetime import timedelta
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.contrib.auth.models import User
from django.utils import timezone

class AccountTests(TestCase):
    @override_settings(EMAIL_HOST='smtp.example.test',EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_recovery_is_rate_limited(self):
        User.objects.create_user('recover@example.com',email='recover@example.com',password='Strong-Test-12345')
        for _ in range(5): self.client.post('/recover/',{'email':'recover@example.com'})
        self.assertEqual(self.client.post('/recover/',{'email':'recover@example.com'}).status_code,429)

    @override_settings(EMAIL_HOST='')
    def test_unconfigured_recovery_does_not_fail(self):
        User.objects.create_user('recover@example.com',email='recover@example.com',password='Strong-Test-12345')
        self.assertEqual(self.client.post('/recover/',{'email':'recover@example.com'}).status_code,302)
    def register(self):
        return self.client.post('/register/', {'email': 'owner@example.com', 'password1': 'Correct-Horse-832!', 'password2': 'Correct-Horse-832!'})

    def test_unverified_user_cannot_access_dashboard(self):
        self.register()
        self.assertRedirects(self.client.get('/dashboard/'), '/verify-pending/', fetch_redirect_response=False)

    def test_verification_token_is_single_use_and_expires(self):
        from recruiting.models import Verification
        from recruiting.accounts import verify_token
        self.register()
        token = self.client.session['local_verification_token']
        self.assertTrue(verify_token(token))
        self.assertFalse(verify_token(token))
        self.client.logout()
        self.client.post('/register/', {'email': 'other@example.com', 'password1': 'Another-Strong-832!', 'password2': 'Another-Strong-832!'})
        token = self.client.session['local_verification_token']
        Verification.objects.filter(used_at__isnull=True).update(created_at=timezone.now() - timedelta(hours=25))
        self.assertFalse(verify_token(token))

    def test_local_mail_requires_initiating_session(self):
        self.register()
        self.assertContains(self.client.get('/local-mail/'), 'Local verification')
        from django.test import Client
        self.assertEqual(Client().get('/local-mail/').status_code, 404)

    @override_settings(LOCAL_DEVELOPMENT=False)
    def test_production_disables_local_mail(self):
        self.assertEqual(self.client.get('/local-mail/').status_code, 404)

    @override_settings(EMAIL_HOST='')
    def test_missing_smtp_is_not_reported_as_sent(self):
        self.register()
        self.assertContains(self.client.get('/verify-pending/'), 'No email was sent')

    def test_signup_invalid_password_and_duplicate_are_safe(self):
        self.register()
        self.client.logout()
        response = self.register()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(User.objects.count(), 1)

    def test_login_is_rate_limited(self):
        for _ in range(10):
            self.client.post('/login/', {'username': 'nobody@example.com', 'password': 'wrong'})
        self.assertEqual(self.client.post('/login/', {'username': 'nobody@example.com', 'password': 'wrong'}).status_code, 429)
