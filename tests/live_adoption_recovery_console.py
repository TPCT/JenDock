"""Browser acceptance harness: real auth/control/SQLite, synthetic Docker inventory.

Run locally, then sign in as fixture-owner with local-test-only. Each environment's
first successful save response is replaced with 503. This verifies ambiguous-response
UI recovery, not Docker or production acceptance. Ctrl-C removes temporary state.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import socketserver
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import adoption
import control
from server import Application,handler
from store import connect,snapshot
import test_adoption_environments as fixtures


def main():
    f=fixtures.AdoptionEnvironmentTests();f.setUp()
    root=f.path.parent
    config=root/'projects.json';config.write_text('{}')
    credentials=root/'credentials.json';salt='ab'*16
    credentials.write_text(json.dumps({'username':'fixture-owner','salt':salt,'password_hash':hashlib.pbkdf2_hmac('sha256',b'local-test-only',bytes.fromhex(salt),260000).hex()}));credentials.chmod(0o600)
    sock=str(root/'control.sock');calls=[];lost=set()
    def inventory(*args):
        calls.append({'action':'collect'})
        return dict(f.inventory,observed=time.time())
    with contextlib.ExitStack() as stack:
        stack.callback(f.doCleanups)
        for name,value in {'DATABASE':str(f.path),'CONFIG':str(config),'JOURNAL':root/'journal'}.items():stack.enter_context(patch.object(control,name,value))
        stack.enter_context(patch.object(adoption,'collect',side_effect=inventory))
        stack.enter_context(patch.object(control,'review_compose',None))
        unix=socketserver.ThreadingUnixStreamServer(sock,control.Handler);unix.daemon_threads=True
        thread=threading.Thread(target=unix.serve_forever,daemon=True);thread.start()
        stack.callback(lambda:(unix.shutdown(),unix.server_close(),thread.join()))
        app=Application(config,f.path,credentials);app.secure=False;app.cookie_name='adoption_fixture';app.control_socket=sock;app.web_root=ROOT/'frontend/dist'
        original=app.action
        def action(session,body):
            result=original(session,body)
            if body.get('action','').startswith('adoption-'):
                record={k:body[k] for k in ('action','project','environment','fingerprint','services','recipe_revision') if k in body}
                record['status']=result[0];calls.append(record)
                (root/'requests.json').write_text(json.dumps(calls,indent=2))
            env=body.get('environment')
            if body.get('action')=='adoption-save' and result[0]==200 and env not in lost:
                lost.add(env)
                return 503,{'error':'Fixture: accepted result deliberately withheld. Recover the original request.'}
            return result
        app.action=action
        server=ThreadingHTTPServer(('127.0.0.1',19203),handler(app));app.origin='http://127.0.0.1:19203'
        stack.callback(server.server_close)
        db=connect(f.path);snapshot(db,'worker',{'state':'running'});db.close()
        print(json.dumps({'url':app.origin,'state':str(root),'boundary':'synthetic inventory; real auth, control and SQLite'}),flush=True)
        try:server.serve_forever()
        except KeyboardInterrupt:pass

if __name__=='__main__':main()
