"""Unintegrated MySQL/MariaDB typed additive DDL compiler; no execution.

Only consumed parser tuples are accepted. A future executor must pin server
dialect/version, InnoDB, database charset/collation and sql_mode, and require
explicit_defaults_for_timestamp=ON before using this grammar. This module does
not prove existing schema compatibility, migration ledger state or DDL rollback.
MySQL-family DDL has implicit commits; PostgreSQL transaction guarantees do not
apply. Alembic's current implicit/explicit `public` distinction is lost by its
parser, so that namespace is rejected rather than silently remapped.
"""
import re


class CompileError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__('MySQL migration compilation failed: ' + code + '.')


RESERVED = frozenset({'migrations', 'alembic_version', 'deploy_monitor_migration_hashes',
                     'deploy_monitor_laravel_hashes', 'deploy_monitor_migration_operations'})
SYSTEM_DATABASES = frozenset({'mysql', 'information_schema', 'performance_schema', 'sys'})
TYPES = {'integer':'INT', 'bigint':'BIGINT', 'smallint':'SMALLINT',
         'boolean':'TINYINT(1)', 'text':'TEXT', 'date':'DATE', 'timestamp':'DATETIME'}


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', value):
        raise CompileError('invalid_identifier')
    return '`' + value + '`'


def _target(operation, database, engine, framework):
    identifier(database)
    if database.lower() in SYSTEM_DATABASES or engine not in ('mysql', 'mariadb'):
        raise CompileError('unsupported_target')
    if type(operation) is not tuple or len(operation) < 4:
        raise CompileError('invalid_operation')
    _, schema, table = operation[:3]
    identifier(table)
    if table.lower() in RESERVED:
        raise CompileError('reserved_relation')
    if framework == 'laravel':
        if schema != 'public':
            raise CompileError('unsupported_namespace')
    elif framework == 'alembic':
        if schema == 'public':
            raise CompileError('ambiguous_alembic_namespace')
        if schema != database:
            raise CompileError('foreign_namespace')
    else:
        raise CompileError('unsupported_framework')
    return identifier(database) + '.' + identifier(table)


def _column(column, semantic, *, framework, table, additive):
    if type(column) is not tuple or len(column) != 4:
        raise CompileError('invalid_column')
    name, kind, nullable, primary = column
    identifier(name)
    if (type(nullable) is not bool or type(primary) is not bool or primary and nullable
            or additive and (not nullable or primary) or type(kind) is not tuple or not kind
            or not isinstance(kind[0], str)):
        raise CompileError('invalid_column')
    if kind[0] == 'varchar':
        if len(kind) != 2 or type(kind[1]) is not int or not 1 <= kind[1] <= 16383:
            raise CompileError('unsupported_string_length')
        sqltype = 'VARCHAR(' + str(kind[1]) + ')'
    elif len(kind) == 1 and kind[0] in TYPES:
        sqltype = TYPES[kind[0]]
    else:
        raise CompileError('unsupported_type')
    auto = unsigned = False
    if framework == 'laravel':
        if type(semantic) is not tuple or len(semantic) != 7:
            raise CompileError('missing_column_semantics')
        ordinal, schema, relation, field, source_type, auto, unsigned = semantic
        if (type(ordinal) is not int or ordinal < 0 or (schema, relation, field) != ('public', table, name)
                or type(auto) is not bool or type(unsigned) is not bool or not isinstance(source_type, str)):
            raise CompileError('invalid_column_semantics')
        expected = {'id':('bigint',), 'increments':('integer',), 'bigIncrements':('bigint',),
                    'integer':('integer',), 'bigInteger':('bigint',), 'smallInteger':('smallint',),
                    'boolean':('boolean',), 'text':('text',), 'date':('date',),
                    'dateTime':('timestamp',), 'timestamp':('timestamp',)}
        if source_type == 'string':
            if kind[0] != 'varchar':raise CompileError('invalid_column_semantics')
        elif expected.get(source_type) != kind:
            raise CompileError('invalid_column_semantics')
        identity = source_type in ('id', 'increments', 'bigIncrements')
        if auto != identity or unsigned != identity or identity and not primary:
            raise CompileError('invalid_identity_semantics')
        if source_type == 'dateTime':sqltype = 'DATETIME'
        if source_type == 'timestamp':sqltype = 'TIMESTAMP'
    elif semantic is not None:
        raise CompileError('unexpected_column_semantics')
    return name, sqltype, nullable, primary, auto, unsigned


