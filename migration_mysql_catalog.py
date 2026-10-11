"""Bounded, read-only MySQL-family migration metadata; no DDL or ledger.

Caller owns a fresh authenticated connection, socket/deadline bounds and migration
serialization. Metadata reads are not an atomic schema snapshot: protocol callers
must fence and compare observations around effects. Cluster identity is unproved.
Foreign-key metadata is privilege-filtered: absence means none observed, not
global absence. Execution must preserve existing columns/keys, leave engine
foreign-key enforcement enabled, and never infer destructive DDL is safe here.
"""
import re
import json
from migration_mysql import CompileError, SYSTEM_DATABASES, identifier, compile_operation, _column


class CatalogError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__('MySQL migration catalog failed: ' + code + '.')


IDENTITY = 'SELECT DATABASE(),CURRENT_USER(),VERSION()'
SETTINGS = ('SELECT @@SESSION.explicit_defaults_for_timestamp,@@SESSION.default_storage_engine,'
            '@@SESSION.sql_mode,@@SESSION.character_set_client,@@SESSION.character_set_connection,'
            '@@SESSION.collation_connection,@@lower_case_table_names')
DEFAULTS = ('SELECT DEFAULT_CHARACTER_SET_NAME,DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA '
            'WHERE BINARY SCHEMA_NAME=BINARY %s')
PARTIAL_REVOKES = 'SELECT @@GLOBAL.partial_revokes'
FOREIGN_KEY_CHECKS = 'SELECT @@SESSION.foreign_key_checks'
GLOBAL_TRIGGER = ("SELECT PRIVILEGE_TYPE FROM information_schema.USER_PRIVILEGES "
                  "WHERE BINARY GRANTEE=BINARY %s AND PRIVILEGE_TYPE='TRIGGER'")
SCHEMA_TRIGGER = ("SELECT TABLE_SCHEMA FROM information_schema.SCHEMA_PRIVILEGES "
                  "WHERE BINARY GRANTEE=BINARY %s AND PRIVILEGE_TYPE='TRIGGER'")
TABLE_TRIGGER = ("SELECT TABLE_NAME FROM information_schema.TABLE_PRIVILEGES "
                 "WHERE BINARY GRANTEE=BINARY %s AND PRIVILEGE_TYPE='TRIGGER' "
                 "AND BINARY TABLE_SCHEMA=BINARY %s AND BINARY TABLE_NAME=BINARY %s")
TABLE = ('SELECT TABLE_TYPE,ENGINE,TABLE_COLLATION,CREATE_OPTIONS FROM information_schema.TABLES '
         'WHERE BINARY TABLE_SCHEMA=BINARY %s AND BINARY TABLE_NAME=BINARY %s')
COLUMNS = ('SELECT COLUMN_NAME,ORDINAL_POSITION,DATA_TYPE,COLUMN_TYPE,IS_NULLABLE,COLUMN_DEFAULT,EXTRA,'
           'CHARACTER_MAXIMUM_LENGTH,NUMERIC_PRECISION,NUMERIC_SCALE,DATETIME_PRECISION,'
           'CHARACTER_SET_NAME,COLLATION_NAME,GENERATION_EXPRESSION FROM information_schema.COLUMNS '
           'WHERE BINARY TABLE_SCHEMA=BINARY %s AND BINARY TABLE_NAME=BINARY %s ORDER BY ORDINAL_POSITION')
INDEXES = ('SELECT INDEX_NAME,NON_UNIQUE,SEQ_IN_INDEX,COLUMN_NAME,COLLATION,SUB_PART,INDEX_TYPE '
           'FROM information_schema.STATISTICS WHERE BINARY TABLE_SCHEMA=BINARY %s '
           'AND BINARY TABLE_NAME=BINARY %s ORDER BY INDEX_NAME,SEQ_IN_INDEX')
INDEX_VISIBILITY = ('SELECT INDEX_NAME,' + '{visibility}' + ' FROM information_schema.STATISTICS '
                    'WHERE BINARY TABLE_SCHEMA=BINARY %s AND BINARY TABLE_NAME=BINARY %s '
                    'ORDER BY INDEX_NAME,SEQ_IN_INDEX')
TRIGGERS = ('SELECT TRIGGER_NAME FROM information_schema.TRIGGERS WHERE BINARY EVENT_OBJECT_SCHEMA=BINARY %s '
            'AND BINARY EVENT_OBJECT_TABLE=BINARY %s ORDER BY TRIGGER_NAME')
CONSTRAINTS = ('SELECT CONSTRAINT_NAME,CONSTRAINT_TYPE FROM information_schema.TABLE_CONSTRAINTS '
               'WHERE BINARY TABLE_SCHEMA=BINARY %s AND BINARY TABLE_NAME=BINARY %s ORDER BY CONSTRAINT_NAME')
