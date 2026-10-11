"""Fixed private MySQL-family application identity probe; no grant inspection.

Application accounts may write. This probe verifies identity only, not application
privileges, cluster identity or database integrity. Connector socket timeouts and
the enclosing helper deadline bound execution.
"""
import re
from database_mysql import DatabaseError, SYSTEM_SCHEMAS


def validate(value):
    if not isinstance(value, dict) or set(value) != {'action', 'engine', 'database', 'user', 'password', 'port'}:
        raise DatabaseError('invalid_request')
    if value['action'] != 'application-identity' or value['engine'] not in ('mysql', 'mariadb'):
        raise DatabaseError('invalid_request')
    if type(value['port']) is not int or not 1 <= value['port'] <= 65535:
        raise DatabaseError('invalid_request')
    for key, maximum in (('database', 64), ('user', 128), ('password', 4096)):
        item = value[key]
        if not isinstance(item, str) or not 1 <= len(item) <= maximum or '\x00' in item:
            raise DatabaseError('invalid_request')
        if key != 'password' and any(ord(c) < 32 or ord(c) == 127 for c in item):
            raise DatabaseError('invalid_request')
    if value['database'] in SYSTEM_SCHEMAS:
        raise DatabaseError('invalid_request')
    return value


def inspect(value, connect):
    validate(value)
    connection = cursor = None
    try:
        connection = connect()
        cursor = connection.cursor()
        cursor.execute('SELECT DATABASE(),CURRENT_USER(),VERSION()')
        rows = cursor.fetchmany(2)
        if len(rows) != 1 or len(rows[0]) != 3:
            raise DatabaseError('identity_changed')
        database, account, version = rows[0]
        if database != value['database'] or not isinstance(account, str) or len(account) > 512 or account.rsplit('@', 1)[0] != value['user']:
            raise DatabaseError('identity_changed')
        if not isinstance(version, str) or not 1 <= len(version) <= 128 or any(ord(c) < 32 or ord(c) == 127 for c in version):
            raise DatabaseError('unsupported_engine')
        engine = 'mariadb' if 'MariaDB' in version else 'mysql'
        if engine != value['engine']:
            raise DatabaseError('identity_changed')
        supported = re.match(r'^(10\.(6|11)\.|11\.(4|8)\.)', version) if engine == 'mariadb' else re.match(r'^8\.(0|4)\.', version)
        if not supported:
            raise DatabaseError('unsupported_engine')
        return {'engine': engine, 'database': database, 'server_version': version,
                'cluster_identity_verified': False}
    except DatabaseError:
        raise
    except Exception:
        raise DatabaseError('connection_failed') from None
    finally:
        for resource in (cursor, connection):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
