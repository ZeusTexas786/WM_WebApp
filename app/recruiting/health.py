from django.http import JsonResponse
from django.db import connection
from django.conf import settings
from pathlib import Path
from django.utils import timezone
from datetime import timedelta

def live(request): return JsonResponse({'status':'alive'})

def ready(request):
    try:
        with connection.cursor() as cursor: cursor.execute('SELECT 1')
        if getattr(settings, 'FIXTURE_PILOT', False):
            from .pilot import assert_fixture_ready
            assert_fixture_ready()
        elif not settings.LOCAL_DEVELOPMENT:
            from .models import ServiceHeartbeat
            from .scanning import scanner_ready
            import socket,struct,json
            if not Path(settings.PARSER_SOCKET).exists(): raise RuntimeError
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
                client.settimeout(2)
                client.connect(settings.PARSER_SOCKET)
                payload=b'{"operation":"ping"}'
                client.sendall(struct.pack('!I',len(payload))+payload)
                if client.recv(64)!=b'{"status":"ready"}': raise RuntimeError
            if not scanner_ready(): raise RuntimeError
            if not ServiceHeartbeat.objects.filter(service='worker',seen_at__gte=timezone.now()-timedelta(seconds=60)).exists(): raise RuntimeError
        return JsonResponse({'status':'ready'})
    except Exception: return JsonResponse({'status':'not_ready'},status=503)
