"""Bounded MySQL/MariaDB inspection through an injected DB-API connection.

No driver, credentials, network configuration, or arbitrary SQL is accepted here.
The connector must use a fresh connection, bounded socket timeouts and disable
multi-statements/local infile. Initial supported accounts have direct grants only.
Rows require SELECT plus database-scoped LOCK TABLES; metadata works with SELECT.
LOCK TABLES READ fences namespace replacement while fresh metadata and bounded
rows are read. Cluster administrators are trusted, as for the PostgreSQL adapter.
Engine/version support is provisional until the corresponding live fixtures pass.
"""
import hashlib
import json
import re
import time
from cryptography.fernet import Fernet, InvalidToken


class DatabaseError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__('Database inspection could not complete: ' + code + '.')


def quote_identifier(value):
    if not isinstance(value, str) or not value or len(value) > 64 or any(ord(c) < 32 for c in value):
        raise DatabaseError('invalid_request')
    return '`' + value.replace('`', '``') + '`'


_QUOTED = r"(?:`(?:``|[^`])*`|'(?:''|[^'])*')"
_GRANT = re.compile(r'GRANT (USAGE|SELECT|LOCK TABLES|SELECT, LOCK TABLES|LOCK TABLES, SELECT) ON (\*|' + _QUOTED + r')\.(\*|' + _QUOTED + r') TO (' + _QUOTED + r')@(' + _QUOTED + r')(.*)')


def _unquote(value):
    return value[1:-1].replace(value[0]*2, value[0])


def _database_grant_name(value):
    # Database-level GRANT names are LIKE patterns even inside backticks on
    # default supported configurations. Do not accept wider wildcard authority.
    result = []
    i = 0
    while i < len(value):
        char = value[i]
        if char in ('_', '%'):
            raise DatabaseError('unsupported_privileges')
        if char == chr(92):
            i += 1
            if i >= len(value) or value[i] not in ('_', '%', chr(92)):
                raise DatabaseError('unsupported_privileges')
            char = value[i]
        result.append(char)
        i += 1
    return ''.join(result)


def classify_grants(grants, account, database):
    """Accept only completely classified, direct own-database read grants.

    SHOW GRANTS output may contain authentication hashes on MariaDB. No output
    or exceptions from this parser contain the original grant text.
    """
    if not isinstance(grants, list) or not 1 <= len(grants) <= 100:
        raise DatabaseError('unsupported_privileges')
    selected = False
    lock = False
    tables = set()
    for grant in grants:
        match = _GRANT.fullmatch(grant) if isinstance(grant, str) and len(grant) <= 8192 else None
        if not match:
            raise DatabaseError('unsupported_privileges')
        privileges, schema, table, user, host, suffix = match.groups()
        if _unquote(user) + '@' + _unquote(host) != account:
            raise DatabaseError('identity_changed')
        # MariaDB's own-account USAGE may report native-password authentication.
        # Explicitly reject every unclassified authentication/plugin/options form.
        if suffix and not (privileges == 'USAGE' and re.fullmatch(r" IDENTIFIED BY PASSWORD '\*[0-9A-F]{40}'", suffix)):
            raise DatabaseError('unsupported_privileges')
        if privileges == 'USAGE':
            if schema != '*' or table != '*':
                raise DatabaseError('unsupported_privileges')
            continue
        if schema == '*' or (_database_grant_name(_unquote(schema)) if table == '*' else _unquote(schema)) != database:
            raise DatabaseError('privileged_credential')
        if 'LOCK TABLES' in privileges:
            if table != '*':
                raise DatabaseError('unsupported_privileges')
            lock = True
        if 'SELECT' in privileges:
            selected = True
            tables.add('*' if table == '*' else _unquote(table))
    if not selected:
        raise DatabaseError('permission_denied')
    return {'lock_tables': lock, 'tables': tables}


INTEGER_TYPES = {'tinyint', 'smallint', 'mediumint', 'int', 'bigint'}
TEXT_TYPES = INTEGER_TYPES | {'decimal', 'float', 'double', 'char', 'varchar', 'tinytext', 'text', 'mediumtext', 'longtext', 'date', 'datetime', 'timestamp', 'time', 'year'}
SYSTEM_SCHEMAS = {'mysql', 'sys', 'performance_schema', 'information_schema'}


