import base64
import tempfile
from pathlib import Path
from datetime import timedelta
from django.test import TestCase,override_settings
from django.contrib.auth.models import User
from django.utils import timezone
from unittest.mock import patch
from recruiting.models import Profile,HiringSession,Candidate

class ArchiveTests(TestCase):
    def test_archive_is_authenticated_and_restores_files_and_metadata(self):
        from recruiting.archives import archive_account,restore_account,archive_path
        now=timezone.now(); user=User.objects.create_user('archive@example.test')
        profile=Profile.objects.create(user=user,paid=True,status='archiving',last_activity_at=now-timedelta(days=200))
        hiring=HiringSession.objects.create(owner=user,title='Private role',jd='Private job description')
        name=f'{user.pk}/{hiring.pk}/sample.txt'
        candidate=Candidate.objects.create(session=hiring,filename='Original.txt',storage_name=name,digest='x',rubric_version=0,blocks=[{'id':'b1','text':'Private source'}])
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[3]/'.local') as folder:
            with override_settings(PRIVATE_ROOT=Path(folder),ARCHIVE_KEY=base64.urlsafe_b64encode(b't'*32).decode(),LOCAL_DEVELOPMENT=False,LIFECYCLE_EXECUTION_ENABLED=True):
                path=Path(folder)/name; path.parent.mkdir(parents=True); path.write_text('Synthetic document')
                archive_account(profile.pk); profile.refresh_from_db()
                self.assertEqual(profile.status,'archived'); self.assertFalse(path.exists())
                candidate.refresh_from_db(); self.assertEqual(candidate.blocks,[])
                encrypted=archive_path(profile).read_bytes()
                self.assertNotIn(b'Private source',encrypted)
                restore_account(profile.pk)
                self.assertEqual(path.read_text(),'Synthetic document')
                candidate.refresh_from_db(); self.assertEqual(candidate.filename,'Original.txt')
                self.assertEqual(candidate.blocks[0]['text'],'Private source')

    def test_snapshot_survives_crash_after_plaintext_cleanup(self):
        from recruiting.archives import archive_account,restore_account,archive_path
        import shutil
        user=User.objects.create_user('crash@example.test')
        profile=Profile.objects.create(user=user,paid=True,status='archiving')
        hiring=HiringSession.objects.create(owner=user,title='Private',jd='Description')
        name=f'{user.pk}/{hiring.pk}/source.txt'
        Candidate.objects.create(session=hiring,filename='source.txt',storage_name=name,digest='x',rubric_version=0)
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[3]/'.local') as folder:
            with override_settings(PRIVATE_ROOT=Path(folder),ARCHIVE_KEY=base64.urlsafe_b64encode(b't'*32).decode(),LOCAL_DEVELOPMENT=False,LIFECYCLE_EXECUTION_ENABLED=True):
                path=Path(folder)/name; path.parent.mkdir(parents=True); path.write_text('Only surviving copy')
                original=shutil.rmtree
                def interrupted(target,*args,**kwargs):
                    result=original(target,*args,**kwargs)
                    if Path(target)==Path(folder)/str(user.pk): raise OSError('Injected crash point')
                    return result
                with patch('recruiting.archives.shutil.rmtree',side_effect=interrupted):
                    with self.assertRaises(OSError): archive_account(profile.pk)
                profile.refresh_from_db()
                self.assertTrue(profile.archive_name); self.assertTrue(archive_path(profile).exists()); self.assertFalse(path.exists())
                archive_account(profile.pk); restore_account(profile.pk)
                self.assertEqual(path.read_text(),'Only surviving copy')

    def test_oversized_metadata_is_rejected_before_source_removal(self):
        from recruiting.archives import archive_account
        user=User.objects.create_user('large@example.test')
        profile=Profile.objects.create(user=user,paid=True,status='archiving')
        hiring=HiringSession.objects.create(owner=user,title='Role',jd='x'*100)
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[3]/'.local') as folder:
            with override_settings(PRIVATE_ROOT=Path(folder),ARCHIVE_KEY=base64.urlsafe_b64encode(b't'*32).decode(),LOCAL_DEVELOPMENT=False,LIFECYCLE_EXECUTION_ENABLED=True):
                with patch('recruiting.archives.METADATA_LIMIT',10,create=True):
                    with self.assertRaises(ValueError): archive_account(profile.pk)
                hiring.refresh_from_db(); self.assertEqual(hiring.jd,'x'*100)

    def test_cancelled_archive_restoration_preserves_cancellation(self):
        from recruiting.archives import archive_account,restore_account
        user=User.objects.create_user('cancelled-archive@example.test')
        now=timezone.now()
        profile=Profile.objects.create(user=user,paid=True,status='archiving')
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[3]/'.local') as folder:
            with override_settings(PRIVATE_ROOT=Path(folder),ARCHIVE_KEY=base64.urlsafe_b64encode(b't'*32).decode(),LOCAL_DEVELOPMENT=False,LIFECYCLE_EXECUTION_ENABLED=True):
                archive_account(profile.pk)
                Profile.objects.filter(pk=profile.pk).update(cancelled_at=now)
                restore_account(profile.pk,allow_cancelled=True)
                profile.refresh_from_db(); self.assertEqual(profile.cancelled_at,now)
                self.assertEqual(profile.status,'active')

    def test_corrupted_archive_never_becomes_plaintext(self):
        from recruiting.archives import decrypt_stream,encrypt_stream
        import io
        with override_settings(ARCHIVE_KEY=base64.urlsafe_b64encode(b't'*32).decode()):
            encrypted=io.BytesIO(); encrypt_stream(io.BytesIO(b'private'),encrypted)
            for value in [encrypted.getvalue()[:-1],encrypted.getvalue()[:-16]+b'x'*16]:
                with self.assertRaises(ValueError): decrypt_stream(io.BytesIO(value),io.BytesIO())
