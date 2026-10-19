"""Linux-only isolated parser. It receives document bytes, never app credentials."""
import json,os,socketserver,struct,subprocess,sys,tempfile,threading
from pathlib import Path

LIMIT=10*1024*1024
slots=threading.BoundedSemaphore(2)

def read_exact(stream,size):
    parts=[]
    while size:
        part=stream.read(min(size,65536))
        if not part: raise ValueError('Incomplete input')
        parts.append(part); size-=len(part)
    return b''.join(parts)

def limits():
    import resource
    resource.setrlimit(resource.RLIMIT_CPU,(15,15))
    resource.setrlimit(resource.RLIMIT_AS,(256*1024*1024,256*1024*1024))
    resource.setrlimit(resource.RLIMIT_FSIZE,(12*1024*1024,12*1024*1024))
    resource.setrlimit(resource.RLIMIT_NOFILE,(32,32))

class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(25)
        if not slots.acquire(blocking=False):
            self.wfile.write(b'{"error":"busy"}'); return
        try:
            size=struct.unpack('!I',read_exact(self.rfile,4))[0]
            if not 0<size<=512: raise ValueError
            header=json.loads(read_exact(self.rfile,size))
            if header=={'operation':'ping'}:
                self.wfile.write(b'{"status":"ready"}'); return
            format=header.get('format'); length=header.get('size')
            if format not in ('.txt','.pdf','.docx') or type(length) is not int or not 0<length<=LIMIT: raise ValueError
            with tempfile.TemporaryDirectory(prefix='parse-') as directory:
                path=Path(directory)/('document'+format)
                path.write_bytes(read_exact(self.rfile,length))
                result=subprocess.run([sys.executable,'-I','/parser/limited_parse.py',str(path),format],capture_output=True,timeout=20,env={'PATH':'/usr/local/bin:/usr/bin:/bin','LANG':'C.UTF-8'})
                if result.returncode or len(result.stdout)>1500000: raise ValueError
                blocks=json.loads(result.stdout)
                self.wfile.write(json.dumps({'blocks':blocks}).encode())
        except Exception:
            try: self.wfile.write(b'{"error":"document_rejected"}')
            except OSError: pass
        finally: slots.release()

class Server(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):
    daemon_threads=True
    request_queue_size=4

if __name__=='__main__':
    os.umask(0o007)
    path=Path('/run/parser/parser.sock')
    path.unlink(missing_ok=True)
    with Server(str(path),Handler) as server: server.serve_forever()
