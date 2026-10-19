from contextlib import contextmanager
from threading import Event,Thread
from django.db import close_old_connections
from django.utils import timezone
from .models import ServiceHeartbeat

def write_heartbeat(service):
    close_old_connections()
    try: ServiceHeartbeat.objects.update_or_create(service=service,defaults={'seen_at':timezone.now()})
    finally: close_old_connections()

@contextmanager
def service_heartbeat(service,interval=15):
    stopped=Event()
    def run():
        while not stopped.is_set():
            try: write_heartbeat(service)
            except Exception: pass  # Readiness observes expiry; never log database secrets.
            stopped.wait(interval)
    thread=Thread(target=run,name=f'{service}-heartbeat',daemon=True); thread.start()
    try: yield
    finally:
        stopped.set(); thread.join(timeout=5)
