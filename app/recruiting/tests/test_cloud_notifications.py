from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless
from unittest.mock import patch
from django.contrib.auth.models import User
from django.core import mail
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from recruiting.models import DeletionTombstone, HiringSession, Profile
from recruiting.cloud.models import CloudBatch, CloudOutbox


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
                   PUBLIC_ORIGIN='http://127.0.0.1:8000', EMAIL_TIMEOUT=15)
class CloudNotificationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('notice@example.test', email='notice@example.test')
        self.profile = Profile.objects.create(user=self.user, verified_at=timezone.now())
        self.session = HiringSession.objects.create(owner=self.user, title='PRIVATE ROLE NAME',
                                                    jd='PRIVATE CANDIDATE TEXT', mode='live')
        self.batch = CloudBatch.objects.create(owner=self.user, session=self.session,
            account_key=self.profile.account_key, account_epoch=self.profile.account_epoch,
            input_mode='files', rubric_version=0, stage='validate', sealed_at=timezone.now(),
            expires_at=timezone.now()+timedelta(hours=24))

    def test_acknowledgement_is_deduplicated_and_branded_without_private_content(self):
        from recruiting.cloud.notifications import queue_acknowledgement, deliver_notifications
        first = queue_acknowledgement(self.batch)
        self.assertEqual(first.pk, queue_acknowledgement(self.batch).pk)
        self.assertEqual(deliver_notifications(), 1)
        self.assertEqual(deliver_notifications(), 0)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [self.user.email])
        self.assertEqual(message.from_email, 'WebMonkie by AscendWorksNow <ascendworksnow@gmail.com>')
        self.assertEqual(message.subject, 'The WebMonkies have your brief')
        self.assertIn('Serious work and spirited debate are on the agenda.', message.body)
        self.assertIn("We'll email you when your results are ready.", message.body)
        self.assertIn(f'/batches/{self.batch.pk}/', message.body)
        self.assertIn('https://ascendworksnow.com', message.body)
        for part in [message.body, message.alternatives[0].content]:
            self.assertNotIn('PRIVATE', part)
            self.assertNotIn('resume analysis', part)

    def test_unsealed_batch_cannot_send_acknowledgement(self):
        from recruiting.cloud.notifications import queue_acknowledgement
        self.batch.sealed_at = None
        self.batch.stage = 'uploading'
        self.batch.save()
        with self.assertRaises(ValueError):
            queue_acknowledgement(self.batch)
        self.assertFalse(CloudOutbox.objects.exists())

    def test_terminal_notice_waits_until_ready_and_has_no_attachments(self):
        from recruiting.cloud.notifications import queue_terminal_notice, deliver_notifications
        self.assertIsNone(queue_terminal_notice(self.batch))
        self.batch.stage = 'completed'
        self.batch.expanded_manifest = [{'filename': 'PRIVATE.pdf'}, {'filename': 'OTHER.pdf'}]
        self.batch.settled_credits = 1
        self.batch.save()
        event = queue_terminal_notice(self.batch)
        self.assertEqual(event.pk, queue_terminal_notice(self.batch).pk)
        self.assertEqual(deliver_notifications(), 1)
        self.assertIn('1 of 2', mail.outbox[0].body)
        self.assertIn('could not be completed', mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].attachments, [])
        self.assertNotIn('PRIVATE', mail.outbox[0].body)

    def test_delivery_failure_is_retried_with_backoff_and_stops_after_three_attempts(self):
        from recruiting.cloud.notifications import queue_acknowledgement, deliver_notifications
        event = queue_acknowledgement(self.batch)
        with patch('django.core.mail.EmailMultiAlternatives.send', side_effect=OSError('private SMTP error')):
            self.assertEqual(deliver_notifications(), 0)
            event.refresh_from_db()
            self.assertEqual(event.state, 'pending')
            self.assertGreater(event.lease_until, timezone.now())
            self.assertEqual(deliver_notifications(), 0)
            for _ in range(2):
                CloudOutbox.objects.filter(pk=event.pk).update(lease_until=timezone.now()-timedelta(seconds=1))
                deliver_notifications()
        event.refresh_from_db()
        self.assertEqual(event.state, 'failed')
        self.assertEqual(event.attempts, 3)

    def test_cancelled_or_changed_epoch_suppresses_pending_mail(self):
        from recruiting.cloud.notifications import queue_acknowledgement, deliver_notifications
        event = queue_acknowledgement(self.batch)
        self.profile.account_epoch += 1
        self.profile.save()
        self.assertEqual(deliver_notifications(), 0)
        event.refresh_from_db()
        self.assertEqual(event.state, 'cancelled')

        self.assertEqual(len(mail.outbox), 0)

    def test_completed_acknowledgement_does_not_claim_work_is_queued(self):
        from recruiting.cloud.notifications import queue_acknowledgement, deliver_notifications
        queue_acknowledgement(self.batch)
        self.batch.stage = 'completed'
        self.batch.save()
        deliver_notifications()
        self.assertIn('ready', mail.outbox[0].body)
        self.assertNotIn("We'll email you when", mail.outbox[0].body)

    def test_sample_notifications_are_explicitly_labelled(self):
        from recruiting.cloud.notifications import queue_acknowledgement, deliver_notifications
        self.session.mode = 'sample'
        self.session.save()
        queue_acknowledgement(self.batch)
        deliver_notifications()
        self.assertIn('Sample', mail.outbox[0].subject)
        self.assertIn('synthetic', mail.outbox[0].body)

    def test_invalid_origin_never_enters_a_message(self):
        from recruiting.cloud.notifications import queue_acknowledgement, deliver_notifications
        event = queue_acknowledgement(self.batch)
        with override_settings(PUBLIC_ORIGIN='https://safe.example@evil.example/not-an-origin'):
            self.assertEqual(deliver_notifications(), 0)
        self.assertEqual(len(mail.outbox), 0)
        event.refresh_from_db()
        self.assertEqual(event.state, 'failed')

    def test_unknown_notification_type_is_not_sent(self):
        from recruiting.cloud.notifications import deliver_notifications
        event = CloudOutbox.objects.create(batch=self.batch, kind='notification',
                                           stage='forged', event_key='forged')
        self.assertEqual(deliver_notifications(), 0)
        event.refresh_from_db()
        self.assertEqual(event.state, 'failed')

    def test_zero_send_result_is_not_marked_delivered(self):
        from recruiting.cloud.notifications import queue_acknowledgement, deliver_notifications
        event = queue_acknowledgement(self.batch)
        with patch('django.core.mail.EmailMultiAlternatives.send', return_value=0):
            deliver_notifications()
        event.refresh_from_db()
        self.assertEqual(event.state, 'pending')

    def test_existing_atomic_completion_event_is_delivered_without_duplicate(self):
        from recruiting.cloud.notifications import queue_terminal_notice, deliver_notifications
        self.batch.stage = 'completed'
        self.batch.save()
        event = CloudOutbox.objects.create(batch=self.batch, kind='notification',
            stage='completed', event_key='notification:completed')
        self.assertEqual(queue_terminal_notice(self.batch).pk, event.pk)
        self.assertEqual(CloudOutbox.objects.filter(kind='notification').count(), 1)
        self.assertEqual(deliver_notifications(), 1)

    def test_deletion_tombstone_suppresses_old_restored_notification(self):
        from recruiting.cloud.notifications import queue_acknowledgement, deliver_notifications
        event = queue_acknowledgement(self.batch)
        DeletionTombstone.objects.create(account_key=self.profile.account_key)
        self.assertEqual(deliver_notifications(), 0)
        event.refresh_from_db()
        self.assertEqual(event.state, 'cancelled')


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
                   PUBLIC_ORIGIN='http://127.0.0.1:8000')
