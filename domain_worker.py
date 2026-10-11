"""Worker-side ingress runtime assembly. Caller holds the deployment control lock."""
import json
import math
import os
from pathlib import Path
import re
import time

import ingress_capacity
import ingress_ports
import ingress_runtime


def configuration():
    if os.environ.get('INGRESS_ENABLED') != '1':
        return None
    image = os.environ.get('INGRESS_GATEWAY_IMAGE', '')
    socket = os.environ.get('INGRESS_HOST_SOCKET', '')
    if not re.fullmatch(ingress_runtime.IMAGE_PATTERN, image):
        raise ValueError('Configure a pinned gateway image before enabling domain execution.')
    if not socket or not Path(socket).is_absolute() or '..' in Path(socket).parts:
        raise ValueError('Configure an absolute host-helper socket.')
    memory = int(os.environ.get('INGRESS_GATEWAY_MEMORY_MIB', '32'))
    cpu = float(os.environ.get('INGRESS_GATEWAY_CPU', '0.1'))
    if not 16 <= memory <= 4096 or not math.isfinite(cpu) or not .05 <= cpu <= 8:
        raise ValueError('Configure bounded gateway resource limits.')
    return {'image': image, 'socket': socket, 'resources': {'memory_mib': memory, 'cpu': cpu}}


class Gateway:
    def __init__(self, *, root, docker, compose, host_client, image, resources):
        self.root = Path(root)
        self.docker, self.compose = docker, compose
        self.host_client, self.image, self.resources = host_client, image, resources

    def __call__(self, db, definition, operation_id, *, authorize, reconcile=False):
        authorize()
        ingress_runtime.initialize(db)
        project, environment = definition['project'], definition['environment']
        row = db.execute('SELECT * FROM ingress_runtime_intents WHERE project=? AND environment=?', (project, environment)).fetchone()
        if row:
            gateway = json.loads(row['definition'])
            if ingress_runtime.digest(gateway) != row['digest'] or Path(row['root']) != self.root.resolve():
                raise ValueError('Gateway intent identity or root changed.')
            import ingress_route_generations
            addition = ingress_route_generations.operation(db, operation_id)
            if addition and addition['phase'] not in ('committed', 'aborted'):
                # A candidate may already be on disk/in memory. Ordinary verification
                # against the previous head cannot precede journal reconciliation.
                route = {'hostname': definition['hostname'], 'service_id': definition['service_id']}
                ingress_runtime.prepare_route_addition(db, gateway, operation_id, route,
                    root=self.root, docker=self.docker, authorize=authorize)
                ingress_runtime.recover_route_addition(db, gateway, operation_id,
                    root=self.root, docker=self.docker, authorize=authorize)
            elif addition and addition['phase'] == 'aborted':
                if not reconcile:
                    raise ValueError('Gateway route addition was aborted; explicit domain resume is required.')
                route = {'hostname': definition['hostname'], 'service_id': definition['service_id']}
                # Validate the original accepted request before refreshing a safe
                # pre-signal attempt; this cannot adopt another route's journal.
                ingress_runtime.prepare_route_addition(db, gateway, operation_id, route,
                    root=self.root, docker=self.docker, authorize=authorize)
                ingress_runtime.retry_aborted_route_addition(db, gateway, operation_id,
                    root=self.root, docker=self.docker, authorize=authorize)
                ingress_runtime.apply_route_addition(db, gateway, operation_id,
                    root=self.root, docker=self.docker, authorize=authorize)
            # The original operation owns physical resources for the environment.
            # Later domain operations append routes without taking those resources.
            if row['phase'] != 'verified':
                if gateway['operation_id'] != operation_id:
                    raise ValueError('An earlier gateway operation requires reconciliation.')
                binding = ingress_runtime.apply_gateway(db, gateway, root=self.root, docker=self.docker, compose=self.compose, authorize=authorize)
            else:
                binding = ingress_runtime.verify_gateway(db, gateway, root=self.root, docker=self.docker, compose=self.compose, authorize=authorize)
            import ingress_bindings
            routes = ingress_bindings.current_routes(db, gateway)
            matching = [r for r in routes if r['hostname'] == definition['hostname']]
            if matching:
                if len(matching) != 1 or matching[0]['service_id'] != definition['service_id'] or matching[0]['http_endpoint'] != definition['http_endpoint']:
                    raise ValueError('Existing hostname targets a different application.')
            else:
                route = {'hostname': definition['hostname'], 'service_id': definition['service_id']}
                ingress_runtime.prepare_route_addition(db, gateway, operation_id, route, root=self.root, docker=self.docker, authorize=authorize)
                effect = ingress_runtime.recover_route_addition if reconcile else ingress_runtime.apply_route_addition
                effect(db, gateway, operation_id, root=self.root, docker=self.docker, authorize=authorize)
                binding = ingress_runtime.verify_gateway(db, gateway, root=self.root, docker=self.docker, compose=self.compose, authorize=authorize)
            effective = dict(gateway, routes=ingress_bindings.current_routes(db, gateway))
            ingress_runtime._wait_http(effective, self.docker, binding['container_id'], authorize)
            binding = ingress_runtime.verify_gateway(db, gateway, root=self.root, docker=self.docker, compose=self.compose, authorize=authorize)
            return dict(binding, http_verified=True)
        # Check the host namespace, not the worker container's own loopback.
        ports = self.host_client.call('available_ports', {})
        if (not isinstance(ports, dict) or not isinstance(ports.get('ports'), list)
                or not 1 <= len(ports['ports']) <= 4096
                or any(type(p) is not int or not 1024 <= p <= 65535 for p in ports['ports'])
                or len(set(ports['ports'])) != len(ports['ports'])
                or type(ports.get('observed')) not in (int, float)
                or not 0 <= time.time() - ports['observed'] <= 30):
            raise ValueError('Fresh host port availability is required.')
        free = set(ports['ports'])
        if max(free) - min(free) > 4095:
            raise ValueError('Host port range exceeds its bound.')
        authorize()
        if self.root.is_symlink() or not self.root.is_absolute():
            raise ValueError('Managed gateway root must be an absolute directory.')
        self.root.mkdir(mode=0o700, parents=False, exist_ok=True)
        # These are retained reservations, not claims that the OS port is leased.
        # Docker binding and runtime identity verification remain authoritative.
        ingress_capacity.reserve(db, project, environment, operation_id, self.resources, definition['policy_revision'])
        ingress_ports.reserve(db, project, environment, operation_id, first=min(free), last=max(free), occupied_ports=set(), available=lambda p: p in free)
        gateway = ingress_runtime.prepare_gateway(db, project, environment, operation_id,
            [{'hostname': definition['hostname'], 'service_id': definition['service_id']}],
            image=self.image, resources=self.resources, docker=self.docker)
        return ingress_runtime.apply_gateway(db, gateway, root=self.root, docker=self.docker, compose=self.compose, authorize=authorize)
