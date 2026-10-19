import http.client
import ipaddress
import socket
import ssl
import time
from html.parser import HTMLParser
from urllib.parse import urlsplit, urljoin
from django.conf import settings
from django.utils import timezone
from .assessment import text
from .models import Preference

DOCUMENTATION = {'python': ('https://docs.python.org/3/tutorial/', 'Python tutorial'), 'django':('https://docs.djangoproject.com/en/5.2/topics/testing/overview/', 'Django testing'), 'postgres':('https://www.postgresql.org/docs/current/using-explain.html','PostgreSQL EXPLAIN'), 'sql':('https://www.postgresql.org/docs/current/using-explain.html','PostgreSQL EXPLAIN'), 'javascript':('https://developer.mozilla.org/en-US/docs/Web/JavaScript/Guide','JavaScript guide'), 'react':('https://react.dev/learn','React documentation')}

def validate_destination(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in settings.RESEARCH_HOSTS or parsed.username or parsed.password or parsed.port not in (None,443):
        raise ValueError('Research URL is outside the approved HTTPS documentation sources.')
    addresses = socket.getaddrinfo(parsed.hostname,443,type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError('Research destination is not public infrastructure.')
    return parsed, addresses[0][4][0]

def fetch_once(url):
    parsed, address = validate_destination(url)
    # Pin the validated address while retaining host-based certificate verification.
    connection = http.client.HTTPSConnection(parsed.hostname,443,timeout=8,context=ssl.create_default_context())
    raw = socket.create_connection((address,443),timeout=8)
    try:
        connection.sock = ssl.create_default_context().wrap_socket(raw,server_hostname=parsed.hostname)
        connection.request('GET',parsed.path+('?' + parsed.query if parsed.query else '') or '/',headers={'User-Agent':'WebMonkie-Research/0.1','Accept':'text/html,text/plain','Accept-Encoding':'identity'})
        response = connection.getresponse()
        pieces, size, deadline = [], 0, time.monotonic()+12
        while True:
            if time.monotonic()>deadline: raise TimeoutError
            piece = response.read(16384)
            if not piece: break
            size += len(piece)
            if size > 600000: raise ValueError('Documentation response exceeds size limit.')
            pieces.append(piece)
        return response.status, dict(response.getheaders()), b''.join(pieces)
    finally:
        connection.close()
        raw.close()

class PlainText(HTMLParser):
    def __init__(self): super().__init__(); self.pieces=[]; self.hidden=0
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'): self.hidden+=1
    def handle_endtag(self,tag):
        if tag in ('script','style'): self.hidden=max(0,self.hidden-1)
    def handle_data(self,data):
        if not self.hidden and data.strip(): self.pieces.append(data.strip())

def fetch_documentation(url):
    try:
        for _ in range(4):
            parsed = urlsplit(url)
            # Structural validation is also applied before a mocked/network fetch.
            if parsed.scheme != 'https' or parsed.hostname not in settings.RESEARCH_HOSTS or parsed.username or parsed.password or parsed.port not in (None,443): raise ValueError('Unapproved research destination.')
            status, headers, body = fetch_once(url)
            headers = {key.lower():value for key,value in headers.items()}
            if status in (301,302,303,307,308):
                url = urljoin(url,headers.get('location',''))
                continue
            if status != 200 or len(body)>600000 or 'text/' not in headers.get('content-type',''):
                raise ValueError('Documentation could not be retrieved safely.')
            parser=PlainText(); parser.feed(body.decode('utf-8',errors='replace'))
            return {'url':url,'text':' '.join(parser.pieces)[:18000],'retrieved_at':timezone.now().isoformat()}
        raise ValueError('Too many documentation redirects.')
    except (OSError, http.client.HTTPException) as exc:
        raise ValueError('Documentation research is unavailable; retry later.') from exc

def validate_pack(payload,sources):
    if not isinstance(payload,dict): raise ValueError('Invalid interview response.')
    questions=payload.get('questions')
    if not isinstance(questions,list) or not 5<=len(questions)<=10: raise ValueError('Interview pack needs 5–10 questions.')
    allowed={source['url'] for source in sources}
    result=[]
    for q in questions:
        if not isinstance(q,dict): raise ValueError('Invalid interview question.')
        item={key:text(q.get(key),2500,key) for key in ('skill','question','answer','alternatives','rubric')}
        urls=q.get('source_urls')
        if not isinstance(urls,list) or any(url not in allowed for url in urls): raise ValueError('Interview cites an unfetched source.')
        item['source_urls']=urls
        item['research_verified']=bool(urls)
        result.append(item)
    cited=sum(bool(q['source_urls']) for q in result)
    research_status=(f'{cited} of {len(result)} questions cite retrieved documentation. Uncited answers are not research-verified; all answer guidance requires human review.' if cited else 'Answers are not research-verified. No fetched source was cited by these questions.')
    return {'questions':result,'sources':[{key:source[key] for key in ('url','title','retrieved_at')} for source in sources], 'research_status':research_status,'sample':False}

def generate_interview_pack(session,candidate):
    from .providers import request_json
    from .samples import sample_interviews
    if session.mode == 'sample': return sample_interviews()
    job=candidate.job
    terms=' '.join(c['name'] for c in job.rubric).lower()
    choices=[]
    for keyword,pair in DOCUMENTATION.items():
        if keyword in terms and pair not in choices: choices.append(pair)
    sources=[]
    for url,title in choices[:3]:
        source=fetch_documentation(url); source['title']=title; sources.append(source)
    preferences=list(Preference.objects.filter(owner=session.owner).order_by('-created_at').values_list('text',flat=True)[:5])
    result=request_json('Create 5–10 practical hands-on interview questions including candidate-specific probes. Return {"questions":[{"skill":"...","question":"scenario and follow-up","answer":"expected reasoning with context/version caveats","alternatives":"acceptable approaches","rubric":"how the interviewer assesses evidence","source_urls":["only provided documentation URLs"]}]}. Do not imply that any question proves authenticity. Reference provided documentation for technical claims, and clearly permit reasonable alternatives.',{'jd':job.jd,'criteria':job.rubric,'assessment':job.result,'documentation':sources,'untrusted_approved_preferences':preferences},settings.AI_ASSESS_MODEL,7000)
    return validate_pack(result,sources)

def save_approved_preference(owner,preference):
    return Preference.objects.create(owner=owner,text=text(preference,1000,'approved preference'))
