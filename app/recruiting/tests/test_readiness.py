import tempfile
from pathlib import Path
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.utils import timezone
from recruiting.models import ServiceHeartbeat

class ReadinessTests(TestCase):
    def test_existing_socket_does_not_prove_parser_or_scanner_is_ready(self):
        ServiceHeartbeat.objects.create(service='worker',seen_at=timezone.now())
        with patch('recruiting.health.Path.exists',return_value=True):
            with override_settings(LOCAL_DEVELOPMENT=False,PARSER_SOCKET='/run/parser/parser.sock'):
                self.assertEqual(self.client.get('/health/ready/').status_code,503)