class CloudNotificationDeliveryBoundaryTests(TransactionTestCase):
    def setUp(self):
        user = User.objects.create_user('boundary@example.test', email='boundary@example.test')
        profile = Profile.objects.create(user=user, verified_at=timezone.now())
        session = HiringSession.objects.create(owner=user, title='Role', jd='Python')
        self.batch = CloudBatch.objects.create(owner=user, session=session,
            account_key=profile.account_key, account_epoch=profile.account_epoch, input_mode='files',
            rubric_version=0, stage='validate', sealed_at=timezone.now(),
            expires_at=timezone.now()+timedelta(hours=1))

    def test_smtp_send_holds_no_database_transaction(self):
        from recruiting.cloud.notifications import queue_acknowledgement, deliver_notifications
        queue_acknowledgement(self.batch)
        observed = []
        def send(**kwargs):
            observed.append(connection.in_atomic_block)
            return 1
        with patch('django.core.mail.EmailMultiAlternatives.send', side_effect=send):
            self.assertEqual(deliver_notifications(), 1)
        self.assertEqual(observed, [False])

    @skipUnless(connection.vendor == 'postgresql', 'Requires real row locking.')
    def test_simultaneous_deliverers_accept_one_notification(self):
        from recruiting.cloud.notifications import queue_acknowledgement, deliver_notifications
        queue_acknowledgement(self.batch)
        def deliver(_):
            close_old_connections()
            try:
                return deliver_notifications()
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(deliver, [1, 2]))
        self.assertEqual(sum(results), 1)
        self.assertEqual(CloudOutbox.objects.filter(state='delivered').count(), 1)

