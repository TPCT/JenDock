"""Private one-request database helper. Never log requests or driver errors."""
import json
import sys
import database_postgres
import database_mysql
import application_database_identity

def main():
    result={'error':'invalid_request'}
    try:
        raw=sys.stdin.buffer.read(32769)
        if len(raw)>32768:raise ValueError()
        def pairs(items):
            value={}
            for key,item in items:
                if key in value:raise ValueError()
                value[key]=item
            return value
        def constant(_):raise ValueError()
        value=json.loads(raw,object_pairs_hook=pairs,parse_constant=constant)
        fields={'database','user','password','port','schemas','row_columns','cursor_key','cursor_context','request'}
        application_identity = isinstance(value,dict) and value.get('action') == 'application-identity'
        if application_identity:
            application_database_identity.validate(value)
        elif not isinstance(value,dict) or set(value) not in (fields,fields|{'engine'}):raise ValueError()
        engine=value.get('engine','postgresql')
        if engine not in ('postgresql','mysql','mariadb'):raise ValueError()
        if type(value['port']) is not int or not 1<=value['port']<=65535:raise ValueError()
        if any(not isinstance(value[k],str) or not 1<=len(value[k])<=4096 or '\x00' in value[k] for k in ('database','user','password')):raise ValueError()
        options={}
        if engine=='postgresql':
            import psycopg
            def connect():
                return psycopg.connect(host='127.0.0.1',port=value['port'],dbname=value['database'],user=value['user'],password=value['password'],connect_timeout=3,sslmode='disable',options='-c search_path=pg_catalog')
            adapter_type=database_postgres.Inspector
        else:
            import pymysql
            def connect():
                return pymysql.connect(host='127.0.0.1',port=value['port'],database=value['database'],user=value['user'],password=value['password'],connect_timeout=3,read_timeout=4,write_timeout=3,charset='utf8mb4',autocommit=True,local_infile=False,client_flag=0)
            adapter_type=database_mysql.Inspector
            options['expected_engine']=engine
        if application_identity:
            result={'result':application_database_identity.inspect(value,connect)}
        else:
            adapter=adapter_type(connect,database=value['database'],user=value['user'],schemas=value['schemas'],row_columns=value['row_columns'],cursor_key=value['cursor_key'].encode(),cursor_context=value['cursor_context'],**options)
            result={'result':adapter.inspect(value['request'])}
    except (database_postgres.DatabaseError,database_mysql.DatabaseError) as error:result={'error':error.code}
    except Exception:pass
    encoded=json.dumps(result,allow_nan=False).encode()
    if len(encoded)>240000:encoded=b'{"error":"result_limit"}'
    sys.stdout.buffer.write(encoded)

if __name__=='__main__':main()
