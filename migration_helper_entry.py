"""Private JSONL migration helper; stdout contains protocol frames only."""
import json
import select
import sys
from migration_postgres import MigrationError
from migration_plan import PlanError
from migration_protocol import MAX_REQUEST,MAX_RESPONSE,dispatch,validate_request


def decode(raw):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('duplicate field')
            result[key]=value
        return result
    def invalid(_):raise ValueError('invalid constant')
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=invalid)


def main():
    def emit(value):
        encoded=json.dumps(value,allow_nan=False,separators=(',',':')).encode()
        if len(encoded)>MAX_RESPONSE:raise MigrationError('output_limit')
        sys.stdout.buffer.write(encoded+b'\n');sys.stdout.buffer.flush()
    result={'error':'invalid_request'}
    try:
        raw=sys.stdin.buffer.readline(MAX_REQUEST+1)
        if len(raw)>MAX_REQUEST or not raw.endswith(b'\n'):raise MigrationError('input_limit')
        request=validate_request(decode(raw));sequence=0
        def authorize():
            nonlocal sequence
            sequence+=1
            if sequence>10000:raise MigrationError('authority_limit')
            emit({'event':'authorize','sequence':sequence})
            if not select.select([sys.stdin.buffer],[],[],15)[0]:raise MigrationError('authority_unavailable')
            response=sys.stdin.buffer.readline(4097)
            if len(response)>4096 or not response.endswith(b'\n'):raise MigrationError('authority_unavailable')
            ack=decode(response)
            if ack!={'sequence':sequence,'authorized':True} or type(ack.get('sequence')) is not int:raise MigrationError('authority_denied')
        import psycopg
        def connect():
            return psycopg.connect(host='127.0.0.1',port=request['port'],dbname=request['database'],user=request['user'],password=request['password'],connect_timeout=3,sslmode='disable',autocommit=True,options='-c search_path=pg_catalog')
        result={'result':dispatch(request,connect=connect,authorize=authorize)}
    except MigrationError as error:result={'error':error.code}
    except PlanError:result={'error':'unsupported_source'}
    except Exception:result={'error':'helper_failed'}
    emit(result)


if __name__=='__main__':main()
