"""Closed workload adapter boundary. Never project arbitrary application output.

Transport owns byte/time limits and JSON decoding. This module checks version,
shape, scope and result identity before output can reach console, MCP or journals.
"""
import copy
import datetime
import re


class ProtocolError(ValueError):
    def __init__(self, code='invalid_protocol'):
        self.code = code
        super().__init__('Workload instrumentation returned an invalid or unsupported response.')


OPERATIONS = {
    'capabilities': (set(), set()),
    'queue.observe': (set(), set()),
    'queue.failures': ({'queue'}, {'limit', 'cursor'}),
    'queue.retry': ({'request_id', 'queue', 'failed_job_id', 'failure_revision'}, set()),
    'queue.receipt': ({'request_id'}, set()),
    'scheduler.observe': (set(), set()),
    'scheduler.history': (set(), {'limit', 'cursor'}),
}
ERRORS = frozenset('unsupported_driver unsupported_compatibility invalid_scope instrumentation_not_configured invalid_configuration unsupported_operation invalid_request_scope unregistered_queue invalid_request_id invalid_limit transaction_unavailable observation_timeout response_limit unsupported_payload unsupported_retry_policy invalid_cursor invalid_request request_conflict failure_changed registry_not_observed registry_limit registry_changed unsupported_schedule request_limit dependency_unavailable'.split())
HEX = r'[a-f0-9]{64}'
UUID = r'[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}'
NAME = r'[A-Za-z0-9_-]{1,80}'
KEY = r'[A-Za-z0-9_-]{16,80}'
OUTCOMES = {'started', 'completed', 'failed', 'skipped'}


def require(ok):
    if not ok:
        raise ProtocolError()


def fields(value, required, optional=()):
    require(type(value) is dict and set(required) <= set(value) <= set(required) | set(optional))


def text(value, pattern):
    require(type(value) is str and re.fullmatch(pattern, value) is not None)


def integer(value, minimum=0, maximum=9223372036854775807):
    require(type(value) is int and minimum <= value <= maximum)


def timestamp(value):
    text(value, r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)')
    try:
        require(datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).utcoffset() is not None)
    except (ValueError, OverflowError):
        raise ProtocolError() from None


def cursor(value):
    if value is not None:
        require(type(value) is str and len(value) <= 1024)
        text(value, r'[A-Za-z0-9+/]+=*\.[a-f0-9]{64}')


def items(value, maximum):
    require(type(value) is list and len(value) <= maximum)
    return value


def validate_request(value):
    fields(value, {'protocol', 'operation', 'scope'}, {k for pair in OPERATIONS.values() for group in pair for k in group})
    require(type(value['protocol']) is int and value['protocol'] == 1)
    operation = value['operation']
    require(type(operation) is str and operation in OPERATIONS)
    required, optional = OPERATIONS[operation]
    fields(value, {'protocol', 'operation', 'scope'} | required, optional)
    scope = value['scope']
    fields(scope, {'project', 'environment', 'service_id', 'definition_digest'})
    require(scope['environment'] in ('development', 'production'))
    text(scope['project'], r'[a-z0-9][a-z0-9-]{0,63}')
    text(scope['service_id'], r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}')
    text(scope['definition_digest'], HEX)
    for key, pattern in [('queue', NAME), ('request_id', KEY), ('failed_job_id', UUID), ('failure_revision', HEX)]:
        if key in value:
            text(value[key], pattern)
    if 'limit' in value:
        integer(value['limit'], 1, 100)
    if 'cursor' in value:
        cursor(value['cursor'])
    return copy.deepcopy(value)


def _receipt(result, request):
    if result.get('status') == 'absent':
        fields(result, {'request_id', 'status'})
        require(request['operation'] == 'queue.receipt')
    else:
        fields(result, {'request_id', 'status', 'failed_job_id', 'queue', 'queue_job_id', 'committed_at'})
        require(result['status'] == 'requeued')
        text(result['failed_job_id'], UUID)
        text(result['queue'], NAME)
        text(result['queue_job_id'], r'[1-9][0-9]{0,18}')
        timestamp(result['committed_at'])
        if request['operation'] == 'queue.retry':
            require(all(result[k] == request[k] for k in ('failed_job_id', 'queue')))
    require(result['request_id'] == request['request_id'])


