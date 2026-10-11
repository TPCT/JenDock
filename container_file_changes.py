"""Bounded Docker change metadata; no file contents or shell execution."""
import http.client
import json
import os
import re
import socket


def read(container_id):
    endpoint = os.environ.get('DOCKER_HOST', 'unix:///var/run/docker.sock')
    if not isinstance(container_id, str) or not re.fullmatch('[a-f0-9]{64}', container_id) or not endpoint.startswith('unix://'):
        return None

    class Connection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect(endpoint[7:])

    connection = Connection('localhost', timeout=3)
    try:
        connection.request('GET', '/containers/' + container_id + '/changes')
        response = connection.getresponse()
        if response.status != 200:
            return None
        raw = response.read(524289)
        if len(raw) > 524288:
            return None
        value = json.loads(raw)
        return value if isinstance(value, list) and len(value) <= 4096 else None
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        connection.close()
