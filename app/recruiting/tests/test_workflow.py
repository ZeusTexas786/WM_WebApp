import tempfile
from pathlib import Path
from django.test import TransactionTestCase, override_settings
from django.contrib.auth.models import User
from django.utils import timezone
from recruiting.models import Profile, HiringSession, Job

class WorkflowTests(TransactionTestCase):
    @override_settings(AI_API_KEY='')
    def test_unconfigured_live_worker_fails_without_charging_or_sample_fallback(self):
        from django.core.management import call_command
        from recruiting.uploads import admit_upload
        from django.core.files.uploadedfile import SimpleUploadedFile
        hiring=HiringSession.objects.create(owner=self.user,title='Live',jd='Python',mode='live',confirmed_at=timezone.now(),rubric_version=1,rubric=[{'id':'c1','name':'Python','weight':1}])
        candidate=admit_upload(self.user,hiring,SimpleUploadedFile('resume.txt',b'Built Python APIs in a production backend.'))
        call_command('worker',once=True)
        job=Job.objects.get(candidate=candidate)
        self.assertEqual(job.status,'failed')
        self.assertEqual(job.result,{})
        self.assertFalse(job.reserved)
        self.assertEqual(Profile.objects.get(user=self.user).completed_credits,0)
    def setUp(self):
        from django.conf import settings
        root = settings.BASE_DIR.parent/'.local'/'tests'; root.mkdir(parents=True,exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=root); self.addCleanup(self.tmp.cleanup)
        self.setting = override_settings(PRIVATE_ROOT=Path(self.tmp.name)); self.setting.enable(); self.addCleanup(self.setting.disable)
        self.user = User.objects.create_user('workflow@example.com')
        Profile.objects.create(user=self.user,verified_at=timezone.now())
        self.client.force_login(self.user)

    def test_synthetic_sample_runs_through_worker_and_report(self):
        self.client.post('/sessions/new/',{'title':'Python Engineer','jd':'Python SQL tests cloud','mode':'sample'})
        hiring = HiringSession.objects.get()
        self.client.post(f'/sessions/{hiring.pk}/confirm/',{'names':['Python backend development','PostgreSQL and data modelling','Automated testing and reliability','Cloud deployment and operations'],'weights':['3','2','2','1']})
        response = self.client.post(f'/sessions/{hiring.pk}/sample/')
        self.assertEqual(response.status_code,302)
        from django.core.management import call_command
        call_command('worker', once=True)
        job = Job.objects.filter(status='completed').first()
        self.assertIsNotNone(job)
        self.assertTrue(job.result['sample'])
        self.assertContains(self.client.get(f'/candidates/{job.candidate_id}/'), 'Synthetic sample')
        self.assertContains(self.client.get(f'/candidates/{job.candidate_id}/evidence/'), 'Python')
        profile = Profile.objects.get(user=self.user)
        self.assertEqual(profile.completed_credits,1)

    def test_sample_mode_refuses_real_resume_uploads(self):
        self.client.post('/sessions/new/',{'title':'Sample','jd':'Python','mode':'sample'})
        hiring = HiringSession.objects.get()
        from django.core.files.uploadedfile import SimpleUploadedFile
        response = self.client.post(f'/sessions/{hiring.pk}/upload/',{'files':[SimpleUploadedFile('real.txt',b'Real private resume should not get a sample report.')]})
        self.assertEqual(response.status_code,400)

    def test_report_and_status_are_owner_scoped(self):
        other=User.objects.create_user('other'); Profile.objects.create(user=other,verified_at=timezone.now())
        hiring=HiringSession.objects.create(owner=other,title='Private',jd='Python')
        self.assertEqual(self.client.get(f'/sessions/{hiring.pk}/status/').status_code,404)
