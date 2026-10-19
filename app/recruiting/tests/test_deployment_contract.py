import json
from pathlib import Path
from django.test import SimpleTestCase

ROOT=Path(__file__).resolve().parents[3]

class DeploymentContractTests(SimpleTestCase):
    def test_parser_has_no_network_secrets_or_source_mount(self):
        services=json.loads((ROOT/'deploy/compose.json').read_text())['services']
        parser=services['parser']
        self.assertEqual(parser['network_mode'],'none')
        self.assertTrue(parser['read_only'])
        self.assertEqual(parser['cap_drop'],['ALL'])
        self.assertNotIn('env_file',parser)
        self.assertEqual(parser['user'],'10001:10001')
        self.assertEqual(parser['volumes'],['parser_socket:/run/parser'])
        self.assertLessEqual(parser['pids_limit'],32)

    def test_only_loopback_gateway_has_published_ports(self):
        services=json.loads((ROOT/'deploy/compose.json').read_text())['services']
        for name,service in services.items():
            if name=='gateway': self.assertEqual(service['ports'],['127.0.0.1:8088:8080'])
            else: self.assertNotIn('ports',service)
            self.assertNotIn('/var/run/docker.sock',str(service))

    def test_build_context_excludes_private_state(self):
        rules=(ROOT/'.dockerignore').read_text().splitlines()
        self.assertEqual(rules[0],'**')
        for rule in ('!app/recruiting/**','!requirements-production.txt'): self.assertIn(rule,rules)
        self.assertNotIn('!.env',rules)

    def test_upload_spool_can_accept_supported_batch(self):
        services=json.loads((ROOT/'deploy/compose.json').read_text())['services']
        self.assertIn('/tmp:size=1536m,mode=1777',services['web']['tmpfs'])

    def test_database_bootstrap_does_not_use_app_superuser(self):
        services=json.loads((ROOT/'deploy/compose.json').read_text())['services']
        self.assertEqual(services['database']['environment']['POSTGRES_USER'],'postgres')
        self.assertIn('POSTGRES_ADMIN_PASSWORD',services['database']['environment']['POSTGRES_PASSWORD'])
