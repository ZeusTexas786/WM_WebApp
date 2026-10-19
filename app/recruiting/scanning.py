import socket
import struct
from pathlib import Path
from django.conf import settings

def scanner_ready():
    try:
        with socket.create_connection((settings.SCANNER_HOST,settings.SCANNER_PORT),timeout=2) as client:
            client.settimeout(2)
            client.sendall(b'zPING\0')
            return client.recv(32).rstrip(b'\0\n')==b'PONG'
    except OSError: return False

def scan_stream(stream,host,port):
    if not host: return 'unavailable'
    try:
        with socket.create_connection((host,port),timeout=10) as client:
            client.settimeout(20)
            client.sendall(b'zINSTREAM\0')
            total=0
            while True:
                chunk=stream.read(65536)
                if not chunk: break
                total+=len(chunk)
                if total>10*1024*1024: return 'unavailable'
                client.sendall(struct.pack('!I',len(chunk))+chunk)
            client.sendall(b'\0\0\0\0')
            response=b''
            while len(response)<2048 and b'\0' not in response:
                chunk=client.recv(512)
                if not chunk: break
                response+=chunk
            message=response.rstrip(b'\0\n').decode('utf-8',errors='replace')
            if message=='stream: OK': return 'clean'
            if message.startswith('stream: ') and message.endswith(' FOUND'): return 'infected'
    except (OSError,ValueError): pass
    return 'unavailable'

def scan_path(path):
    with Path(path).open('rb') as stream: return scan_stream(stream,settings.SCANNER_HOST,settings.SCANNER_PORT)
