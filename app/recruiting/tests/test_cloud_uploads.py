"""Private upload behavior, quotas, ownership and zero-spend intake boundaries."""
import hashlib
import io
import json
import tempfile
from datetime import timedelta
from importlib import import_module
from pathlib import Path
from unittest.mock import patch
from unittest import skipUnless
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from django.db import connection, close_old_connections
from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from recruiting.models import Candidate, HiringSession, Profile
from recruiting.cloud import state
from recruiting.cloud.models import CloudBatch, CloudOutbox, UploadReservation


class UploadFixtures:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[3]/'.local')
        self.addCleanup(self.tmp.cleanup)
        self.settings = override_settings(PRIVATE_ROOT=Path(self.tmp.name), AUTOMATIC_WORKFLOW_ENABLED=True,
            AUTOMATIC_TRANSPORT='remote', AUTOMATIC_REMOTE_EXECUTOR='', AUTOMATIC_MIN_FREE_BYTES=0)
        self.settings.enable(); self.addCleanup(self.settings.disable)
        self.user = User.objects.create_user('upload-owner')
        Profile.objects.create(user=self.user, verified_at=timezone.now())
        self.session = HiringSession.objects.create(owner=self.user, title='Engineer', jd='Python')
        try:
            self.storage = import_module('recruiting.cloud.storage')
        except ModuleNotFoundError:
            self.fail('Private bounded resumable storage has not been implemented.')
        self.disk = self.storage.PrivateDiskStorage(self.user.pk)
        self.client.force_login(self.user)

    def reserve(self, content=b'Python resume', key='one', role='resume'):
        batch = state.create_batch(self.user.pk, self.session.pk, 'files')
        item = self.storage.reserve_uploads(batch.pk, self.user.pk, [{'client_key': key,
            'filename': key+'.txt', 'role': role, 'byte_size': len(content),
            'sha256': hashlib.sha256(content).hexdigest()}])[0]
        return batch, item

    def write(self, item, data=b'Python resume', offset=0):
        return self.disk.write_chunk(item, offset, io.BytesIO(data), len(data))


