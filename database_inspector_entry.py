"""Private one-request PostgreSQL helper. Never log requests or driver errors."""
import json
import sys
from database_postgres import Inspector,DatabaseError

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
        if not isinstance(value,dict) or set(value)!=fields:raise ValueError()
        if type(value['port']) is not int or not 1<=value['port']<=65535:raise ValueError()
        if any(not isinstance(value[k],str) or not 1<=len(value[k])<=4096 or '\x00' in value[k] for k in ('database','user','password')):raise ValueError()
        import psycopg
        def connect():
            return psycopg.connect(host='127.0.0.1',port=value['port'],dbname=value['database'],user=value['user'],password=value['password'],connect_timeout=3,sslmode='disable',options='-c search_path=pg_catalog')
        adapter=Inspector(connect,database=value['database'],user=value['user'],schemas=value['schemas'],row_columns=value['row_columns'],cursor_key=value['cursor_key'].encode(),cursor_context=value['cursor_context'])
        result={'result':adapter.inspect(value['request'])}
    except DatabaseError as error:result={'error':error.code}
    except Exception:pass
    encoded=json.dumps(result,allow_nan=False).encode()
    if len(encoded)>240000:encoded=b'{"error":"result_limit"}'
    sys.stdout.buffer.write(encoded)

if __name__=='__main__':main()
