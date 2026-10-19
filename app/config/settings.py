import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
# Local environment file is never served or committed. Existing process values win.
env_file = BASE_DIR.parent / '.env'
if os.environ.get('DJANGO_SETTINGS_MODULE','config.settings') != 'config.production' and env_file.exists():
    for line in env_file.read_text(encoding='utf-8').splitlines():
        if line.strip() and not line.lstrip().startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
LOCAL_DEVELOPMENT = os.environ.get('WEBMONKIE_LOCAL', '1') == '1'
DEBUG = False
secret_file = BASE_DIR / '.secret'
if LOCAL_DEVELOPMENT and not secret_file.exists():
    secret_file.write_text(secrets.token_urlsafe(48), encoding='utf-8')
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY') or (secret_file.read_text() if LOCAL_DEVELOPMENT else '')
if not SECRET_KEY:
    raise RuntimeError('DJANGO_SECRET_KEY must be configured.')
ALLOWED_HOSTS = ['127.0.0.1', 'localhost', '[::1]'] if LOCAL_DEVELOPMENT else os.environ.get('ALLOWED_HOSTS', '').split(',')
INSTALLED_APPS = ['django.contrib.auth', 'django.contrib.contenttypes', 'django.contrib.sessions', 'django.contrib.messages', 'django.contrib.staticfiles', 'recruiting']
MIDDLEWARE = ['django.middleware.security.SecurityMiddleware', 'django.contrib.sessions.middleware.SessionMiddleware', 'django.middleware.common.CommonMiddleware', 'django.middleware.csrf.CsrfViewMiddleware', 'django.contrib.auth.middleware.AuthenticationMiddleware', 'django.contrib.messages.middleware.MessageMiddleware', 'django.middleware.clickjacking.XFrameOptionsMiddleware', 'recruiting.middleware.SecurityHeaders']
ROOT_URLCONF = 'config.urls'
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates', 'DIRS': [], 'APP_DIRS': True, 'OPTIONS': {'context_processors': ['django.template.context_processors.request', 'django.contrib.auth.context_processors.auth', 'django.contrib.messages.context_processors.messages', 'recruiting.context.runtime']}}]
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': BASE_DIR / 'db.sqlite3', 'OPTIONS': {'timeout': 20, 'transaction_mode': 'IMMEDIATE'}}}
AUTH_PASSWORD_VALIDATORS = [{'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'}, {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 12}}, {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'}, {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'}]
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'America/Chicago'
USE_TZ = True
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
PRIVATE_ROOT = BASE_DIR / 'private'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
LOGIN_URL = '/login/'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_SECURE = not LOCAL_DEVELOPMENT
CSRF_COOKIE_SECURE = not LOCAL_DEVELOPMENT
SECURE_SSL_REDIRECT = not LOCAL_DEVELOPMENT
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'same-origin'
X_FRAME_OPTIONS = 'DENY'
DATA_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FILES = 30
FILE_UPLOAD_HANDLERS=['recruiting.upload_limits.LimitedUploadHandler','django.core.files.uploadhandler.MemoryFileUploadHandler','django.core.files.uploadhandler.TemporaryFileUploadHandler']
FILE_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = os.environ.get('SMTP_HOST', '')
EMAIL_PORT = int(os.environ.get('SMTP_PORT', '587'))
EMAIL_HOST_USER = os.environ.get('SMTP_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('SMTP_PASSWORD', '')
EMAIL_USE_TLS = True
EMAIL_TIMEOUT = 15
DEFAULT_FROM_EMAIL = os.environ.get('SMTP_FROM', 'WebMonkie <no-reply@localhost>')
PUBLIC_ORIGIN = os.environ.get('PUBLIC_ORIGIN', 'http://127.0.0.1:8000')
AI_API_KEY = os.environ.get('AI_API_KEY', '')
AI_EXTRACT_MODEL = os.environ.get('AI_EXTRACT_MODEL', 'gpt-5.4-mini')
AI_ASSESS_MODEL = os.environ.get('AI_ASSESS_MODEL', 'gpt-5.4')
AI_MAX_DAILY_REQUESTS = int(os.environ.get('AI_MAX_DAILY_REQUESTS','100'))
RESEARCH_HOSTS = ('docs.python.org', 'docs.djangoproject.com', 'www.postgresql.org', 'developer.mozilla.org', 'react.dev', 'nodejs.org')
TRUSTED_PROXY_NETWORKS=()
LIFECYCLE_EXECUTION_ENABLED=False
SCANNER_HOST=''
SCANNER_PORT=3310
PARSER_SOCKET=''
MAX_ACCOUNT_STORAGE_BYTES=300*1024*1024
MAX_ACCOUNT_QUEUED_JOBS=30
