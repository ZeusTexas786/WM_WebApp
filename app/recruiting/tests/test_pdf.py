import io
from django.test import TestCase
from django.contrib.auth.models import User
from django.utils import timezone
from recruiting.models import Profile,HiringSession,Candidate,Job
from recruiting.samples import SAMPLES,sample_assessment
from pypdf import PdfReader

class PdfTests(TestCase):
    def test_download_is_private_and_one_page_for_long_brief(self):
        user=User.objects.create_user('pdf'); Profile.objects.create(user=user,verified_at=timezone.now())
        hiring=HiringSession.objects.create(owner=user,title='Senior Python Engineer',jd='Python')
        name,headline,paragraphs,states=SAMPLES[0]
        blocks=[{'id':f'b{i+1}','location':f'section {i+1}','text':p} for i,p in enumerate(paragraphs)]
        candidate=Candidate.objects.create(session=hiring,filename='Alex_Morgan.txt',storage_name='unused',digest='a',rubric_version=1,blocks=blocks)
        criteria=[{'id':f'c{i+1}','name':('Python engineering and reliable services ' * 5)[:200],'weight':1} for i in range(4)]
        result=sample_assessment(candidate,criteria)
        for e in result['evaluations']: e['explanation']='A detailed explanation of the experience and supporting evidence. '*10
        Job.objects.create(candidate=candidate,rubric=criteria,jd='Python',status='completed',reserved=False,result=result)
        self.assertEqual(self.client.get(f'/candidates/{candidate.pk}/pdf/').status_code,302)
        self.client.force_login(user)
        response=self.client.get(f'/candidates/{candidate.pk}/pdf/')
        self.assertEqual(response.status_code,200)
        reader=PdfReader(io.BytesIO(response.content))
        self.assertEqual(len(reader.pages),1)
        self.assertIn('Alex Morgan',reader.pages[0].extract_text())
        self.assertIn('Condensed',reader.pages[0].extract_text())