def _result(result, request):
    require(type(result) is dict)
    operation = request['operation']
    if operation == 'capabilities':
        fields(result, {'package_version', 'package_digest', 'framework_version', 'adapter_revision', 'role', 'operations', 'retry_policy'})
        require(result['package_version'] == '1.0.0' and result['framework_version'] == '12.69.3')
        text(result['package_digest'], HEX)
        require(type(result['adapter_revision']) is int and result['adapter_revision'] == 1)
        require(result['role'] in ('queue', 'scheduler'))
        expected = ['queue.observe', 'queue.failures', 'queue.retry', 'queue.receipt'] if result['role'] == 'queue' else ['scheduler.observe', 'scheduler.history']
        require(result['operations'] == expected and result['retry_policy'] == 'registered_plain_jobs_without_retry_until')
    elif operation == 'queue.observe':
        fields(result, {'connection_id', 'driver', 'queues', 'workers'})
        text(result['connection_id'], r'[A-Za-z_][A-Za-z0-9_]{0,62}')
        require(result['driver'] == 'database')
        names = []
        for row in items(result['queues'], 20):
            fields(row, {'name', 'ready', 'delayed', 'reserved'})
            text(row['name'], NAME)
            names.append(row['name'])
            for key in ('ready', 'delayed', 'reserved'):
                integer(row[key])
        require(len(names) == len(set(names)))
        for row in items(result['workers'], 100):
            fields(row, {'instance_id', 'observed_at', 'last_outcome'})
            text(row['instance_id'], r'[A-Za-z0-9_.-]{1,60}:[0-9]{1,20}:[a-f0-9]{16}')
            timestamp(row['observed_at'])
            require(row['last_outcome'] in (None, 'processing', 'completed', 'failed'))
    elif operation in ('queue.retry', 'queue.receipt'):
        _receipt(result, request)
    elif operation == 'queue.failures':
        fields(result, {'items', 'next_cursor'})
        cursor(result['next_cursor'])
        for row in items(result['items'], request.get('limit', 25)):
            fields(row, {'failed_job_id', 'queue', 'failed_at', 'classification', 'failure_revision', 'retry_supported'})
            text(row['failed_job_id'], UUID)
            require(row['queue'] == request['queue'] and row['classification'] == 'job_failed')
            timestamp(row['failed_at'])
            text(row['failure_revision'], HEX)
            require(type(row['retry_supported']) is bool)
    elif operation == 'scheduler.observe':
        fields(result, {'registry_revision', 'items', 'next_cursor'})
        require(result['registry_revision'] == request['scope']['definition_digest'] and result['next_cursor'] is None)
        for row in items(result['items'], 100):
            fields(row, {'schedule_id', 'name', 'expression', 'timezone', 'without_overlapping', 'last_outcome', 'next_due_at', 'background_supported'})
            text(row['schedule_id'], HEX)
            text(row['name'], r'[A-Za-z0-9 _.-]{1,100}')
            text(row['expression'], r'[A-Za-z0-9*/?,#L W-]{5,120}')
            text(row['timezone'], r'[A-Za-z0-9_+/.-]{1,100}')
            require(type(row['without_overlapping']) is bool and row['background_supported'] is False)
            require(row['last_outcome'] is None or (type(row['last_outcome']) is str and row['last_outcome'] in OUTCOMES))
            timestamp(row['next_due_at'])
    else:
        fields(result, {'items', 'next_cursor'})
        cursor(result['next_cursor'])
        for row in items(result['items'], request.get('limit', 25)):
            fields(row, {'event_id', 'schedule_id', 'outcome', 'observed_at'})
            text(row['event_id'], r'[1-9][0-9]{0,18}')
            text(row['schedule_id'], HEX)
            require(type(row['outcome']) is str and row['outcome'] in OUTCOMES)
            timestamp(row['observed_at'])


def validate_response(value, request):
    request = validate_request(request)
    require(type(value) is dict)
    if value.get('status') in ('error', 'unsupported'):
        fields(value, {'protocol', 'status', 'observed_at', 'error'})
        fields(value['error'], {'code'})
        require(type(value['error']['code']) is str and value['error']['code'] in ERRORS)
    else:
        fields(value, {'protocol', 'status', 'observed_at', 'scope', 'result'})
        require(value['status'] == 'ok' and value['scope'] == request['scope'])
        _result(value['result'], request)
    require(type(value['protocol']) is int and value['protocol'] == 1)
    timestamp(value['observed_at'])
    return copy.deepcopy(value)
