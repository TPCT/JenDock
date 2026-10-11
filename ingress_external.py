"""Bounded public HTTPS route proof. This evidence alone never establishes Ready.

Injection points are trusted Python test/administrator capabilities, not request
fields. The production transport connects to one validated numeric address and
verifies the original hostname with TLS SNI and normal CA trust.
"""
import hashlib
import ipaddress
import math
import queue
import re
import socket
import ssl
import threading
import time

PATH='/.well-known/deploy-monitor/route-proof'
HEADER_LIMIT=16384
BODY_LIMIT=128
HOST=re.compile(r'(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z')

class ExternalError(ValueError):pass


def _remaining(deadline):
    remaining=deadline-time.monotonic()
    if remaining<=0:raise ExternalError('External HTTPS observation timed out.')
    return remaining


def public_addresses(addresses):
    if not isinstance(addresses,(list,tuple)) or not 1<=len(addresses)<=16:raise ExternalError('Public hostname resolution is unavailable.')
    result=[]
    for raw in addresses:
        if not isinstance(raw,str) or '%' in raw:raise ExternalError('Public hostname resolution is invalid.')
        try:address=ipaddress.ip_address(raw)
        except ValueError:raise ExternalError('Public hostname resolution is invalid.') from None
        if (not address.is_global or address.is_multicast or address.is_reserved or address.is_unspecified
                or isinstance(address,ipaddress.IPv6Address) and (address.ipv4_mapped or address.sixtofour or address.teredo)):
            raise ExternalError('External verification requires public network destinations.')
        normalized=str(address)
        if normalized not in result:result.append(normalized)
    return result


# System DNS cannot be cancelled portably. Limit outstanding resolver threads so
# repeated timeouts cannot create an unbounded pool of blocked lookups.
_RESOLVERS=threading.BoundedSemaphore(4)
def _resolve(hostname,deadline):
    if not _RESOLVERS.acquire(blocking=False):raise ExternalError('Public hostname resolver is busy.')
    result=queue.Queue(maxsize=1)
    def lookup():
        try:result.put((True,[row[4][0] for row in socket.getaddrinfo(hostname,443,type=socket.SOCK_STREAM)]))
        except Exception:result.put((False,None))
        finally:_RESOLVERS.release()
    threading.Thread(target=lookup,daemon=True).start()
    try:ok,addresses=result.get(timeout=min(4,_remaining(deadline)))
    except queue.Empty:raise ExternalError('Public hostname resolution timed out.') from None
    if not ok:raise ExternalError('Public hostname resolution failed.')
    return addresses


class _Reader:
    def __init__(self,connection,deadline):self.connection=connection;self.deadline=deadline;self.buffer=bytearray()
    def receive(self):
        self.connection.settimeout(_remaining(self.deadline))
        chunk=self.connection.recv(4096)
        if not chunk:raise ExternalError('External HTTPS response was incomplete.')
        self.buffer.extend(chunk)
    def until(self,separator,limit):
        while True:
            index=self.buffer.find(separator)
            if index>=0:
                end=index+len(separator)
                if end>limit:raise ExternalError('External HTTPS response exceeded its bound.')
                data=bytes(self.buffer[:index]);del self.buffer[:end];return data
            if len(self.buffer)>=limit:raise ExternalError('External HTTPS response exceeded its bound.')
            self.receive()
    def exact(self,length):
        if not 0<=length<=BODY_LIMIT:raise ExternalError('External HTTPS body exceeded its bound.')
        while len(self.buffer)<length:self.receive()
        result=bytes(self.buffer[:length]);del self.buffer[:length];return result