class CloudUploadTests(UploadFixtures, TestCase):
    def test_sequential_resume_duplicate_and_changed_chunk(self):
        batch, item = self.reserve()
        grant = self.disk.initiate_upload(item)
        self.assertEqual(grant.offset, 0)
        self.assertEqual(self.write(item, b'Python'), 6)
        self.assertEqual(self.write(item, b'Python'), 6)
        with self.assertRaises(ValueError): self.write(item, b'Pyth0n')
        with self.assertRaises(ValueError): self.write(item, b'x', 7)
        self.assertEqual(self.write(item, b' resume', 6), 13)
        result = self.disk.finalize_upload(item)
        self.assertEqual(result.byte_size, 13)
        item.refresh_from_db(); self.assertIsNotNone(item.uploaded_at)
        self.assertEqual(self.disk.finalize_upload(item), result)
        accepted = self.storage.accept_batch(batch.pk, self.user.pk)
        self.assertEqual(accepted.stage, 'validate')
        self.storage.accept_batch(batch.pk, self.user.pk)
        self.assertEqual(CloudOutbox.objects.filter(event_key='notification:acknowledgement').count(), 1)
        with self.assertRaises(ValueError): self.write(item)

    def test_actual_bytes_hash_size_and_bounded_reads(self):
        _, item = self.reserve()
        class Bounded(io.BytesIO):
            def read(inner, size=-1):
                self.assertGreater(size, 0); self.assertLessEqual(size, 65536)
                return super().read(size)
        with self.assertRaises(ValueError): self.disk.write_chunk(item, 0, Bounded(b'longer than declared input'), 13)
        self.assertEqual(self.disk.committed_offset(item), 0)
        with self.assertRaises(ValueError): self.disk.write_chunk(item, 0, Bounded(b'xx'), 13)
        self.assertEqual(self.disk.committed_offset(item), 0)
        with self.assertRaises(ValueError): self.disk.write_chunk(item, 0, Bounded(b'x'), 1024*1024+1)
        self.write(item, b'Xython resume')
        with self.assertRaises(ValueError): self.disk.finalize_upload(item)
        item.refresh_from_db(); self.assertIsNone(item.uploaded_at)

    def test_cross_owner_and_forged_storage_identity(self):
        _, item = self.reserve()
        stranger = User.objects.create_user('stranger')
        Profile.objects.create(user=stranger, verified_at=timezone.now())
        with self.assertRaises(ValueError): self.storage.PrivateDiskStorage(stranger.pk).initiate_upload(item)
        item.storage_key = '../../escape'
        with self.assertRaises(ValueError): self.disk.initiate_upload(item)
        item.refresh_from_db(); item.storage_backend = 'https://forged.invalid/bucket'
        with self.assertRaises(ValueError): self.disk.initiate_upload(item)

    def test_expiry_cleanup_deletes_only_scoped_incomplete_files(self):
        batch, item = self.reserve(); self.write(item, b'Python')
        unrelated = Path(self.tmp.name)/'keep'; unrelated.write_bytes(b'keep')
        CloudBatch.objects.filter(pk=batch.pk).update(expires_at=timezone.now()-timedelta(seconds=1))
        with self.assertRaises(ValueError): self.disk.initiate_upload(item)
        self.assertEqual(self.storage.cleanup_abandoned_uploads(), 1)
        batch.refresh_from_db(); self.assertEqual(batch.stage, 'expired')
        self.assertEqual(batch.reserved_credits, 0)
        self.assertFalse(UploadReservation.objects.filter(pk=item.pk).exists())
        self.assertEqual(unrelated.read_bytes(), b'keep')

    def test_expired_never_sealed_finalized_sources_are_removed_and_release_quota(self):
        batch,item=self.reserve(); self.write(item); self.disk.finalize_upload(item)
        final=Path(self.tmp.name)/item.storage_key
        self.assertTrue(final.exists())
        CloudBatch.objects.filter(pk=batch.pk).update(expires_at=timezone.now()-timedelta(seconds=1))
        self.assertEqual(self.storage.cleanup_abandoned_uploads(),1)
        self.assertFalse(final.exists())
        self.assertFalse(UploadReservation.objects.filter(pk=item.pk).exists())
        with override_settings(MAX_ACCOUNT_STORAGE_BYTES=13,AUTOMATIC_PROJECT_STORAGE_BYTES=13):
            self.reserve(key='replacement')

    def test_staging_cleanup_preserves_accepted_inputs_even_after_expiry(self):
        batch,item=self.reserve(); self.write(item); self.disk.finalize_upload(item)
        self.storage.accept_batch(batch.pk,self.user.pk)
        CloudBatch.objects.filter(pk=batch.pk).update(expires_at=timezone.now()-timedelta(seconds=1))
        self.assertEqual(self.storage.cleanup_abandoned_uploads(),0)
        self.assertTrue((Path(self.tmp.name)/item.storage_key).exists())
        self.assertTrue(UploadReservation.objects.filter(pk=item.pk).exists())

    def test_abandoned_finalized_source_keeps_declaration_when_deletion_fails(self):
        batch,item=self.reserve(); self.write(item); self.disk.finalize_upload(item)
        CloudBatch.objects.filter(pk=batch.pk).update(expires_at=timezone.now()-timedelta(seconds=1))
        with patch.object(Path,'unlink',side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError): self.storage.cleanup_abandoned_uploads()
        self.assertTrue(UploadReservation.objects.filter(pk=item.pk).exists())
        self.assertTrue((Path(self.tmp.name)/item.storage_key).exists())

    def test_cancelled_never_sealed_finalized_sources_are_cleaned(self):
        batch,item=self.reserve(); self.write(item); self.disk.finalize_upload(item)
        Profile.objects.filter(user=self.user).update(cancelled_at=timezone.now())
        self.assertEqual(self.storage.cleanup_abandoned_uploads(),1)
        batch.refresh_from_db()
        self.assertEqual(batch.stage,'cancelled'); self.assertEqual(batch.reserved_credits,0)
        self.assertFalse((Path(self.tmp.name)/item.storage_key).exists())
        self.assertFalse(UploadReservation.objects.filter(pk=item.pk).exists())

    @override_settings(AUTOMATIC_REMOTE_EXECUTOR='recruiting.tests.test_cloud_uploads.ReadyExecutor')
    def test_cancel_finished_unsealed_upload_is_idempotent_and_removes_actual_bytes(self):
        batch,item=self.reserve(); self.write(item); self.disk.finalize_upload(item)
        url=f'/batches/{batch.pk}/uploads/{item.pk}/cancel/'
        first=self.client.post(url,data='{}',content_type='application/json')
        self.assertEqual(first.status_code,200)
        self.assertFalse((Path(self.tmp.name)/item.storage_key).exists())
        self.assertFalse(UploadReservation.objects.filter(pk=item.pk).exists())
        self.assertEqual(self.client.post(url,data='{}',content_type='application/json').status_code,200)
        batch.refresh_from_db(); self.assertEqual(batch.reserved_credits,0)

    def test_mixed_abandoned_draft_removes_finalized_and_partial_sources(self):
        batch,first=self.reserve(); self.write(first); self.disk.finalize_upload(first)
        data=b'Another resume'
        second=self.storage.reserve_uploads(batch.pk,self.user.pk,[{
            'client_key':'second','filename':'second.txt','role':'resume',
            'byte_size':len(data),'sha256':hashlib.sha256(data).hexdigest()}])[0]
        self.disk.write_chunk(second,0,io.BytesIO(b'Another'),7)
        CloudBatch.objects.filter(pk=batch.pk).update(expires_at=timezone.now()-timedelta(seconds=1))
        self.assertEqual(self.storage.cleanup_abandoned_uploads(),2)
        self.assertFalse(batch.inputs.exists())
        for item in (first,second):
            final=Path(self.tmp.name)/item.storage_key
            self.assertFalse(final.exists())
            self.assertFalse(final.with_name(final.name+'.part').exists())

    def test_symlink_directory_and_oversized_part_are_rejected(self):
        _, item = self.reserve(); self.disk.initiate_upload(item)
        target = Path(self.tmp.name)/item.storage_key
        target.with_name(target.name+'.part').write_bytes(b'x'*14)
        with self.assertRaises(ValueError): self.disk.finalize_upload(item)
        item.refresh_from_db(); self.assertIsNone(item.uploaded_at)
        with patch.object(Path, 'is_symlink', return_value=True):
            with self.assertRaises(ValueError): self.disk.committed_offset(item)

    def test_storage_quota_counts_legacy_and_reserved_other_batches(self):
        Candidate.objects.create(session=self.session, filename='old.txt', storage_name='old', digest='a'*64, byte_size=10, rubric_version=1)
        with override_settings(MAX_ACCOUNT_STORAGE_BYTES=22):
            with self.assertRaises(ValueError): self.reserve()
        with override_settings(AUTOMATIC_PROJECT_STORAGE_BYTES=12):
            with self.assertRaises(ValueError): self.reserve()
        with override_settings(AUTOMATIC_MIN_FREE_BYTES=10**20):
            with self.assertRaises(ValueError): self.reserve()
        self.assertFalse(UploadReservation.objects.exists())
        self.reserve()
        with override_settings(MAX_ACCOUNT_STORAGE_BYTES=30):
            with self.assertRaises(ValueError): self.reserve(key='two')

    def test_acceptance_checks_all_actual_files_and_atomic_ack_rollback(self):
        batch, item = self.reserve()
        with self.assertRaises(ValueError): self.storage.accept_batch(batch.pk, self.user.pk)
        self.write(item); self.disk.finalize_upload(item)
        from recruiting.cloud.notifications import queue_acknowledgement
        def fail_after_queue(batch):
            queue_acknowledgement(batch)
            raise ValueError('acceptance aborted')
        with patch('recruiting.cloud.notifications.queue_acknowledgement', side_effect=fail_after_queue):
            with self.assertRaises(ValueError): self.storage.accept_batch(batch.pk, self.user.pk)
        batch.refresh_from_db(); self.assertEqual(batch.stage, 'uploading')
        self.assertFalse(CloudOutbox.objects.exists())
        (Path(self.tmp.name)/item.storage_key).unlink()
        with self.assertRaises(ValueError): self.storage.accept_batch(batch.pk, self.user.pk)

    def test_orphan_and_result_bytes_consume_project_space(self):
        (Path(self.tmp.name)/'orphan').write_bytes(b'x'*30)
        with override_settings(AUTOMATIC_PROJECT_STORAGE_BYTES=40):
            with self.assertRaises(ValueError): self.reserve()
        self.assertFalse(UploadReservation.objects.exists())

    def test_free_space_floor_is_rechecked_before_every_chunk(self):
        _, item = self.reserve()
        with patch('recruiting.cloud.storage.shutil.disk_usage', return_value=type('Usage',(),{'free':1})()):
            with self.assertRaises(ValueError): self.write(item)
        self.assertEqual(self.disk.committed_offset(item),0)
    def test_completed_source_projection_does_not_double_charge_disk(self):
        batch,item=self.reserve(); self.write(item); self.disk.finalize_upload(item)
        Candidate.objects.create(session=self.session, filename=item.filename, storage_name=item.storage_key,
            digest=item.sha256,byte_size=13,rubric_version=1)
        with override_settings(MAX_ACCOUNT_STORAGE_BYTES=26,AUTOMATIC_PROJECT_STORAGE_BYTES=26):
            self.reserve(content=b'Another bytes',key='two')
        with override_settings(MAX_ACCOUNT_STORAGE_BYTES=27):
            with self.assertRaises(ValueError): self.reserve(content=b'other',key='three')

    def test_finalization_retry_after_seal_returns_same_verified_identity(self):
        batch,item=self.reserve(); self.write(item)
        original=self.disk.finalize_upload(item)
        self.storage.accept_batch(batch.pk,self.user.pk)
        self.assertEqual(self.disk.finalize_upload(item),original)

    def test_exact_chunk_retry_does_not_need_additional_disk_allowance(self):
        _,item=self.reserve(); self.write(item,b'Python')
        with patch('recruiting.cloud.storage.shutil.disk_usage', return_value=type('Usage',(),{'free':1})()):
            self.assertEqual(self.write(item,b'Python'),6)

    def test_sample_mutations_and_additions_never_become_fixture_inputs(self):
        with override_settings(AUTOMATIC_TRANSPORT='fixtures'):
            response=self.client.post('/batches/sample/',data='{}',content_type='application/json')
            batch=CloudBatch.objects.get(pk=response.json()['batch_id'])
            contract=import_module('recruiting.cloud.sample_contract')
            item=batch.inputs.first()
            for field,value in [('filename','other.txt'),('sha256','f'*64),('byte_size',1),('role','jd')]:
                original=getattr(item,field)
                UploadReservation.objects.filter(pk=item.pk).update(**{field:value})
                self.assertFalse(contract.is_canonical_sample(batch))
                UploadReservation.objects.filter(pk=item.pk).update(**{field:original})
            for field,value in [('jd','custom'),('rubric',[]),('mode','live')]:
                original=getattr(batch.session,field)
                HiringSession.objects.filter(pk=batch.session_id).update(**{field:value})
                self.assertFalse(contract.is_canonical_sample(batch))
                HiringSession.objects.filter(pk=batch.session_id).update(**{field:original})
            self.assertTrue(contract.is_canonical_sample(batch))
            self.assertEqual(self.client.post(f'/batches/{batch.pk}/uploads/',data='{}',content_type='application/json').status_code,503)
            with override_settings(AUTOMATIC_TRANSPORT='remote',AUTOMATIC_REMOTE_EXECUTOR='recruiting.tests.test_cloud_uploads.ReadyExecutor'):
                self.assertEqual(self.client.post(f'/batches/{batch.pk}/uploads/',data='{}',content_type='application/json').status_code,400)
            other=UploadReservation(batch=batch,client_key='extra',filename='extra.txt',role='resume',byte_size=1,sha256='e'*64)
            other.storage_key=f'cloud/{batch.account_key}/{batch.pk}/inputs/{other.pk}';other.save()
            self.assertFalse(contract.is_canonical_sample(batch))

    @override_settings(AUTOMATIC_REMOTE_EXECUTOR='recruiting.tests.test_cloud_uploads.ReadyExecutor')
    def test_chunk_stream_never_uses_request_body_and_failed_io_preserves_offset(self):
        batch,item=self.reserve()
        from django.core.handlers.wsgi import WSGIRequest
        with patch.object(WSGIRequest,'body',property(lambda request: (_ for _ in ()).throw(AssertionError('unbounded body read')))):
            response=self.client.post(f'/batches/{batch.pk}/uploads/{item.pk}/chunk/?offset=0',data=b'Python',content_type='application/octet-stream')
        self.assertEqual(response.status_code,200)
        with patch('recruiting.cloud.storage.os.fsync',side_effect=OSError('secret path must not leak')):
            response=self.client.post(f'/batches/{batch.pk}/uploads/{item.pk}/chunk/?offset=6',data=b' resume',content_type='application/octet-stream')
        self.assertEqual(response.status_code,503)
        self.assertNotIn('secret path',response.content.decode())
        self.assertEqual(self.disk.committed_offset(item),6)

    def test_legacy_endpoint_cannot_store_arbitrary_bytes_in_automatic_profile(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        response=self.client.post(f'/sessions/{self.session.pk}/upload/',{'files':SimpleUploadedFile('resume.txt',b'Python experience in production')})
        self.assertEqual(response.status_code,400)
        self.assertFalse(Candidate.objects.exists())

    def test_legacy_admission_counts_reserved_cloud_bytes(self):
        self.reserve()
        self.session.confirmed_at=timezone.now(); self.session.save()
        from django.core.files.uploadedfile import SimpleUploadedFile
        from recruiting.uploads import store_upload
        with override_settings(MAX_ACCOUNT_STORAGE_BYTES=40):
            with self.assertRaises(ValueError):
                store_upload(self.user,self.session,SimpleUploadedFile('resume.txt',b'Python experience in production'))
        self.assertFalse(Candidate.objects.exists())

    def test_unconfigured_live_rejects_before_accepting_bytes(self):
        response = self.client.post('/batches/new/', data=json.dumps({'title':'Role','jd':'Python','input_mode':'files'}),content_type='application/json')
        self.assertEqual(response.status_code, 503)
        self.assertFalse(CloudBatch.objects.exists())

    @override_settings(AUTOMATIC_REMOTE_EXECUTOR='recruiting.tests.test_cloud_uploads.ReadyExecutor')
    def test_owner_csrf_and_forged_json_claims_cannot_confirm_bytes(self):
        response = self.client.post('/batches/new/', data=json.dumps({'title':'Role','jd':'Python','input_mode':'files'}), content_type='application/json')
        self.assertEqual(response.status_code, 201)
        batch_id = response.json()['batch_id']
        payload = {'files':[{'client_key':'one','filename':'one.txt','role':'resume','byte_size':13,'sha256':hashlib.sha256(b'Python resume').hexdigest()}]}
        url = f'/batches/{batch_id}/uploads/'
        response = self.client.post(url, data=json.dumps(payload), content_type='application/json')
        self.assertEqual(response.status_code, 201)
        item_id = response.json()['uploads'][0]['id']
        base = f'/batches/{batch_id}/uploads/{item_id}/'
        self.assertEqual(self.client.post(base+'finish/', data=json.dumps({'bucket':'forged','generation':'123','url':'https://evil'}), content_type='application/json').status_code, 400)
        self.assertEqual(self.client.post(base+'chunk/?offset=0', data=b'Python resume', content_type='application/octet-stream').status_code, 200)
        self.assertEqual(self.client.post(base+'finish/', data='{}', content_type='application/json').status_code, 200)
        self.assertEqual(self.client.post(f'/batches/{batch_id}/submit/', data='{}', content_type='application/json').status_code, 200)
        stranger=User.objects.create_user('other'); Profile.objects.create(user=stranger,verified_at=timezone.now())
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(base).status_code, 404)
        from django.test import Client
        csrf_client=Client(enforce_csrf_checks=True); csrf_client.force_login(self.user)
        self.assertEqual(csrf_client.post(base+'chunk/?offset=0', data=b'x', content_type='application/octet-stream').status_code,403)

    @override_settings(AUTOMATIC_TRANSPORT='fixtures')
    def test_only_server_seeded_canonical_sample_is_accepted(self):
        self.assertEqual(self.client.post('/batches/new/', data=json.dumps({'title':'custom','jd':'custom'}), content_type='application/json').status_code,503)
        self.assertEqual(self.client.post('/batches/sample/', data=json.dumps({'jd':'custom'}), content_type='application/json').status_code,400)
        response=self.client.post('/batches/sample/', data='{}', content_type='application/json')
        self.assertEqual(response.status_code,201)
        batch=CloudBatch.objects.get(pk=response.json()['batch_id'])
        self.assertEqual(batch.stage,'validate'); self.assertEqual(batch.inputs.count(),3)
        contract=import_module('recruiting.cloud.sample_contract')
        self.assertTrue(contract.is_canonical_sample(batch))
        HiringSession.objects.filter(pk=batch.session_id).update(title='mutated')
        self.assertFalse(contract.is_canonical_sample(batch))
        self.assertContains(self.client.get(f'/batches/{batch.pk}/'), 'Synthetic demonstration')


