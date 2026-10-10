import subprocess
import sys
import unittest
from unittest.mock import patch
import container_logs


class ContainerLogTests(unittest.TestCase):
    def test_output_is_bounded_and_collects_stderr(self):
        text, limited = container_logs.capture([sys.executable, '-c', 'import sys;sys.stderr.write("error\\n");sys.stderr.flush();print("x"*100000)'], limit=1024)
        self.assertTrue(limited)
        self.assertLessEqual(len(text.encode()), 1024)
        self.assertIn('error', text)

    def test_failed_command_does_not_return_its_output(self):
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            container_logs.capture([sys.executable, '-c', 'print("private-sentinel");raise SystemExit(1)'])

    def test_timeout_is_bounded(self):
        with self.assertRaisesRegex(ValueError, 'timed out'):
            container_logs.capture([sys.executable, '-c', 'import time;time.sleep(3)'], timeout=.05)

    def test_redacts_known_secrets_and_credentials(self):
        result = container_logs.redact('password=hunter2 token: Bearer abc123 postgresql://name:pass@db/x keyval \x1b[31mOK\x00', {'APP_KEY': 'keyval'})
        for secret in ('hunter2', 'abc123', 'name:pass', 'keyval', '\x1b', '\x00'):
            self.assertNotIn(secret, result)
        self.assertIn('OK', result)

    def test_identity_and_tail_checked_before_docker_logs(self):
        import control
        container = {'Id': 'a'*64, 'Config': {'Env': []}}
        with patch.object(control, 'inspect_target', return_value=(container, None, None, None)), patch.object(container_logs, 'capture') as capture:
            for body in ({'container_id':'b'*64}, {'container_id':'a'*64, 'tail':501}, {'container_id':'a'*64, 'tail':True}):
                with self.assertRaises(ValueError): container_logs.read(body)
            capture.assert_not_called()
            capture.return_value=('hello', False)
            result=container_logs.read({'container_id':'a'*64})
            self.assertEqual(result['text'],'hello')
            self.assertEqual(capture.call_args.args[0][-1], 'a'*64)

    def test_closed_output_process_still_obeys_timeout(self):
        with self.assertRaisesRegex(ValueError, 'timed out'):
            container_logs.capture([sys.executable, '-c', 'import os,time;os.close(1);os.close(2);time.sleep(3)'], timeout=.05)

    def test_truncated_line_does_not_expose_partial_known_secret(self):
        text, limited = container_logs.capture([sys.executable, '-c', 'print("safe\\n" + "sensitive-value" * 500)'], limit=10)
        self.assertTrue(limited)
        self.assertEqual(text, 'safe')

    def test_json_keys_and_ansi_split_known_secrets_are_redacted(self):
        text = container_logs.redact('"password":"json secret with spaces" abc\x1b[31mdef standalone-key',
                                     {'PASSWORD': 'abcdef', 'API_KEY': 'standalone-key'})
        for value in ('json secret with spaces', 'with spaces', 'abcdef', 'standalone-key'):
            self.assertNotIn(value, text)

    def test_final_response_remains_byte_bounded_after_redaction_expands(self):
        import control
        container = {'Id': 'a'*64, 'Config': {'Env': ['API_KEY=abcd']}}
        with patch.object(control, 'inspect_target', return_value=(container, None, None, None)), \
             patch.object(container_logs, 'capture', return_value=('abcd ' * 200, False)), \
             patch.object(container_logs, 'MAX_BYTES', 128):
            result = container_logs.read({'container_id': 'a'*64})
        self.assertTrue(result['truncated'])
        self.assertLessEqual(len(result['text'].encode()), 128)
        self.assertNotIn('abcd', result['text'])