def _response(connection,deadline):
    reader=_Reader(connection,deadline);head=reader.until(b'\r\n\r\n',HEADER_LIMIT)
    lines=head.split(b'\r\n')
    if not re.fullmatch(rb'HTTP/1\.[01] 200(?: [\x20-\x7e]{0,128})?',lines[0]):raise ExternalError('External HTTPS did not return a successful proof.')
    headers={}
    if len(lines)>65:raise ExternalError('External HTTPS headers exceeded their bound.')
    for line in lines[1:]:
        if b':' not in line:raise ExternalError('External HTTPS headers are invalid.')
        key,value=line.split(b':',1);key=key.lower();value=value.strip()
        if not re.fullmatch(rb'[a-z0-9!#$%&\'*+.^_`|~-]+',key) or key in headers or any(c<32 or c==127 for c in value):raise ExternalError('External HTTPS headers are invalid.')
        headers[key]=value
    if headers.get(b'content-encoding',b'identity').lower()!=b'identity':raise ExternalError('Encoded external proof is unsupported.')
    transfer=headers.get(b'transfer-encoding');length=headers.get(b'content-length')
    if transfer:
        if transfer.lower()!=b'chunked' or length is not None:raise ExternalError('External HTTPS framing is ambiguous.')
        body=bytearray()
        for _ in range(16):
            size=reader.until(b'\r\n',32)
            if not re.fullmatch(rb'[0-9a-fA-F]{1,8}',size):raise ExternalError('External HTTPS chunk framing is invalid.')
            amount=int(size,16)
            if amount==0:
                if reader.exact(2)!=b'\r\n':raise ExternalError('External HTTPS trailers are unsupported.')
                return bytes(body)
            if len(body)+amount>BODY_LIMIT:raise ExternalError('External HTTPS body exceeded its bound.')
            body.extend(reader.exact(amount))
            if reader.exact(2)!=b'\r\n':raise ExternalError('External HTTPS chunk framing is invalid.')
        raise ExternalError('External HTTPS chunks exceeded their bound.')
    if length is not None:
        if not re.fullmatch(rb'[0-9]{1,3}',length):raise ExternalError('External HTTPS content length is invalid.')
        return reader.exact(int(length))
    body=reader.buffer
    while len(body)<=BODY_LIMIT:
        connection.settimeout(_remaining(deadline));chunk=connection.recv(min(4096,BODY_LIMIT+1-len(body)))
        if not chunk:return bytes(body)
        body.extend(chunk)
    raise ExternalError('External HTTPS body exceeded its bound.')


def _transport(hostname,addresses,deadline,context):
    address=addresses[0]  # No hidden resolver retries, redirects or alternate URLs.
    family=socket.AF_INET6 if ':' in address else socket.AF_INET
    with socket.socket(family,socket.SOCK_STREAM) as raw:
        raw.settimeout(_remaining(deadline));raw.connect((address,443))
        raw.settimeout(_remaining(deadline))
        with context.wrap_socket(raw,server_hostname=hostname) as secured:
            secured.settimeout(_remaining(deadline))
            request=('GET '+PATH+' HTTP/1.1\r\nHost: '+hostname+'\r\nAccept: text/plain\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n').encode('ascii')
            secured.sendall(request)
            certificate=secured.getpeercert(binary_form=True)
            if not certificate:raise ExternalError('External HTTPS certificate is unavailable.')
            body=_response(secured,deadline)
            return {'status':200,'body':body,'peer_address':address,'certificate_fingerprint':hashlib.sha256(certificate).hexdigest()}


def verify(hostname,expected_token,*,timeout=10,ca_file=None,resolver=None,transport=None):
    """Trusted injected transport(hostname, addresses, deadline, ssl_context).

    With a custom transport, a custom resolver may supply fixture-private IPs;
    production transport always enforces public addresses, even with a resolver.
    proof_digest is SHA256 of the exact public token's ASCII bytes.
    """
    if not isinstance(hostname,str) or not HOST.fullmatch(hostname):raise ExternalError('Use an exact normalized public hostname.')
    try:ipaddress.ip_address(hostname)
    except ValueError:pass
    else:raise ExternalError('External verification requires a hostname.')
    if not isinstance(expected_token,str) or not re.fullmatch('[a-f0-9]{64}',expected_token):raise ExternalError('An exact public route proof token is required.')
    if type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0<timeout<=15:raise ExternalError('External verification deadline is invalid.')
    deadline=time.monotonic()+timeout
    try:
        context=ssl.create_default_context(cafile=ca_file)
        context.minimum_version=ssl.TLSVersion.TLSv1_2
        addresses=(resolver or _resolve)(hostname,deadline)
        if transport is None or resolver is None:addresses=public_addresses(addresses)
        elif not isinstance(addresses,(list,tuple)) or not 1<=len(addresses)<=16:raise ExternalError('Fixture resolution is invalid.')
        _remaining(deadline)
        response=(transport or _transport)(hostname,addresses,deadline,context)
        _remaining(deadline)
        if (not isinstance(response,dict) or response.get('status')!=200 or response.get('body')!=expected_token.encode('ascii')
                or response.get('peer_address') not in addresses or not re.fullmatch('[a-f0-9]{64}',str(response.get('certificate_fingerprint','')))):
            raise ExternalError('External HTTPS route proof is unverified.')
        return {'hostname':hostname,'status':'verified','observed_at':time.time(),'proof_digest':hashlib.sha256(expected_token.encode()).hexdigest(),
                'peer_address':response['peer_address'],'certificate_fingerprint':response['certificate_fingerprint'],'ready':False}
    except ExternalError:raise
    except Exception:raise ExternalError('External HTTPS route proof is unavailable.') from None