class ReadyExecutor:
    def ready(self):
        return True



@skipUnless(connection.vendor == 'postgresql', 'Requires real PostgreSQL transaction advisory locks.')
class CloudUploadConcurrencyTests(UploadFixtures, TransactionTestCase):
    def parallel(self, functions):
        barrier=Barrier(len(functions))
        def invoke(function):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                try: function(); return True
                except ValueError: return False
            finally: close_old_connections()
        with ThreadPoolExecutor(max_workers=len(functions)) as pool:
            return list(pool.map(invoke,functions))

    def test_two_accounts_cannot_overbook_global_project_capacity(self):
        other=User.objects.create_user('second-account')
        Profile.objects.create(user=other,verified_at=timezone.now())
        second=HiringSession.objects.create(owner=other,title='Role',jd='Python')
        batches=[state.create_batch(self.user.pk,self.session.pk,'files'),state.create_batch(other.pk,second.pk,'files')]
        spec={'client_key':'one','filename':'one.txt','role':'resume','byte_size':13,'sha256':hashlib.sha256(b'Python resume').hexdigest()}
        with override_settings(AUTOMATIC_PROJECT_STORAGE_BYTES=20):
            result=self.parallel([lambda batch=batch:self.storage.reserve_uploads(batch.pk,batch.owner_id,[spec]) for batch in batches])
        self.assertEqual(sum(result),1)
        self.assertEqual(UploadReservation.objects.count(),1)

    def test_legacy_and_cloud_contend_for_the_same_account_disk_allowance(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from recruiting.uploads import store_upload
        self.session.confirmed_at=timezone.now(); self.session.save()
        batch=state.create_batch(self.user.pk,self.session.pk,'files')
        spec={'client_key':'one','filename':'one.txt','role':'resume','byte_size':13,'sha256':hashlib.sha256(b'Python resume').hexdigest()}
        with override_settings(MAX_ACCOUNT_STORAGE_BYTES=40):
            result=self.parallel([lambda:self.storage.reserve_uploads(batch.pk,self.user.pk,[spec]),
                lambda:store_upload(self.user,self.session,SimpleUploadedFile('resume.txt',b'Python experience in production'))])
        self.assertEqual(sum(result),1)
        self.assertEqual(UploadReservation.objects.count()+Candidate.objects.count(),1)

    def test_admission_acquires_global_before_profile_and_releases_on_view_failure(self):
        from recruiting.cloud.storage import admission_view
        queries=[]
        def capture(execute,sql,params,many,context):
            queries.append(sql)
            return execute(sql,params,many,context)
        batch=state.create_batch(self.user.pk,self.session.pk,'files')
        spec={'client_key':'one','filename':'one.txt','role':'resume','byte_size':13,'sha256':hashlib.sha256(b'Python resume').hexdigest()}
        with connection.execute_wrapper(capture): self.storage.reserve_uploads(batch.pk,self.user.pk,[spec])
        advisory=next(i for i,sql in enumerate(queries) if 'pg_advisory_xact_lock' in sql)
        profile=next(i for i,sql in enumerate(queries) if 'recruiting_profile' in sql and 'FOR UPDATE' in sql)
        self.assertLess(advisory,profile)
        @admission_view
        def failing(): raise ValueError('view failed')
        with self.assertRaises(ValueError): failing()
        self.assertTrue(all(self.parallel([lambda:self.reserve(content=b'other resume',key='other')])[0:1]))
