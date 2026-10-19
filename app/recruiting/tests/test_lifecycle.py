from datetime import datetime, timedelta, timezone as tz
from types import SimpleNamespace
from django.test import SimpleTestCase, TestCase
from django.contrib.auth.models import User
from django.utils import timezone
from recruiting.models import Profile

def profile(**kwargs):
    values={'last_activity_at':datetime(2026,1,31,12,tzinfo=tz.utc),'paid':False,'notice_at':None,'cancelled_at':None,'status':'active'}
    values.update(kwargs)
    return SimpleNamespace(**values)

class LifecycleTests(SimpleTestCase):
    def test_calendar_month_and_download_window(self):
        from recruiting.lifecycle import evaluate_lifecycle
        p=profile()
        self.assertEqual(evaluate_lifecycle(p,datetime(2026,2,28,11,tzinfo=tz.utc)),'retain')
        now=datetime(2026,2,28,12,tzinfo=tz.utc)
        self.assertEqual(evaluate_lifecycle(p,now),'send_download_notice')
        p.notice_at=now
        self.assertEqual(evaluate_lifecycle(p,now+timedelta(days=2,hours=23)),'download_window')
        self.assertEqual(evaluate_lifecycle(p,now+timedelta(days=3)),'delete_account')

    def test_return_or_payment_cancels_inactivity_deletion(self):
        from recruiting.lifecycle import evaluate_lifecycle
        now=datetime(2026,3,5,12,tzinfo=tz.utc)
        self.assertEqual(evaluate_lifecycle(profile(notice_at=now-timedelta(days=4),last_activity_at=now-timedelta(days=1)),now),'retain')
        self.assertEqual(evaluate_lifecycle(profile(notice_at=now-timedelta(days=4),paid=True),now),'retain')

    def test_paid_archive_after_six_months(self):
        from recruiting.lifecycle import evaluate_lifecycle
        self.assertEqual(evaluate_lifecycle(profile(paid=True),datetime(2026,7,31,12,tzinfo=tz.utc)),'archive_data_keep_subscription')

    def test_explicit_cancellation_is_separate(self):
        from recruiting.lifecycle import evaluate_lifecycle
        now=datetime(2026,3,5,12,tzinfo=tz.utc)
        p=profile(paid=True,last_activity_at=now,cancelled_at=now-timedelta(days=3))
        self.assertEqual(evaluate_lifecycle(p,now),'delete_account')

class DispatchTests(TestCase):
    def test_notice_clock_starts_only_after_dispatch(self):
        from recruiting.lifecycle import record_notice_dispatch
        p=Profile.objects.create(user=User.objects.create_user('notice'))
        now=timezone.now()
        record_notice_dispatch(p,now,False)
        p.refresh_from_db(); self.assertIsNone(p.notice_at)
        record_notice_dispatch(p,now,True)
        p.refresh_from_db(); self.assertEqual(p.notice_at,now)

    def test_preview_never_deletes_account_or_cancels_jobs(self):
        from recruiting.lifecycle import evaluate_lifecycle
        p=Profile.objects.create(user=User.objects.create_user('keep'),last_activity_at=timezone.now()-timedelta(days=60),notice_at=timezone.now()-timedelta(days=4))
        self.assertEqual(evaluate_lifecycle(p,timezone.now()),'delete_account')
        self.assertTrue(User.objects.filter(pk=p.user_id).exists())