class Inspector:
    def __init__(self, connect, *, database, user, schemas, row_columns=None, cursor_key, cursor_context):
        quote_identifier(database)
        if not isinstance(user, str) or not user or len(user) > 128 or database in SYSTEM_SCHEMAS or schemas != [database]:
            raise DatabaseError('invalid_policy')
        self.connect = connect
        self.database = database
        self.user = user
        self.schemas = schemas
        self.columns = row_columns or {}
        if not isinstance(self.columns, dict):
            raise DatabaseError('invalid_policy')
        self.cipher = Fernet(cursor_key)
        self.context = hashlib.sha256(json.dumps(cursor_context, sort_keys=True, allow_nan=False).encode()).hexdigest()

    def _query(self, cursor, sql, args=(), limit=1001):
        cursor.execute(sql, args)
        rows = cursor.fetchmany(limit)
        if len(rows) >= limit:
            raise DatabaseError('result_limit')
        return rows

    def _cursor(self, token, shape):
        if token is None:
            return None
        try:
            if not isinstance(token, str) or len(token) > 8192:
                raise ValueError()
            data = json.loads(self.cipher.decrypt(token.encode(), ttl=900))
            if set(data) != {'context', 'shape', 'after'} or data['context'] != self.context or data['shape'] != shape:
                raise ValueError()
            return data['after']
        except (ValueError, TypeError, KeyError, InvalidToken):
            raise DatabaseError('cursor_stale') from None

    def _next(self, after, shape):
        return self.cipher.encrypt(json.dumps({'context': self.context, 'shape': shape, 'after': after}).encode()).decode()

    def _relation(self, c, schema, table):
        rows = self._query(c, 'SELECT TABLE_TYPE,ENGINE FROM information_schema.TABLES WHERE BINARY TABLE_SCHEMA=BINARY %s AND BINARY TABLE_NAME=BINARY %s', (schema, table))
        if len(rows) != 1:
            raise DatabaseError('table_unavailable')
        return rows[0]

    def inspect(self, request):
        if not isinstance(request, dict) or request.get('action') not in ('schemas', 'tables', 'table', 'rows'):
            raise DatabaseError('invalid_request')
        action = request['action']
        allowed = {'action', 'limit', 'cursor'} | ({'schema'} if action != 'schemas' else set()) | ({'table'} if action in ('table', 'rows') else set())
        limit = request.get('limit', 25)
        if set(request) - allowed or type(limit) is not int or not 1 <= limit <= 100:
            raise DatabaseError('invalid_request')
        schema, table = request.get('schema'), request.get('table')
        if action != 'schemas' and schema not in self.schemas:
            raise DatabaseError('permission_denied')
        if action in ('table', 'rows'):
            quote_identifier(table)
        shape = [action, schema, table, limit]
        after = self._cursor(request.get('cursor'), shape)
        conn = None
        c = None
        locked = False
        try:
            conn = self.connect()
            c = conn.cursor()
            identity = self._query(c, 'SELECT DATABASE(),CURRENT_USER(),VERSION()')[0]
            if identity[0] != self.database or not isinstance(identity[1], str) or identity[1].rsplit('@', 1)[0] != self.user:
                raise DatabaseError('identity_changed')
            version = identity[2]
            if not isinstance(version, str):
                raise DatabaseError('unsupported_engine')
            engine = 'mariadb' if 'MariaDB' in version else 'mysql'
            # Deliberately do not claim compatibility with arbitrary server forks.
            supported_version = re.match(r'^(10\.(6|11)\.|11\.(4|8)\.)', version) if engine == 'mariadb' else re.match(r'^8\.(0|4)\.', version)
            if not supported_version:
                raise DatabaseError('unsupported_engine')
            role = self._query(c, 'SELECT CURRENT_ROLE()')[0][0]
            if role not in (None, 'NONE'):
                raise DatabaseError('unsupported_privileges')
            grants = classify_grants([r[0] for r in self._query(c, 'SHOW GRANTS')], identity[1], self.database)
            c.execute('SET SESSION lock_wait_timeout=1')
            c.execute('SET SESSION innodb_lock_wait_timeout=1')
            c.execute('SET SESSION max_statement_time=3' if engine == 'mariadb' else 'SET SESSION max_execution_time=3000')
            c.execute('SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED')
            c.execute('START TRANSACTION READ ONLY')
            next_cursor = None
            if action in ('schemas', 'tables'):
                if after is not None and (not isinstance(after, str) or len(after) > 64):
                    raise DatabaseError('cursor_stale')
                if action == 'schemas':
                    rows = self._query(c, 'SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE BINARY SCHEMA_NAME=BINARY %s AND (%s IS NULL OR BINARY SCHEMA_NAME>BINARY %s) ORDER BY BINARY SCHEMA_NAME LIMIT %s', (self.database, after, after, limit + 1))
                    data = [r[0] for r in rows[:limit]]
                else:
                    rows = self._query(c, 'SELECT TABLE_NAME,TABLE_TYPE FROM information_schema.TABLES WHERE BINARY TABLE_SCHEMA=BINARY %s AND (%s IS NULL OR BINARY TABLE_NAME>BINARY %s) ORDER BY BINARY TABLE_NAME LIMIT %s', (schema, after, after, limit + 1))
                    data = [{'name': r[0], 'kind': 'r' if r[1] == 'BASE TABLE' else 'v'} for r in rows[:limit]]
                if len(rows) > limit:
                    next_cursor = self._next(rows[limit - 1][0], shape)
            else:
                if '*' not in grants['tables'] and table not in grants['tables']:
                    raise DatabaseError('permission_denied')
                relation = self._relation(c, schema, table)
                if action == 'rows':
                    if relation != ('BASE TABLE', 'InnoDB') and list(relation) != ['BASE TABLE', 'InnoDB']:
                        raise DatabaseError('unsupported_relation')
                    if not grants['lock_tables']:
                        raise DatabaseError('row_lock_privilege_required')
                    # READ locks are connection-scoped (LOCK TABLES implicitly commits).
                    # No user expression is evaluated by acquiring the lock. Re-read
                    # catalogs only after acquiring it; a swapped view then fails closed.
                    c.execute('LOCK TABLES ' + quote_identifier(schema) + '.' + quote_identifier(table) + ' READ')
                    locked = True
                    relation = self._relation(c, schema, table)
                    if list(relation) != ['BASE TABLE', 'InnoDB']:
                        raise DatabaseError('unsupported_relation')
                columns = self._query(c, 'SELECT COLUMN_NAME,DATA_TYPE,IS_NULLABLE,EXTRA,ORDINAL_POSITION,COLUMN_TYPE,GENERATION_EXPRESSION FROM information_schema.COLUMNS WHERE BINARY TABLE_SCHEMA=BINARY %s AND BINARY TABLE_NAME=BINARY %s ORDER BY ORDINAL_POSITION', (schema, table))
                indexes = self._query(c, 'SELECT INDEX_NAME,NON_UNIQUE,SEQ_IN_INDEX,COLUMN_NAME,INDEX_TYPE,SUB_PART FROM information_schema.STATISTICS WHERE BINARY TABLE_SCHEMA=BINARY %s AND BINARY TABLE_NAME=BINARY %s ORDER BY INDEX_NAME,SEQ_IN_INDEX', (schema, table))
                fingerprint = hashlib.sha256(json.dumps([relation, columns, indexes], default=str).encode()).hexdigest()
                if action == 'table':
                    if after is not None:
                        raise DatabaseError('invalid_request')
                    keys = self._query(c, "SELECT k.CONSTRAINT_NAME,CASE WHEN k.REFERENCED_TABLE_NAME IS NOT NULL THEN 'FOREIGN KEY' WHEN BINARY k.CONSTRAINT_NAME=BINARY 'PRIMARY' THEN 'PRIMARY KEY' ELSE 'UNIQUE' END,k.COLUMN_NAME,k.REFERENCED_TABLE_SCHEMA,k.REFERENCED_TABLE_NAME,k.REFERENCED_COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE k WHERE BINARY k.TABLE_SCHEMA=BINARY %s AND BINARY k.TABLE_NAME=BINARY %s ORDER BY k.CONSTRAINT_NAME,k.ORDINAL_POSITION", (schema, table))
                    grouped_keys = {}
                    for name, kind, column, rs, rt, rc in keys:
                        item = grouped_keys.setdefault(name, {'name': name, 'kind': {'PRIMARY KEY': 'p', 'UNIQUE': 'u', 'FOREIGN KEY': 'f'}.get(kind, '?'), 'columns': []})
                        item['columns'].append(column)
                        if kind == 'FOREIGN KEY':
                            if rs != self.database or ('*' not in grants['tables'] and rt not in grants['tables']):
                                item['reference'] = {'restricted': True}
                            else:
                                item.setdefault('reference', {'schema': rs, 'table': rt, 'columns': []})['columns'].append(rc)
                    grouped_indexes = {}
                    for name, nonunique, seq, col, kind, prefix in indexes:
                        grouped_indexes.setdefault(name, {'name': name, 'primary': name == 'PRIMARY', 'unique': not bool(nonunique), 'valid': True, 'columns': []})['columns'].append(col)
                    data = {'columns': [{'name': r[0], 'type': r[5], 'nullable': r[2] == 'YES'} for r in columns], 'keys': list(grouped_keys.values()), 'indexes': list(grouped_indexes.values()), 'schema_fingerprint': fingerprint}
                else:
                    selected = self.columns.get(schema, {}).get(table)
                    if not isinstance(selected, list) or not 1 <= len(selected) <= 20 or any(not isinstance(n, str) for n in selected) or len(set(selected)) != len(selected):
                        raise DatabaseError('row_policy_required')
                    lookup = {r[0]: r for r in columns}
                    if any(n not in lookup or lookup[n][1] not in TEXT_TYPES or lookup[n][6] or 'GENERATED' in lookup[n][3] for n in selected):
                        raise DatabaseError('unsupported_columns')
                    primary = [r for r in indexes if r[0] == 'PRIMARY']
                    if len(primary) != 1 or primary[0][4] != 'BTREE' or primary[0][5] is not None or primary[0][3] not in lookup:
                        raise DatabaseError('unsupported_pagination')
                    key = lookup[primary[0][3]]
                    if key[1] not in INTEGER_TYPES or key[6] or 'GENERATED' in key[3]:
                        raise DatabaseError('unsupported_pagination')
                    last = None
                    if after is not None:
                        if not isinstance(after, dict) or set(after) != {'fingerprint', 'key'} or after['fingerprint'] != fingerprint or type(after['key']) is not int or not -(2**63) <= after['key'] < 2**64:
                            raise DatabaseError('cursor_stale')
                        last = after['key']
                    limit = min(limit, max(1, 200000 // (len(selected) * 257 * 6) - 1))
                    projection = ','.join('LEFT(CAST(' + quote_identifier(n) + ' AS CHAR CHARACTER SET utf8mb4),257)' for n in selected)
                    quoted_key = quote_identifier(key[0])
                    sql = 'SELECT ' + quoted_key + ',' + projection + ' FROM ' + quote_identifier(schema) + '.' + quote_identifier(table) + ' WHERE (%s IS NULL OR ' + quoted_key + '>%s) ORDER BY ' + quoted_key + ' LIMIT %s'
                    rows = self._query(c, sql, (last, last, limit + 1))
                    if len(rows) > limit:
                        next_cursor = self._next({'key': int(rows[limit - 1][0]), 'fingerprint': fingerprint}, shape)
                    data = {'columns': selected, 'rows': [[{'value': v[:256] if v is not None else None, 'truncated': v is not None and len(v) > 256} for v in row[1:]] for row in rows[:limit]], 'schema_fingerprint': fingerprint}
            result = {'engine': engine, 'identity': {'database': identity[0], 'user': self.user, 'account': identity[1], 'server_version': version, 'cluster_identity_verified': False}, 'data': data, 'next_cursor': next_cursor, 'page_size': limit, 'observed_at': time.time()}
            if len(json.dumps(result, allow_nan=False).encode()) > 240000:
                raise DatabaseError('result_limit')
            return result
        except DatabaseError:
            raise
        except Exception as error:
            number = error.args[0] if getattr(error, 'args', None) and type(error.args[0]) is int else None
            safe = 'connection_failed' if conn is None or number in (1045, 1049, 2002, 2003, 2006, 2013) else 'timeout' if number in (1205, 1213, 1317, 1969, 3024) else 'permission_denied' if number in (1044, 1142, 1143, 1227) else 'query_failed'
            raise DatabaseError(safe) from None
        finally:
            if locked and c is not None:
                try:c.execute('UNLOCK TABLES')
                except Exception:pass
            if c is not None:
                try:c.close()
                except Exception:pass
            if conn is not None:
                try:conn.rollback()
                except Exception:pass
                try:conn.close()
                except Exception:pass
