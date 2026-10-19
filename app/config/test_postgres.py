"""Isolated PostgreSQL integration settings. Never a deployment settings module."""
import os
from .settings import *
DATABASES={'default':{'ENGINE':'django.db.backends.postgresql','NAME':os.environ.get('POSTGRES_TEST_DB','postgres'),'USER':os.environ.get('POSTGRES_TEST_USER','wm_test'),'PASSWORD':os.environ['POSTGRES_TEST_PASSWORD'],'HOST':'127.0.0.1','PORT':os.environ.get('POSTGRES_TEST_PORT','55432'),'CONN_MAX_AGE':0,'TEST':{'NAME':'test_webmonkie_production'}}}
