from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from django.db import connection,close_old_connections
from django.test import TransactionTestCase
from django.contrib.auth.models import User
from django.utils import timezone
from recruiting.models import Profile,HiringSession,Candidate,Job

@skipUnless(connection.vendor=='postgresql','Requires real PostgreSQL; SQLite is not portability evidence.')
class PostgresConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.user=User.objects.create_user('postgres-test')
        self.profile=Profile.objects.create(user=self.user,verified_at=timezone.now())
        self.hiring=HiringSession.objects.create(owner=self.user,title='Role',jd='Python',confirmed_at=timezone.now(),rubric_version=1,rubric=[{'id':'c1','name':'Python','weight':1}])
        self.items=[Candidate.objects.create(session=self.hiring,filename=f'{i}.txt',storage_name=f'{i}.txt',digest=str(i),rubric_version=1) for i in range(2)]

    def parallel(self,function,values,synchronize_table=None):
        barrier=Barrier(len(values))
        read_barrier=Barrier(len(values))
        def invoke(value):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                synchronized=False
                def align_unlocked_reads(execute,sql,params,many,context):
                    nonlocal synchronized
                    result=execute(sql,params,many,context)
                    # Force the unsafe interleaving only for ordinary reads. A real
                    # SELECT FOR UPDATE must retain its natural blocking semantics.
                    if synchronize_table and not synchronized and sql.lstrip().upper().startswith('SELECT') and f'FROM "{synchronize_table}"' in sql and 'FOR UPDATE' not in sql.upper():
                        synchronized=True
                        read_barrier.wait(timeout=10)
                    return result
                with connection.execute_wrapper(align_unlocked_reads): return function(value)
            finally: close_old_connections()
        with ThreadPoolExecutor(max_workers=len(values)) as pool: return list(pool.map(invoke,values))

    def test_final_credit_is_reserved_only_once(self):
        from recruiting.jobs import enqueue
        self.profile.completed_credits=29; self.profile.save()
        def admit(pk):
            try: enqueue(Candidate.objects.get(pk=pk),1); return True
            except ValueError: return False
        results=self.parallel(admit,[c.pk for c in self.items],'recruiting_profile')
        self.assertEqual(sum(results),1)
        self.assertEqual(Job.objects.filter(reserved=True).count(),1)

    def test_workers_never_claim_the_same_job(self):
        from recruiting.jobs import enqueue,claim_job
        for item in self.items: enqueue(item,1)
        results=self.parallel(lambda worker:claim_job(worker).pk,['one','two'],'recruiting_job')
        self.assertEqual(len(set(results)),2)

    def test_parallel_completions_preserve_both_credits(self):
        from recruiting.jobs import enqueue,claim_job,complete_job
        for item in self.items: enqueue(item,1)
        jobs=[claim_job('one'),claim_job('two')]
        self.parallel(lambda j:complete_job(j.pk,{'alignment':50},j.lease_token),jobs,'recruiting_profile')
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.completed_credits,2)
        self.assertEqual(Job.objects.filter(status='completed').count(),2)

    def test_simultaneous_success_settles_one_job_once(self):
        from recruiting.jobs import enqueue,claim_job,complete_job
        enqueue(self.items[0],1); job=claim_job('one')
        self.parallel(lambda _:complete_job(job.pk,{'alignment':50},job.lease_token),[1,2])
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.completed_credits,1)

    def test_cancel_and_complete_do_not_leave_a_reserved_terminal_job(self):
        from recruiting.jobs import enqueue,claim_job,complete_job,cancel_job
        enqueue(self.items[0],1); job=claim_job('one')
        self.parallel(lambda value: complete_job(job.pk,{'alignment':50},job.lease_token) if value else cancel_job(job.pk,self.user),[True,False])
        job.refresh_from_db(); self.profile.refresh_from_db()
        self.assertFalse(job.reserved)
        self.assertEqual(self.profile.completed_credits,1 if job.status=='completed' else 0)

    def test_daily_usage_cap_is_atomic(self):
        from recruiting.coordination import reserve_api_request
        results=self.parallel(lambda _:reserve_api_request(1),[1,2])
        self.assertEqual(sum(results),1)
