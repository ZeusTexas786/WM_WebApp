"""The lightweight fixture profile must not weaken ordinary production."""
import os
import runpy
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch
from django.test import SimpleTestCase, TestCase, RequestFactory, override_settings
from django.http import HttpResponse
from django.utils import timezone
from datetime import timedelta
from .test_production_settings import valid_environment


def pilot_environment():
    env = valid_environment()
    for key in ('AI_API_KEY', 'SCANNER_HOST', 'PARSER_SOCKET'):
        env.pop(key)
    env.update(AUTOMATIC_WORKFLOW_ENABLED='1', AUTOMATIC_TRANSPORT='fixtures',
               AI_MAX_DAILY_REQUESTS='0', LIFECYCLE_EXECUTION_ENABLED='0')
    return env


class PilotConfigurationTests(SimpleTestCase):
    def load(self, env=None):
        from config.production_config import load_production_config
        return load_production_config(env or pilot_environment(), profile='fixture_pilot')

    def test_fixture_profile_keeps_production_security_and_removes_live_execution(self):
        result = self.load()
        self.assertTrue(result['FIXTURE_PILOT'])
        self.assertFalse(result['LOCAL_DEVELOPMENT'])
        self.assertFalse(result['DEBUG'])
        self.assertTrue(result['SESSION_COOKIE_SECURE'])
        self.assertTrue(result['CSRF_COOKIE_SECURE'])
        self.assertEqual(result['DATABASES']['default']['ENGINE'], 'django.db.backends.postgresql')
        self.assertEqual(result['AI_MAX_DAILY_REQUESTS'], 0)
        self.assertEqual(result['AI_API_KEY'], '')
        self.assertEqual(result['SCANNER_HOST'], '')
        self.assertEqual(result['PARSER_SOCKET'], '')
        self.assertFalse(result['LIFECYCLE_EXECUTION_ENABLED'])

    def test_fixture_profile_rejects_live_transport_budget_and_lifecycle(self):
        for key, value in [('AUTOMATIC_WORKFLOW_ENABLED','0'), ('AUTOMATIC_TRANSPORT','remote'),
                           ('AI_MAX_DAILY_REQUESTS','1'), ('AI_MAX_DAILY_REQUESTS',''),
                           ('AUTOMATIC_REMOTE_EXECUTOR','some.adapter'),
                           ('LIFECYCLE_EXECUTION_ENABLED','1'), ('WEBMONKIE_LOCAL','1')]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.load(dict(pilot_environment(), **{key:value}))

    def test_operator_ai_key_cannot_leak_into_fixture_provider_settings(self):
        self.assertEqual(self.load(dict(pilot_environment(), AI_API_KEY='synthetic-unused-key'))['AI_API_KEY'], '')

    def test_fixture_profile_still_requires_mail_database_and_exact_origin(self):
        for key in ('SMTP_HOST','SMTP_PASSWORD','POSTGRES_PASSWORD','ARCHIVE_KEY'):
            env = pilot_environment(); env.pop(key)
            with self.subTest(key=key), self.assertRaises(ValueError): self.load(env)
        with self.assertRaises(ValueError):
            self.load(dict(pilot_environment(), PUBLIC_ORIGIN='http://app.example.com'))

    def test_ordinary_production_still_requires_scanner_parser_and_ai_key(self):
        from config.production_config import load_production_config
        with self.assertRaises(ValueError): load_production_config(pilot_environment())
        self.assertFalse(load_production_config(valid_environment())['FIXTURE_PILOT'])
        with self.assertRaises(ValueError): load_production_config(valid_environment(), profile='unknown')

    def test_alternate_production_module_cannot_read_developer_env_or_create_secret(self):
        from django.conf import settings
        with tempfile.TemporaryDirectory(dir=settings.BASE_DIR.parent/'.local') as folder:
            root = Path(folder); config = root/'app'/'config'; config.mkdir(parents=True)
            source = config/'settings.py'
            shutil.copyfile(settings.BASE_DIR/'config'/'settings.py', source)
            (root/'.env').write_text('LOCAL_ENV_SENTINEL=unexpected\nSMTP_HOST=sentinel.invalid\n', encoding='utf-8')
            with patch.dict(os.environ, {'DJANGO_SETTINGS_MODULE':'config.pilot',
                    'WEBMONKIE_LOCAL':'0', 'DJANGO_SECRET_KEY':'synthetic-test-secret'}, clear=True):
                result = runpy.run_path(str(source))
                self.assertNotIn('LOCAL_ENV_SENTINEL', os.environ)
                self.assertEqual(result['EMAIL_HOST'], '')
                self.assertFalse((root/'app'/'.secret').exists())

    def test_pilot_entry_installs_fence_before_session_and_body_parsing(self):
        with patch.dict(os.environ, dict(pilot_environment(), DJANGO_SETTINGS_MODULE='config.pilot'), clear=True):
            result = runpy.run_module('config.pilot')
        middleware = result['MIDDLEWARE']
        fence = middleware.index('recruiting.pilot.FixturePilotFence')
        self.assertLess(middleware.index('django.middleware.security.SecurityMiddleware'), fence)
        self.assertLess(fence, middleware.index('django.contrib.sessions.middleware.SessionMiddleware'))


