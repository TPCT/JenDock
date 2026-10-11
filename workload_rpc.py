"""Bounded private workload RPC, with a kernel-verified worker identity."""
import json
import socket
import database_rpc


def request(payload, path='/state/control.sock'):
    encoded=json.dumps(payload,allow_nan=False).encode()+b'\n'
    if len(encoded)>16384:raise ValueError('Workload request is too large.')
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
        client.settimeout(120)
        client.connect(path)
        if database_rpc.peer_uid(client)!=database_rpc.expected_uid('WORKLOAD_WORKER_UID',0):
            raise ValueError('Workload worker identity could not be verified.')
        client.sendall(encoded)
        with client.makefile('rb') as source:raw=source.readline(262145)
        if len(raw)>262144 or not raw.endswith(b'\n'):raise ValueError('Invalid workload response.')
        result=database_rpc.decode(raw)
        if not isinstance(result,dict) or type(result.get('ok')) is not bool:raise ValueError('Invalid workload response.')
        return result
