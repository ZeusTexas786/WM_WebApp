import tempfile
from pathlib import Path
from io import BytesIO
from zipfile import ZipFile
from django.test import TestCase, override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.auth.models import User
from django.utils import timezone
from recruiting.models import Profile, HiringSession

class UploadTests(TestCase):
    def test_expired_admission_leaves_no_file_or_candidate(self):
        from datetime import timedelta
        from recruiting.models import Candidate
        Profile.objects.filter(user=self.user).update(verified_at=timezone.now()-timedelta(days=4))
        response=self.client.post(f'/sessions/{self.session.pk}/upload/',{'files':[self.file()]})
        self.assertEqual(response.status_code,302)
        self.assertEqual(Candidate.objects.count(),0)
        self.assertEqual(len(list(Path(self.tmp.name).rglob('*.txt'))),0)
    def setUp(self):
        from django.conf import settings
        test_root = settings.BASE_DIR.parent / '.local' / 'tests'
        test_root.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=test_root)
        self.addCleanup(self.tmp.cleanup)
        self.settings_override = override_settings(PRIVATE_ROOT=Path(self.tmp.name))
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.user = User.objects.create_user('owner@example.com')
        Profile.objects.create(user=self.user, verified_at=timezone.now())
        self.session = HiringSession.objects.create(owner=self.user, title='Role', jd='Python', confirmed_at=timezone.now(), rubric_version=1, rubric=[{'id':'c1','name':'Python','weight':1}])
        self.client.force_login(self.user)

    def file(self, name='resume.txt', content=b'Built Python APIs and automated tests for production services.'):
        return SimpleUploadedFile(name, content)

    def test_private_upload_duplicate_names_and_deduplication(self):
        from recruiting.uploads import store_upload
        first = store_upload(self.user, self.session, self.file())
        duplicate = store_upload(self.user, self.session, self.file())
        second = store_upload(self.user, self.session, self.file(content=b'Different Python experience.'))
        self.assertEqual(first.pk, duplicate.pk)
        self.assertNotEqual(first.storage_name, second.storage_name)
        self.assertTrue((Path(self.tmp.name)/first.storage_name).is_file())

    def test_foreign_upload_and_download_rejected(self):
        from recruiting.uploads import store_upload
        candidate = store_upload(self.user, self.session, self.file())
        other = User.objects.create_user('other@example.com')
        Profile.objects.create(user=other, verified_at=timezone.now())
        self.client.force_login(other)
        self.assertEqual(self.client.get(f'/candidates/{candidate.pk}/download/').status_code, 404)
        self.assertEqual(self.client.post(f'/sessions/{self.session.pk}/upload/', {'files': [self.file()]}).status_code, 404)

    def test_invalid_empty_oversized_and_signature(self):
        from recruiting.uploads import store_upload
        for upload in [self.file('bad.exe'), self.file(content=b''), self.file(content=b'x'*(10*1024*1024+1)), self.file('fake.pdf'), self.file('fake.docx')]:
            with self.subTest(upload=upload.name), self.assertRaises(ValueError):
                store_upload(self.user, self.session, upload)

    def test_31_files_rejected(self):
        # Multipart parser rejects oversized batches before any file is stored.
        response = self.client.post(f'/sessions/{self.session.pk}/upload/', {'files': [self.file(f'{i}.txt') for i in range(31)]})
        self.assertEqual(response.status_code, 400)

    def test_corrupt_and_empty_pdf_and_docx_expansion(self):
        from recruiting.parser import extract_text
        bad = Path(self.tmp.name)/'bad.pdf'
        bad.write_bytes(b'%PDF-1.7 corrupt')
        with self.assertRaises(ValueError): extract_text(str(bad), '.pdf')
        from pypdf import PdfWriter
        writer = PdfWriter(); writer.add_blank_page(width=100, height=100)
        with bad.open('wb') as stream: writer.write(stream)
        with self.assertRaises(ValueError): extract_text(str(bad), '.pdf')
        docx = Path(self.tmp.name)/'bad.docx'
        with ZipFile(docx, 'w') as archive:
            archive.writestr('word/document.xml', 'x'*(21*1024*1024))
        with self.assertRaises(ValueError): extract_text(str(docx), '.docx')

    def test_parser_extracts_stable_blocks_and_times_out(self):
        from recruiting.parser import extract_text, parse_bounded
        path = Path(self.tmp.name)/'resume.txt'; path.write_text('Built Python APIs.\n\nDeployed PostgreSQL.', encoding='utf-8')
        blocks = extract_text(str(path), '.txt')
        self.assertEqual(blocks[0]['id'], 'b1')
        self.assertEqual(blocks[1]['text'], 'Deployed PostgreSQL.')
        self.assertEqual(parse_bounded(str(path), '.txt')[0]['text'], 'Built Python APIs.')
        with self.assertRaises(ValueError): parse_bounded(str(path), '.txt', timeout=0.00001)
