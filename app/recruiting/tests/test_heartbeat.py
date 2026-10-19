import threading
from unittest.mock import patch
from django.test import SimpleTestCase

class HeartbeatTests(SimpleTestCase):
    def test_heartbeat_keeps_running_while_main_work_is_blocked(self):
        from recruiting.heartbeats import service_heartbeat
        second_tick=threading.Event(); ticks=[]
        def tick(service):
            ticks.append(service)
            if len(ticks)>=2: second_tick.set()
        with patch('recruiting.heartbeats.write_heartbeat',side_effect=tick):
            with service_heartbeat('worker',interval=.01):
                self.assertTrue(second_tick.wait(1),'Busy work must not stop the service heartbeat.')
