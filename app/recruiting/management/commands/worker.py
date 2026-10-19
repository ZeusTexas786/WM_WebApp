import time
import uuid
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections, transaction
from recruiting.jobs import claim_job, complete_job, fail_job,claim_interview,complete_interview
from recruiting.models import Candidate, Job,ServiceHeartbeat
from django.utils import timezone
from recruiting.parser import parse_bounded

class Command(BaseCommand):
    help = 'Process private resume jobs. Run one worker locally; --once handles one pending job.'
    def add_arguments(self,parser): parser.add_argument('--once',action='store_true')
    def handle(self,*args,**options):
        if options['once']: return self.run_loop(*args,**options)
        from recruiting.heartbeats import service_heartbeat
        with service_heartbeat('worker'): return self.run_loop(*args,**options)

    def run_loop(self,*args,**options):
        worker_id=uuid.uuid4().hex
        while True:
            close_old_connections()
            ServiceHeartbeat.objects.update_or_create(service='worker',defaults={'seen_at':timezone.now()})
            from recruiting.roles import process_one_role
            if process_one_role():
                if options['once']: break
                continue
            job=claim_job(worker_id)
            if job:
                try:
                    candidate=job.candidate
                    root=Path(settings.PRIVATE_ROOT).resolve(); path=(root/candidate.storage_name).resolve()
                    if not path.is_relative_to(root): raise ValueError('Invalid private file path.')
                    if settings.LOCAL_DEVELOPMENT:
                        blocks=parse_bounded(str(path),path.suffix)
                    else:
                        from recruiting.scanning import scan_path
                        Candidate.objects.filter(pk=candidate.pk).update(scan_status='scanning')
                        scan=scan_path(path)
                        Candidate.objects.filter(pk=candidate.pk).update(scan_status=scan)
                        if scan!='clean':
                            if scan=='infected':
                                path.unlink(missing_ok=True)
                                Candidate.objects.filter(pk=candidate.pk).update(byte_size=0)
                            raise ValueError('Document security scan did not pass. Processing is blocked; retry only after scanner availability is restored or submit a safe document.')
                        from recruiting.parser import parse_remote
                        blocks=parse_remote(str(path),path.suffix,settings.PARSER_SOCKET)
                    Candidate.objects.filter(pk=candidate.pk,job__status='processing',job__lease_token=job.lease_token).update(blocks=blocks)
                    candidate.blocks=blocks
                    if candidate.session.mode=='sample':
                        from recruiting.samples import sample_assessment
                        result=sample_assessment(candidate,job.rubric)
                    else:
                        from recruiting.providers import analyze_candidate
                        result=analyze_candidate(job.jd,job.rubric,blocks)
                        result['sample']=False
                    complete_job(job.pk,result,job.lease_token)
                    self.stdout.write(f'Job {job.pk}: completed')
                except (ValueError,RuntimeError) as exc:
                    fail_job(job.pk,str(exc),job.lease_token)
                    self.stdout.write(f'Job {job.pk}: failed safely')
                except Exception:
                    fail_job(job.pk,'Processing interrupted. Retry or inspect the local worker configuration.',job.lease_token)
                    self.stdout.write(f'Job {job.pk}: unexpected processing error')
            else:
                # Interview generation has its own claim, separate from trial settlement.
                interview=claim_interview()
                if interview:
                    try:
                        from recruiting.interviews import generate_interview_pack
                        result=generate_interview_pack(interview.candidate.session,interview.candidate)
                        complete_interview(interview.pk,result,interview.interview_lease_token)
                    except (ValueError,RuntimeError) as exc:
                        complete_interview(interview.pk,None,interview.interview_lease_token,str(exc))
                    except Exception:
                        complete_interview(interview.pk,None,interview.interview_lease_token,'Interview generation interrupted. Retry later.')
                elif not options['once']:
                    time.sleep(1)
            if options['once']: break