REFERENCES = ('SELECT CONSTRAINT_SCHEMA,TABLE_NAME,CONSTRAINT_NAME,COLUMN_NAME,REFERENCED_TABLE_SCHEMA,'
              'REFERENCED_TABLE_NAME,REFERENCED_COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE '
              'WHERE REFERENCED_TABLE_NAME IS NOT NULL AND ((BINARY TABLE_SCHEMA=BINARY %s '
              'AND BINARY TABLE_NAME=BINARY %s) OR (BINARY REFERENCED_TABLE_SCHEMA=BINARY %s '
              'AND BINARY REFERENCED_TABLE_NAME=BINARY %s)) '
              'ORDER BY CONSTRAINT_SCHEMA,TABLE_NAME,CONSTRAINT_NAME,ORDINAL_POSITION')


class Catalog:
    def __init__(self, cursor, *, database, user, engine):
        try:
            identifier(database)
        except CompileError:
            raise CatalogError('invalid_target') from None
        if (database.lower() in SYSTEM_DATABASES or engine not in ('mysql', 'mariadb')
                or not isinstance(user, str) or not 1 <= len(user) <= 128
                or any(ord(c) < 32 or ord(c) == 127 for c in user)):
            raise CatalogError('invalid_target')
        self.cursor, self.database, self.user, self.engine = cursor, database, user, engine
        self._identity = None

    def query(self, sql, args=(), limit=1001):
        """Trusted fixed read queries only; SQL is never accepted from user input."""
        if (not isinstance(sql, str) or len(sql) > 16384 or not sql.startswith('SELECT ')
                or ';' in sql or type(args) is not tuple or type(limit) is not int or not 2 <= limit <= 10001):
            raise CatalogError('invalid_query')
        try:
            self.cursor.execute(sql, args)
            rows = self.cursor.fetchmany(limit)
            if len(rows) >= limit:
                raise CatalogError('result_limit')
            total = 0; result = []
            for row in rows:
                if not isinstance(row, (tuple, list)) or len(row) > 32:
                    raise CatalogError('invalid_metadata')
                for value in row:
                    if value is not None and type(value) not in (str, int, bool):
                        raise CatalogError('invalid_metadata')
                    if isinstance(value, str):
                        total += len(value.encode('utf-8'))
                        if len(value) > 16384 or total > 1024 * 1024:
                            raise CatalogError('result_limit')
                result.append(tuple(row))
            return tuple(result)
        except CatalogError:
            raise
        except Exception:
            raise CatalogError('catalog_query_failed') from None

    def _one(self, sql, args, size):
        rows = self.query(sql, args, limit=2)
        if len(rows) != 1 or len(rows[0]) != size:
            raise CatalogError('identity_changed')
        return rows[0]

    def identity(self):
        database, account, version = self._one(IDENTITY, (), 3)
        if (database != self.database or not isinstance(account, str) or len(account) > 512
                or '@' not in account or account.rsplit('@', 1)[0] != self.user):
            raise CatalogError('identity_changed')
        if not isinstance(version, str) or not 1 <= len(version) <= 128 or any(ord(c) < 32 or ord(c) == 127 for c in version):
            raise CatalogError('unsupported_engine')
        engine = 'mariadb' if 'MariaDB' in version else 'mysql'
        if engine != self.engine:
            raise CatalogError('identity_changed')
        pattern = r'^(10\.(6|11)\.|11\.(4|8)\.)' if engine == 'mariadb' else r'^8\.(0|4)\.'
        if not re.match(pattern, version):
            raise CatalogError('unsupported_engine')
        settings = self._one(SETTINGS, (), 7)
        defaults = self._one(DEFAULTS, (self.database,), 2)
        if self._one(FOREIGN_KEY_CHECKS, (), 1) != (1,):
            raise CatalogError('unsafe_session')
        explicit, storage, mode, client, connection, collation, case = settings
        if explicit not in (1, 'ON') or storage != 'InnoDB':
            raise CatalogError('unsafe_session')
        if (not isinstance(mode, str) or any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9_]+', v)
                for v in (client, connection, collation) + defaults) or type(case) is not int or case != 0):
            raise CatalogError('unsafe_session')
        # Strict mode prevents successful DDL with silent type/default coercion.
        modes = tuple(sorted(set(filter(None, mode.split(',')))))
        if not {'STRICT_TRANS_TABLES', 'STRICT_ALL_TABLES'}.intersection(modes):
            raise CatalogError('unsafe_session')
        proof = (database, account, version, settings[:2] + (modes,) + settings[3:], defaults)
        if self._identity is not None and self._identity != proof:
            raise CatalogError('identity_changed')
        self._identity = proof
        return {'engine': engine, 'database': database, 'user': self.user, 'account': account,
                'server_version': version, 'session': proof[3], 'database_defaults': defaults,
                'foreign_key_checks': True, 'incoming_reference_coverage': 'visible_only',
                'cluster_identity_verified': False}

    def _trigger_visibility(self, name):
        # Empty TRIGGERS is meaningful only with TRIGGER visibility. Direct
        # grants are sufficient; role-only grants are not inferred. Partial
        # revokes invalidate a bare global-grant proof on MySQL.
        proof = self.identity()
        user, host = proof['account'].rsplit('@', 1)
        if any(c in user + host for c in "'\\"):
            raise CatalogError('metadata_visibility_unverified')
        grantee = "'" + user + "'@'" + host + "'"
        partial = self._one(PARTIAL_REVOKES, (), 1)[0] if self.engine == 'mysql' else 0
        if type(partial) is not int or partial not in (0, 1):
            raise CatalogError('metadata_visibility_unverified')
        if not partial and self.query(GLOBAL_TRIGGER, (grantee,)) == (('TRIGGER',),):
            return
        for row in self.query(SCHEMA_TRIGGER, (grantee,)):
            if len(row) != 1 or not isinstance(row[0], str):
                raise CatalogError('metadata_visibility_unverified')
            pattern = row[0]
            if partial:
                matches = pattern == self.database
            else:
                literal = ''; escaped = False; wildcard = False
                for char in pattern:
                    if escaped: literal += char; escaped = False
                    elif char == '\\': escaped = True
                    elif char in ('%', '_'): wildcard = True
                    else: literal += char
                # Database wildcard grants do not union: a more specific
                # non-TRIGGER grant can shadow them. Only an exact grant proves
                # visibility without evaluating the complete grant precedence.
                matches = not escaped and not wildcard and literal == self.database
            if matches: return
        if self.query(TABLE_TRIGGER, (grantee, self.database, name)) == ((name,),):
            return
        raise CatalogError('metadata_visibility_unverified')

    def schema_state(self, names):
        if not isinstance(names, (tuple, list)) or len(names) > 1000:
            raise CatalogError('invalid_relations')
        try:
            for name in names: identifier(name)
        except CompileError:
            raise CatalogError('invalid_relations') from None
        if len(set(names)) != len(names): raise CatalogError('invalid_relations')
        result = []; total = 0
        for name in sorted(names):
            self._trigger_visibility(name)
            args = (self.database, name)
            table = self.query(TABLE, args, limit=2)
            if not table:
                result.append((name, None, (), (), ()))
                continue
            if len(table[0]) != 4 or table[0][0:2] != ('BASE TABLE', 'InnoDB'):
                raise CatalogError('unsafe_relation')
            columns = self.query(COLUMNS, args)
            indexes = self.query(INDEXES, args)
            if any(len(i) != 7 for i in indexes): raise CatalogError('unsafe_relation')
            visibility = self.query(INDEX_VISIBILITY.format(visibility='IS_VISIBLE' if self.engine == 'mysql' else 'IGNORED'), args)
            visible_value = 'YES' if self.engine == 'mysql' else 'NO'
            if len(indexes) != len(visibility) or any(len(v) != 2 or v[0] != i[0] or v[1] not in ('YES', 'NO') for i,v in zip(indexes,visibility)):
                raise CatalogError('unsafe_relation')
            indexes = tuple(i + (v[1] == visible_value,) for i,v in zip(indexes,visibility))
            constraints = self.query(CONSTRAINTS, args)
            if self.query(TRIGGERS, args) or self.query(REFERENCES, args + args):
                raise CatalogError('unsafe_relation')
            if (not columns or any(len(c) != 14 for c in columns)
                    or tuple(c[1] for c in columns) != tuple(range(1, len(columns) + 1))
                    or any(len(i) != 8 or i[3] is None for i in indexes)
                    or any(len(c) != 2 or c[1] not in ('PRIMARY KEY', 'UNIQUE') for c in constraints)):
                raise CatalogError('unsafe_relation')
            item = (name, table[0], columns, indexes, constraints)
            total += len(json.dumps(item, ensure_ascii=True).encode())
            if total > 4 * 1024 * 1024: raise CatalogError('result_limit')
            result.append(item)
        return tuple(result)

    def verify_transition(self, operation, before, column_semantics=(), framework='laravel'):
        """Verify one additive effect and preservation of the observed baseline.

        Returns the single freshly observed table snapshot for a durable journal.
        `before` must come from schema_state and later be authenticated by the
        execution proof. This detects drift; it does not serialize other writers.
        """
        if (type(before) is not tuple or len(before) != 1 or type(before[0]) is not tuple
                or len(before[0]) != 5 or type(operation) is not tuple or len(operation) < 4
                or before[0][0] != operation[2]):
            raise CatalogError('invalid_baseline')
        compile_operation(operation, database=self.database, engine=self.engine,
                          framework=framework, column_semantics=column_semantics)
        after = self.schema_state((operation[2],))
        self._verify_operation(operation, column_semantics, framework, after[0])
        old, new = before[0], after[0]
        action = operation[0]
        if action == 'create_table':
            unchanged = old == (operation[2], None, (), (), ())
        elif old[1] is None:
            unchanged = False
        elif action == 'add_column':
            name = operation[3][0]
            unchanged = (old[1] == new[1] and old[3:] == new[3:]
                         and all(c[0] != name for c in old[2])
                         and len(new[2]) == len(old[2]) + 1 and new[2][:-1] == old[2]
                         and new[2][-1][0] == name)
        else:  # create_index; compiler has already rejected other operations.
            name = operation[3]
            unchanged = (old[1:3] == new[1:3] and old[4:] == new[4:]
                         and all(i[0] != name for i in old[3])
                         and tuple(i for i in new[3] if i[0] != name) == old[3])
        if not unchanged:
            raise CatalogError('schema_changed')
        return after

    def verify_operation(self, operation, column_semantics=(), framework='laravel'):
        compile_operation(operation, database=self.database, engine=self.engine,
                          framework=framework, column_semantics=column_semantics)
        return self._verify_operation(operation, column_semantics, framework,
                                      self.schema_state((operation[2],))[0])

    def _verify_operation(self, operation, column_semantics, framework, state):
        compile_operation(operation, database=self.database, engine=self.engine,
                          framework=framework, column_semantics=column_semantics)
        observed = self.identity()
        _, table, columns, indexes, constraints = state
        if table is None or table[2] != observed['database_defaults'][1] or table[3] != '':
            raise CatalogError('postcondition_failed')
        action = operation[0]
        if action == 'create_index':
            selected = tuple(i for i in indexes if i[0] == operation[3])
            expected = tuple((operation[3], 1, n, name, 'A', None, 'BTREE', True) for n, name in enumerate(operation[4], 1))
            if selected != expected: raise CatalogError('postcondition_failed')
            return True
        definitions = operation[3] if action == 'create_table' else (operation[3],)
        parsed = tuple(_column(c, column_semantics[n] if framework == 'laravel' else None,
                               framework=framework, table=operation[2], additive=action == 'add_column')
                       for n, c in enumerate(definitions))
        if action == 'create_table' and tuple(c[0] for c in columns) != tuple(c[0] for c in parsed):
            raise CatalogError('postcondition_failed')
        primary = tuple(c[0] for c in parsed if c[3])
        for definition, expected in zip(definitions, parsed):
            name, sqltype, nullable, pk, auto, unsigned = expected
            if framework == 'alembic' and pk and len(primary) == 1 and definition[1][0] in ('integer', 'bigint', 'smallint'):
                auto = True
            found = tuple(c for c in columns if c[0] == name)
            if len(found) != 1: raise CatalogError('postcondition_failed')
            c = found[0]
            base = sqltype.split('(')[0].lower()
            # Display widths vary by server; TINYINT(1) is the boolean contract.
            pattern = re.escape(base) + (r'\(1\)' if base == 'tinyint' else r'(?:\([0-9]+\))?' if base in ('int', 'bigint', 'smallint') else re.escape(sqltype[len(base):]))
            pattern += ' unsigned' if unsigned else ''
            expected_default = (None, 'NULL') if self.engine == 'mariadb' and nullable else (None,)
            if (c[2] != base or not isinstance(c[3], str) or not re.fullmatch(pattern, c[3])
                    or c[4] != ('YES' if nullable else 'NO') or c[5] not in expected_default
                    or c[6] != ('auto_increment' if auto else '') or c[13] not in ('', None)):
                raise CatalogError('postcondition_failed')
            numeric = {'tinyint': (3, 0), 'smallint': (5, 0), 'int': (10, 0), 'bigint': (20 if unsigned else 19, 0)}
            if base in numeric and (c[8], c[9]) != numeric[base]: raise CatalogError('postcondition_failed')
            if base not in ('varchar', 'text') and (c[7] is not None or c[11] is not None or c[12] is not None):
                raise CatalogError('postcondition_failed')
            if base == 'varchar' and c[7] != definition[1][1]: raise CatalogError('postcondition_failed')
            if base in ('timestamp', 'datetime') and c[10] != 0: raise CatalogError('postcondition_failed')
            if base in ('varchar', 'text') and (c[11], c[12]) != observed['database_defaults']:
                raise CatalogError('postcondition_failed')
        if action == 'create_table':
            expected_indexes = tuple(('PRIMARY', 0, n, name, 'A', None, 'BTREE', True) for n, name in enumerate(primary, 1))
            expected_constraints = (('PRIMARY', 'PRIMARY KEY'),) if primary else ()
            if indexes != expected_indexes or constraints != expected_constraints:
                raise CatalogError('postcondition_failed')
        return True
