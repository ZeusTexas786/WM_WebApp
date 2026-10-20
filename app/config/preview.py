"""Local fictional preview: do not load .env or enable outbound services."""
import os
os.environ['WEBMONKIE_LOCAL'] = '1'
from .settings import *

_preview_port = int(os.environ.get('WEBMONKIE_PREVIEW_PORT', '8000'))
if not 1024 <= _preview_port <= 65535:
    raise ValueError('Local preview port must be between 1024 and 65535.')
PUBLIC_ORIGIN = f'http://127.0.0.1:{_preview_port}'
ALLOWED_HOSTS = ['127.0.0.1', 'localhost', '[::1]']
AUTOMATIC_WORKFLOW_ENABLED = True
AUTOMATIC_TRANSPORT = 'fixtures'
AUTOMATIC_REMOTE_EXECUTOR = ''
AI_API_KEY = ''
AI_MAX_DAILY_REQUESTS = 0
EMAIL_HOST = ''
EMAIL_HOST_USER = ''
EMAIL_HOST_PASSWORD = ''
LIFECYCLE_EXECUTION_ENABLED = False
