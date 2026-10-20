import json
import os
import subprocess
import sys
from pathlib import Path
from django.test import SimpleTestCase

ROOT=Path(__file__).resolve().parents[3]

class DeploymentContractTests(SimpleTestCase):
    def test_local_preview_cannot_inherit_paid_ai_or_outbound_mail(self):
        env=os.environ.copy()
        env.update(DJANGO_SETTINGS_MODULE='config.preview',WEBMONKIE_LOCAL='0',
            AI_API_KEY='fake-inherited-key',AI_MAX_DAILY_REQUESTS='123',
            SMTP_HOST='smtp.invalid',SMTP_USER='fake-user',SMTP_PASSWORD='fake-password',
            AUTOMATIC_TRANSPORT='remote',AUTOMATIC_REMOTE_EXECUTOR='https://invalid.test',
            WEBMONKIE_PREVIEW_PORT='8123')
        check="""import config.preview as s
assert s.LOCAL_DEVELOPMENT and s.AUTOMATIC_WORKFLOW_ENABLED
assert s.AUTOMATIC_TRANSPORT == 'fixtures' and not s.AUTOMATIC_REMOTE_EXECUTOR
assert s.AI_MAX_DAILY_REQUESTS == 0 and not s.AI_API_KEY
assert not s.EMAIL_HOST and not s.EMAIL_HOST_USER and not s.EMAIL_HOST_PASSWORD
assert not s.LIFECYCLE_EXECUTION_ENABLED
assert s.PUBLIC_ORIGIN == 'http://127.0.0.1:8123'
"""
        result=subprocess.run([sys.executable,'-c',check],cwd=ROOT/'app',env=env,
            capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_fixture_pilot_is_standalone_bounded_and_loopback_only(self):
        config=json.loads((ROOT/'deploy/compose.pilot.json').read_text())
        services=config['services']
        self.assertEqual(set(services),{'web','dispatcher','database'})
        self.assertEqual(services['web']['ports'],['127.0.0.1:8089:8000'])
        self.assertLessEqual(sum(int(s['mem_limit'].rstrip('m')) for s in services.values()),544)
        for name,service in services.items():
            if name!='web': self.assertNotIn('ports',service)
            self.assertNotIn('/var/run/docker.sock',str(service))
        for name in ('web','dispatcher'):
            service=services[name]
            self.assertEqual(service['environment']['DJANGO_SETTINGS_MODULE'],'config.pilot')
            self.assertEqual(service['environment']['AI_MAX_DAILY_REQUESTS'],'0')
            self.assertEqual(service['environment']['AUTOMATIC_TRANSPORT'],'fixtures')
            self.assertTrue(service['read_only'])
            self.assertEqual(service['cap_drop'],['ALL'])
        command=services['web']['command']
        self.assertEqual(command[command.index('--workers')+1],'1')
        self.assertEqual(services['dispatcher']['command'][:3],['python','manage.py','cloud_dispatcher'])
        self.assertNotEqual(config['name'],'webmonkie')

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
