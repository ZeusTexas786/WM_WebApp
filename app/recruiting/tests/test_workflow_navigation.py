from django.test import SimpleTestCase, RequestFactory, override_settings
from django.template.loader import render_to_string
from types import SimpleNamespace


class WorkflowNavigationTests(SimpleTestCase):
    def navigation(self):
        request = RequestFactory().get('/dashboard/')
        request.user = SimpleNamespace(is_authenticated=True, email='preview@example.test')
        return render_to_string('recruiting/base.html', {}, request=request)

    @override_settings(AUTOMATIC_WORKFLOW_ENABLED=True)
    def test_automatic_navigation_reaches_new_intake(self):
        self.assertIn('href="/batches/new/"', self.navigation())

    @override_settings(AUTOMATIC_WORKFLOW_ENABLED=False)
    def test_legacy_navigation_is_preserved_when_automatic_flow_is_off(self):
        self.assertIn('href="/sessions/new/"', self.navigation())

    @override_settings(FIXTURE_PILOT=True, LOCAL_DEVELOPMENT=False)
    def test_production_pilot_does_not_claim_to_be_local_preview(self):
        response = self.client.get('/login/')
        self.assertContains(response, 'SYNTHETIC PILOT')
        self.assertNotContains(response, 'LOCAL PROTOTYPE')
        self.assertNotContains(response, 'Local preview')

    @override_settings(AI_API_KEY='synthetic-disabled-key', AI_MAX_DAILY_REQUESTS=0)
    def test_disabled_api_budget_does_not_advertise_configured_ai(self):
        response = self.client.get('/login/')
        self.assertContains(response, 'Sample mode available')
        self.assertNotContains(response, 'AI configured')
