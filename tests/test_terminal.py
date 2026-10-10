"""Terminal identity, session ownership and bounded transport behavior."""
import base64
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
import terminal_control as terminal
import control

class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=patch.object(control,'DATABASE',str(Path(self.temp.name)/'state.sqlite'));self.db.start()
        self.body={'action':'terminal-open','project':'test','container':'test-app-1','container_id':'a'*64,'actor':'admin','terminal_owner':'b'*64,'request_id':'terminal-request-123456','cols':80,'rows':24}
        self.container={'Id':'a'*64,'State':{'Running':True}}
        terminal.SESSIONS.clear()
    def tearDown(self):
        terminal.SESSIONS.clear();self.db.stop();self.temp.cleanup()
    def test_no_session_identity_cannot_open(self):
        with self.assertRaises(control.ControlError):terminal.execute(dict(self.body,terminal_owner=''))
    def test_stopped_and_replaced_containers_rejected_before_shell(self):
        for c in ({'Id':'c'*64,'State':{'Running':True}},{'Id':'a'*64,'State':{'Running':False}}):
            with patch.object(control,'inspect_target',return_value=(c,None,None,None)),patch.object(terminal,'TerminalSession') as shell:
                with self.assertRaises(control.ControlError):terminal.execute(self.body)
                shell.assert_not_called()
    def test_session_open_idempotency_and_owner_isolation(self):
        class Fake:
            closed=False
            def __init__(self,body,c,docker):
                self.owner=body['terminal_owner'];self.project=body['project'];self.name=body['container'];self.request_id=body['request_id'];self.container_id=c['Id']
            def close(self):self.closed=True
        with patch.object(control,'inspect_target',return_value=(self.container,None,None,None)),patch.object(terminal,'TerminalSession',Fake):
            first=terminal.execute(self.body);second=terminal.execute(self.body)
            self.assertEqual(first,second);self.assertEqual(len(terminal.SESSIONS),1)
            poll=dict(self.body,action='terminal-close',terminal_id=first['terminal_id'])
            with self.assertRaises(control.ControlError):terminal.execute(dict(poll,terminal_owner='c'*64))
            self.assertTrue(terminal.execute(poll)['closed'])
    def test_sequence_deduplicates_input_and_output_cursor_replays(self):
        import threading,time
        session=object.__new__(terminal.TerminalSession)
        session.lock=threading.RLock();session.closed=False;session.sequence=0;session.offset=0;session.buffer=bytearray(b'synthetic output');session.fd=10
        session.resize=lambda cols,rows:None
        with patch('terminal_control.os.write',return_value=3) as write:
            a=session.exchange({'input':'pwd','sequence':1,'cursor':0})
            b=session.exchange({'input':'pwd','sequence':1,'cursor':0})
            self.assertEqual(a,b);write.assert_called_once_with(10,b'pwd')
            self.assertEqual(base64.b64decode(a['output']),b'synthetic output')
            with self.assertRaises(ValueError):session.exchange({'input':'pwd','sequence':3,'cursor':0})
            self.assertEqual(session.exchange({'cursor':a['cursor']})['output'],'')
