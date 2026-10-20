"""Pure, fail-closed validation; never load a developer's local environment file."""
import ipaddress
import base64
import re
from pathlib import Path
from urllib.parse import urlsplit

def load_production_config(environ, *, profile='full'):
    if profile not in ('full', 'fixture_pilot'):
        raise ValueError('Unknown production execution profile.')
    pilot = profile == 'fixture_pilot'
    required=('DJANGO_SECRET_KEY','ALLOWED_HOSTS','PUBLIC_ORIGIN','POSTGRES_DB','POSTGRES_USER','POSTGRES_PASSWORD','POSTGRES_HOST','PRIVATE_ROOT','SMTP_HOST','SMTP_USER','SMTP_PASSWORD','SMTP_FROM','AI_API_KEY','SCANNER_HOST','PARSER_SOCKET','ARCHIVE_KEY','TRUSTED_PROXY_NETWORKS')
    if pilot:
        required = tuple(key for key in required if key not in ('AI_API_KEY','SCANNER_HOST','PARSER_SOCKET'))
        if (environ.get('AUTOMATIC_WORKFLOW_ENABLED') != '1'
                or environ.get('AUTOMATIC_TRANSPORT') != 'fixtures'
                or environ.get('AI_MAX_DAILY_REQUESTS') != '0'
                or environ.get('AUTOMATIC_REMOTE_EXECUTOR', '').strip()
                or environ.get('LIFECYCLE_EXECUTION_ENABLED', '0') != '0'):
            raise ValueError('Fixture pilot requires synthetic execution, zero AI allowance and disabled lifecycle.')
    if any(not environ.get(key,'').strip() for key in required):
        missing=', '.join(key for key in required if not environ.get(key,'').strip())
        raise ValueError('Missing production configuration: '+missing)
    if environ.get('WEBMONKIE_LOCAL')=='1': raise ValueError('Local development cannot be enabled in production.')
    try:
        if len(base64.b64decode(environ['ARCHIVE_KEY'],altchars=b'-_',validate=True))!=32: raise ValueError
    except Exception as exc: raise ValueError('ARCHIVE_KEY must encode 32 random bytes.') from exc
    secret=environ['DJANGO_SECRET_KEY']
    if len(secret)<64 or len(set(secret))<12: raise ValueError('Use a random production secret with at least 64 characters.')
    hosts=[value.strip().lower() for value in environ['ALLOWED_HOSTS'].split(',')]
    for host in hosts:
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?',host) or host in ('localhost','127.0.0.1','0.0.0.0') or '.' not in host:
            raise ValueError('Production hosts must be exact public DNS names.')
        try: ipaddress.ip_address(host)
        except ValueError: pass
        else: raise ValueError('Use a public DNS hostname rather than a raw address.')
    origin=urlsplit(environ['PUBLIC_ORIGIN'])
    if origin.scheme!='https' or origin.hostname not in hosts or origin.username or origin.password or origin.path not in ('','/') or origin.query or origin.fragment or origin.port not in (None,443):
        raise ValueError('PUBLIC_ORIGIN must be an HTTPS origin matching a configured hostname.')
    root=Path(environ['PRIVATE_ROOT']).resolve()
    checkout=Path(__file__).resolve().parents[2]
    if not Path(environ['PRIVATE_ROOT']).is_absolute() or root.is_relative_to(checkout):
        raise ValueError('Private production storage must be absolute and outside the source checkout.')
    networks=[ipaddress.ip_network(value.strip()) for value in environ['TRUSTED_PROXY_NETWORKS'].split(',')]
    if any(network.prefixlen==0 for network in networks): raise ValueError('Never trust all proxy addresses.')
    result = {
        'FIXTURE_PILOT':pilot,
        'LOCAL_DEVELOPMENT':False,'DEBUG':False,'SECRET_KEY':secret,'ALLOWED_HOSTS':hosts,
        'PUBLIC_ORIGIN':environ['PUBLIC_ORIGIN'].rstrip('/'),'CSRF_TRUSTED_ORIGINS':[environ['PUBLIC_ORIGIN'].rstrip('/')],
        'DATABASES':{'default':{'ENGINE':'django.db.backends.postgresql','NAME':environ['POSTGRES_DB'],'USER':environ['POSTGRES_USER'],'PASSWORD':environ['POSTGRES_PASSWORD'],'HOST':environ['POSTGRES_HOST'],'PORT':environ.get('POSTGRES_PORT','5432'),'CONN_MAX_AGE':60,'CONN_HEALTH_CHECKS':True,'OPTIONS':{'connect_timeout':5}}},
        'PRIVATE_ROOT':root,'SESSION_COOKIE_SECURE':True,'CSRF_COOKIE_SECURE':True,
        'SECURE_SSL_REDIRECT':True,'SECURE_HSTS_SECONDS':31536000,'SECURE_HSTS_INCLUDE_SUBDOMAINS':True,'SECURE_HSTS_PRELOAD':False,
        'TRUSTED_PROXY_NETWORKS':tuple(str(network) for network in networks),
        'SCANNER_HOST':environ.get('SCANNER_HOST',''),'SCANNER_PORT':int(environ.get('SCANNER_PORT','3310')),'PARSER_SOCKET':environ.get('PARSER_SOCKET',''),
        'ARCHIVE_KEY':environ['ARCHIVE_KEY'],'LIFECYCLE_EXECUTION_ENABLED':environ.get('LIFECYCLE_EXECUTION_ENABLED','0')=='1',
        'SECURE_PROXY_SSL_HEADER':('HTTP_X_FORWARDED_PROTO','https'),
    }
    if pilot:
        result.update(AUTOMATIC_WORKFLOW_ENABLED=True, AUTOMATIC_TRANSPORT='fixtures',
                      AUTOMATIC_REMOTE_EXECUTOR='', AI_API_KEY='', AI_MAX_DAILY_REQUESTS=0,
                      SCANNER_HOST='', PARSER_SOCKET='', LIFECYCLE_EXECUTION_ENABLED=False)
    return result