class PilotRouteFenceTests(SimpleTestCase):
    def call(self, path):
        from recruiting.pilot import FixturePilotFence
        return FixturePilotFence(lambda request: HttpResponse('reached'))(RequestFactory().get(path))

    @override_settings(FIXTURE_PILOT=True)
    def test_live_legacy_source_and_internal_routes_cannot_bypass_pilot(self):
        candidate = '11111111-1111-1111-1111-111111111111'
        for path in ['/sessions/new/', '/sessions/1/upload/', '/sessions/1/confirm/',
                     f'/candidates/{candidate}/download/', f'/candidates/{candidate}/retry/',
                     f'/candidates/{candidate}/interview/', '/local-mail/',
                     f'/batches/{candidate}/uploads/', '/preferences/']:
            with self.subTest(path=path):
                response = self.call(path)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response['Cache-Control'], 'no-store')
                self.assertEqual(response['X-Frame-Options'], 'DENY')

    @override_settings(FIXTURE_PILOT=True)
    def test_authenticated_workflow_entry_and_results_still_reach_handlers(self):
        candidate = '11111111-1111-1111-1111-111111111111'
        for path in ['/', '/dashboard/', '/login/', '/register/', '/health/ready/',
                     '/batches/new/', '/batches/sample/', f'/batches/{candidate}/',
                     f'/candidates/{candidate}/', f'/candidates/{candidate}/evidence/',
                     f'/candidates/{candidate}/pdf/', '/account/cancel/']:
            with self.subTest(path=path): self.assertEqual(self.call(path).status_code, 200)

    @override_settings(FIXTURE_PILOT=False)
    def test_ordinary_profile_is_unchanged(self):
        self.assertEqual(self.call('/sessions/new/').status_code, 200)


@override_settings(FIXTURE_PILOT=True, LOCAL_DEVELOPMENT=False,
    AUTOMATIC_WORKFLOW_ENABLED=True, AUTOMATIC_TRANSPORT='fixtures',
    AUTOMATIC_REMOTE_EXECUTOR='', AI_MAX_DAILY_REQUESTS=0, AI_API_KEY='',
    LIFECYCLE_EXECUTION_ENABLED=False, EMAIL_HOST='smtp.example.test',
    EMAIL_HOST_USER='synthetic', EMAIL_HOST_PASSWORD='synthetic',
    AUTOMATIC_MIN_FREE_BYTES=0)
class PilotReadinessTests(TestCase):
    def setUp(self):
        from django.conf import settings
        self.tmp = tempfile.TemporaryDirectory(dir=settings.BASE_DIR.parent/'.local')
        self.addCleanup(self.tmp.cleanup)
        self.root_override = override_settings(PRIVATE_ROOT=Path(self.tmp.name))
        self.root_override.enable(); self.addCleanup(self.root_override.disable)

    def ready(self):
        from recruiting.health import ready
        return ready(RequestFactory().get('/health/ready/'))

    def test_requires_fresh_dispatcher_heartbeat_without_local_scanner_calls(self):
        from recruiting.models import ServiceHeartbeat
        self.assertEqual(self.ready().status_code, 503)
        heartbeat = ServiceHeartbeat.objects.create(service='cloud_dispatcher')
        with patch('recruiting.scanning.scanner_ready', side_effect=AssertionError('local scanner')):
            self.assertEqual(self.ready().status_code, 200)
        heartbeat.seen_at = timezone.now()-timedelta(minutes=2); heartbeat.save()
        self.assertEqual(self.ready().status_code, 503)

    def test_fail_closed_on_live_budget_transport_storage_or_mail_misconfiguration(self):
        from recruiting.models import ServiceHeartbeat
        ServiceHeartbeat.objects.create(service='cloud_dispatcher')
        for key, value in [('AI_MAX_DAILY_REQUESTS',1), ('AI_API_KEY','synthetic-unused'),
                           ('AUTOMATIC_TRANSPORT','remote'), ('EMAIL_HOST',''),
                           ('LIFECYCLE_EXECUTION_ENABLED',True), ('LOCAL_DEVELOPMENT',True),
                           ('PRIVATE_ROOT',Path(self.tmp.name)/'missing')]:
            with self.subTest(key=key), override_settings(**{key:value}):
                response = self.ready()
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.content, b'{"status": "not_ready"}')
