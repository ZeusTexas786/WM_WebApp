"""Resolve public official image tags to immutable manifest digests. No credentials."""
import json
import re
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request,urlopen

IMAGES={'PYTHON_IMAGE':('library/python','3.12.14-slim-bookworm'),'POSTGRES_IMAGE':('library/postgres','18.6'),'NGINX_IMAGE':('library/nginx','stable-alpine'),'CLAMAV_IMAGE':('clamav/clamav','stable')}

def resolve(repository,tag):
    query=urlencode({'service':'registry.docker.io','scope':f'repository:{repository}:pull'})
    with urlopen('https://auth.docker.io/token?'+query,timeout=20) as response: token=json.load(response)['token']
    request=Request(f'https://registry-1.docker.io/v2/{repository}/manifests/{tag}',headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'})
    with urlopen(request,timeout=20) as response:
        digest=response.headers['Docker-Content-Digest']
        if not re.fullmatch(r'sha256:[0-9a-f]{64}',digest): raise ValueError('Registry returned an invalid digest.')
        return f'{repository}@{digest}'

if __name__=='__main__':
    resolved={key:resolve(*value) for key,value in IMAGES.items()}
    root=Path(__file__).resolve().parents[1]/'deploy'
    (root/'image-lock.json').write_text(json.dumps({'resolved_at_utc':datetime.now(timezone.utc).isoformat(),'images':resolved,'tags':{key:value[1] for key,value in IMAGES.items()},'status':'resolved; container build and vulnerability acceptance still required'},indent=2)+'\n')
    (root/'images.lock.env').write_text('\n'.join(f'{key}={value}' for key,value in resolved.items())+'\n')
    print('Resolved four public image digests. This does not establish vulnerability acceptance.')
