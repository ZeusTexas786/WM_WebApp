from pathlib import Path
import base64
from django.test import SimpleTestCase

def valid_environment():
    return {'DJANGO_SECRET_KEY':'test-only-production-secret-'*4,'ALLOWED_HOSTS':'app.example.com','PUBLIC_ORIGIN':'https://app.example.com','POSTGRES_DB':'webmonkie','POSTGRES_USER':'webmonkie','POSTGRES_PASSWORD':'test-only-database-password','POSTGRES_HOST':'database','PRIVATE_ROOT':str(Path.home()/'.webmonkie-test-private'),'SMTP_HOST':'smtp.example.com','SMTP_USER':'mailer','SMTP_PASSWORD':'test-only-mail-password','SMTP_FROM':'no-reply@example.com','AI_API_KEY':'test-only-ai-key','SCANNER_HOST':'scanner','PARSER_SOCKET':'/run/parser/parser.sock','ARCHIVE_KEY':base64.urlsafe_b64encode(b't'*32).decode(),'TRUSTED_PROXY_NETWORKS':'172.30.0.0/24'}

class ProductionSettingsTests(SimpleTestCase):
    def test_requires_every_production_dependency(self):
        from config.production_config import load_production_config
        env=valid_environment()
        for key in ['DJANGO_SECRET_KEY','ALLOWED_HOSTS','PUBLIC_ORIGIN','POSTGRES_PASSWORD','POSTGRES_HOST','PRIVATE_ROOT','SMTP_HOST','AI_API_KEY','SCANNER_HOST','PARSER_SOCKET']:
            with self.subTest(key=key),self.assertRaises(ValueError): load_production_config({k:v for k,v in env.items() if k!=key})

    def test_rejects_short_secret_local_origin_and_wildcard_hosts(self):
        from config.production_config import load_production_config
        for key,value in [('DJANGO_SECRET_KEY','short'),('PUBLIC_ORIGIN','http://app.example.com'),('PUBLIC_ORIGIN','https://localhost'),('PUBLIC_ORIGIN','https://app.example.com/path'),('ALLOWED_HOSTS','*'),('ALLOWED_HOSTS','localhost'),('ALLOWED_HOSTS','127.0.0.1'),('WEBMONKIE_LOCAL','1')]:
            with self.subTest(key=key,value=value),self.assertRaises(ValueError): load_production_config(dict(valid_environment(),**{key:value}))

    def test_private_storage_cannot_be_inside_checkout(self):
        from config.production_config import load_production_config
        from django.conf import settings
        with self.assertRaises(ValueError): load_production_config(dict(valid_environment(),PRIVATE_ROOT=str(settings.BASE_DIR/'private')))

    def test_valid_config_uses_postgres_secure_cookies_and_no_local_mail(self):
        from config.production_config import load_production_config
        result=load_production_config(valid_environment())
        self.assertFalse(result['LOCAL_DEVELOPMENT'])
        self.assertTrue(result['SESSION_COOKIE_SECURE'])
        self.assertEqual(result['DATABASES']['default']['ENGINE'],'django.db.backends.postgresql')
        self.assertEqual(result['CSRF_TRUSTED_ORIGINS'],['https://app.example.com'])