def compile_operation(operation, *, database, engine, framework, column_semantics=()):
    target = _target(operation, database, engine, framework)
    action, _, table = operation[:3]
    if type(column_semantics) is not tuple:
        raise CompileError('invalid_column_semantics')
    if action == 'create_index' and len(operation) == 5:
        index, columns = operation[3:]
        identifier(index)
        if (column_semantics or type(columns) is not tuple or not 1 <= len(columns) <= 16
                or any(not isinstance(c,str) for c in columns) or len({c.lower() for c in columns}) != len(columns)):
            raise CompileError('invalid_index')
        return 'CREATE INDEX ' + identifier(index) + ' ON ' + target + ' (' + ','.join(identifier(c) for c in columns) + ')'
    if action not in ('create_table', 'add_column') or len(operation) != 4:
        raise CompileError('unsupported_operation')
    columns = operation[3] if action == 'create_table' else (operation[3],)
    if type(columns) is not tuple or not 1 <= len(columns) <= 256:
        raise CompileError('invalid_columns')
    if framework == 'laravel' and len(column_semantics) != len(columns):
        raise CompileError('missing_column_semantics')
    if framework != 'laravel' and column_semantics:
        raise CompileError('unexpected_column_semantics')
    parsed = [_column(c, column_semantics[i] if framework == 'laravel' else None,
                     framework=framework, table=table, additive=action == 'add_column') for i,c in enumerate(columns)]
    if len({c[0].lower() for c in parsed}) != len(parsed):raise CompileError('duplicate_column')
    primary = [c[0] for c in parsed if c[3]]
    if sum(c[4] for c in parsed) > 1:raise CompileError('invalid_identity_semantics')
    parts = []
    for i,(name, sqltype, nullable, pk, auto, unsigned) in enumerate(parsed):
        if framework == 'alembic' and pk and len(primary) == 1 and columns[i][1][0] in ('integer', 'bigint', 'smallint'):
            auto = True
        if auto and (len(primary) != 1 or name not in primary):raise CompileError('invalid_identity_semantics')
        parts.append(identifier(name) + ' ' + sqltype + (' UNSIGNED' if unsigned else '')
                     + (' NULL' if nullable else ' NOT NULL') + (' AUTO_INCREMENT' if auto else ''))
    if action == 'add_column':return 'ALTER TABLE ' + target + ' ADD COLUMN ' + parts[0]
    if primary:parts.append('PRIMARY KEY (' + ','.join(identifier(c) for c in primary) + ')')
    return 'CREATE TABLE ' + target + ' (' + ','.join(parts) + ') ENGINE=InnoDB'


def compile_laravel_record(record, *, database, engine):
    """Consume a current LaravelPlan migration record, preserving its semantics.

    Ordinal gaps are valid: empty and index-only closures have no column
    semantics. Exact source ordinals require rebuilding the source parser plan;
    this compiler checks only the grouping/order observable in this record.
    """
    if type(record) is not tuple or len(record) != 8 or type(record[6]) is not tuple or type(record[7]) is not tuple:
        raise CompileError('invalid_record')
    offset = 0; result = []; previous_ordinal = None
    for operation in record[6]:
        if type(operation) is not tuple or len(operation) < 4:raise CompileError('invalid_operation')
        count = len(operation[3]) if operation[0] == 'create_table' and type(operation[3]) is tuple else 1 if operation[0] == 'add_column' else 0
        semantics = record[7][offset:offset + count]
        offset += count
        result.append(compile_operation(operation, database=database, engine=engine, framework='laravel', column_semantics=semantics))
        # compile_operation validates each semantic before these comparisons.
        if semantics:
            ordinals = tuple(semantic[0] for semantic in semantics)
            if operation[0] == 'create_table' and len(set(ordinals)) != 1:
                raise CompileError('invalid_semantic_order')
            for ordinal in ordinals:
                if previous_ordinal is not None and ordinal < previous_ordinal:
                    raise CompileError('invalid_semantic_order')
                previous_ordinal = ordinal
    if offset != len(record[7]):raise CompileError('extra_column_semantics')
    return tuple(result)
