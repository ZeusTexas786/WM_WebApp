import tempfile
from pathlib import Path
from unittest.mock import patch
from django.test import TransactionTestCase,SimpleTestCase,override_settings
from django.contrib.auth.models import User
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile
from recruiting.models import Profile,HiringSession,Candidate

class ClamSocket:
    def __init__(self,response): self.response=response; self.sent=b''
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def sendall(self,data): self.sent+=data
    def recv(self,size): result,self.response=self.response[:size],self.response[size:]; return result
    def settimeout(self,value): pass

class ScannerTests(SimpleTestCase):
    def test_clean_infected_and_invalid_protocol_results(self):
        from recruiting.scanning import scan_stream
        import io
        for response,expected in [(b'stream: OK\0','clean'),(b'stream: Eicar-Test-Signature FOUND\0','infected'),(b'wrong reply\0','unavailable')]:
            fake=ClamSocket(response)
            with patch('socket.create_connection',return_value=fake): result=scan_stream(io.BytesIO(b'synthetic data'),'scanner',3310)
            self.assertEqual(result,expected)
            self.assertTrue(fake.sent.startswith(b'zINSTREAM\0'))
            self.assertTrue(fake.sent.endswith(b'\0\0\0\0'))

    def test_scanner_outage_and_timeout_fail_closed(self):
        from recruiting.scanning import scan_stream
        import io
        for failure in [ConnectionRefusedError,TimeoutError]:
            with patch('socket.create_connection',side_effect=failure):
                self.assertEqual(scan_stream(io.BytesIO(b'text'),'scanner',3310),'unavailable')

class UploadSecurityTests(TransactionTestCase):
    def setUp(self):
        from django.conf import settings
        root=settings.BASE_DIR.parent/'.local'/'tests'; root.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=root); self.addCleanup(self.tmp.cleanup)
        self.override=override_settings(PRIVATE_ROOT=Path(self.tmp.name)); self.override.enable(); self.addCleanup(self.override.disable)
        self.user=User.objects.create_user('secure-upload'); Profile.objects.create(user=self.user,verified_at=timezone.now())
        self.hiring=HiringSession.objects.create(owner=self.user,title='Role',jd='Python',confirmed_at=timezone.now(),rubric_version=1,rubric=[{'id':'c1','name':'Python','weight':1}])
        self.client.force_login(self.user)

    @override_settings(MAX_ACCOUNT_STORAGE_BYTES=30)
    def test_retained_storage_quota_enforced(self):
        from recruiting.uploads import store_upload
        with self.assertRaises(ValueError): store_upload(self.user,self.hiring,SimpleUploadedFile('resume.txt',b'A resume with more than thirty bytes of text content.'))
        self.assertEqual(Candidate.objects.count(),0)

    @override_settings(LOCAL_DEVELOPMENT=False,SCANNER_HOST='scanner')
    def test_unscanned_file_cannot_be_downloaded(self):
        from recruiting.uploads import store_upload
        candidate=store_upload(self.user,self.hiring,SimpleUploadedFile('resume.txt',b'Built Python APIs in production.'))
        response=self.client.get(f'/candidates/{candidate.pk}/download/'); response.close()
        self.assertEqual(response.status_code,403)

    @override_settings(LOCAL_DEVELOPMENT=False,SCANNER_HOST='scanner')
    def test_worker_does_not_parse_when_scanning_fails(self):
        from recruiting.uploads import admit_upload
        from django.core.management import call_command
        from recruiting.models import Job
        candidate=admit_upload(self.user,self.hiring,SimpleUploadedFile('resume.txt',b'Built Python APIs in production.'))
        with patch('recruiting.scanning.scan_path',return_value='unavailable'),patch('recruiting.parser.parse_remote',side_effect=AssertionError('Parser must not run')):
            call_command('worker',once=True)
        job=Job.objects.get(candidate=candidate)
        self.assertEqual(job.status,'failed'); self.assertFalse(job.reserved)

class ParserBoundaryTests(SimpleTestCase):
    def test_remote_parser_never_falls_back_to_same_user_process(self):
        from recruiting.parser import parse_remote
        with patch('socket.socket',side_effect=OSError),self.assertRaises(ValueError): parse_remote('unused.txt','.txt','/missing/parser.sock')
