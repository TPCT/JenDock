"""Bounded database-only Unix RPC with kernel peer identity checks."""
import json
import os
import socket
import struct


def peer_uid(connection):
    return struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]


def expected_uid(variable, default):
    value = os.environ.get(variable, str(default))
    if not value.isascii() or not value.isdecimal():
        raise ValueError('Database control identity is not configured.')
    return int(value)


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate control field.')
            result[key] = value
        return result
    def constant(_):
        raise ValueError('Invalid control number.')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def request(payload, path='/state/control.sock'):
    encoded = json.dumps(payload, allow_nan=False).encode() + b'\n'
    if len(encoded) > 65536:
        raise ValueError('Database configuration request is too large.')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(15)
        client.connect(path)
        if peer_uid(client) != expected_uid('DATABASE_WORKER_UID', 0):
            raise ValueError('Database control identity could not be verified.')
        client.sendall(encoded)
        with client.makefile('rb') as source:
            raw = source.readline(262145)
        if len(raw) > 262144 or not raw.endswith(b'\n'):
            raise ValueError('Invalid database control response.')
        value = decode(raw)
        if not isinstance(value, dict) or type(value.get('ok')) is not bool:
            raise ValueError('Invalid database control response.')
        return value
