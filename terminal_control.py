"""Short-lived, session-owned Docker exec terminals; output is memory-only."""
import base64
import fcntl
import os
import pty
import re
import secrets
import select
import signal
import struct
import subprocess
import termios
import threading
import time

SESSIONS = {}
GUARD = threading.RLock()
LIMIT = 262144

class TerminalSession:
    def __init__(self, body, container, docker):
        self.owner, self.project, self.name = body['terminal_owner'], body['project'], body['container']
        self.container_id = container['Id']
        self.request_id = body['request_id']
        self.actor = body['actor']
        self.lock = threading.RLock()
        self.buffer = bytearray()
        self.offset = self.sequence = 0
        self.created = self.touched = time.monotonic()
        self.closed = False
        cols,rows=body.get('cols',80),body.get('rows',24)
        if type(cols) is not int or type(rows) is not int or not 10<=cols<=300 or not 4<=rows<=100:
            raise ValueError('Invalid terminal size.')
        master, slave = pty.openpty()
        self.fd = master
        self.resize(body.get('cols', 80), body.get('rows', 24))
        try:
            self.process = subprocess.Popen([docker, 'exec', '-it', '-e', 'TERM=xterm-256color', self.container_id, '/bin/sh'], stdin=slave, stdout=slave, stderr=slave, start_new_session=True, close_fds=True)
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        threading.Thread(target=self.read, daemon=True).start()

    def resize(self, cols, rows):
        if type(cols) is not int or type(rows) is not int or not 10 <= cols <= 300 or not 4 <= rows <= 100:
            raise ValueError('Invalid terminal size.')
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
        if hasattr(self, 'process') and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGWINCH)

    def read(self):
        try:
            while not self.closed:
                if time.monotonic() - self.touched > 120 or time.monotonic() - self.created > 1800:
                    break
                if not select.select([self.fd], [], [], 1)[0]:
                    if self.process.poll() is not None: break
                    continue
                with self.lock:
                    if self.closed: break
                    chunk = os.read(self.fd, 16384)
                    if not chunk: break
                    self.buffer.extend(chunk)
                    excess = max(0, len(self.buffer) - LIMIT)
                    if excess:
                        del self.buffer[:excess]
                        self.offset += excess
        except OSError:
            pass
        finally:
            self.close()

    def close(self):
        with self.lock:
            if self.closed: return
            self.closed = True
            # SIGHUP on the PTY ends the attached interactive exec. No container stop.
            try: os.close(self.fd)
            except OSError: pass
            if self.process.poll() is None:
                try:
                    os.killpg(self.process.pid, signal.SIGHUP)
                    self.process.wait(timeout=2)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    if self.process.poll() is None: self.process.kill()

    def exchange(self, body):
        with self.lock:
            self.touched = time.monotonic()
            cursor = body.get('cursor', 0)
            if type(cursor) is not int or cursor < 0 or cursor > self.offset + len(self.buffer):
                raise ValueError('Invalid terminal cursor.')
            if not self.closed:
                self.resize(body.get('cols', 80), body.get('rows', 24))
                text = body.get('input', '')
                sequence = body.get('sequence', self.sequence)
                if not isinstance(text, str) or len(text.encode()) > 8192 or type(sequence) is not int:
                    raise ValueError('Invalid terminal input.')
                if text and sequence > self.sequence:
                    if sequence != self.sequence + 1: raise ValueError('Terminal input is out of order. Reconnect before continuing.')
                    # Input is acknowledged only after it is written; never log it.
                    payload = text.encode()
                    while payload:
                        payload = payload[os.write(self.fd, payload):]
                    self.sequence = sequence
            truncated = cursor < self.offset
            chunk = bytes(self.buffer[max(0, cursor-self.offset):])
            return {'output': base64.b64encode(chunk).decode(), 'cursor': self.offset+len(self.buffer), 'sequence': self.sequence, 'closed': self.closed, 'truncated': truncated}

def execute(body):
    from control import ControlError, inspect_target, DOCKER, DATABASE
    from store import connect, audit
    owner = body.get('terminal_owner', '')
    if not re.fullmatch('[a-f0-9]{64}', owner): raise ControlError('Sign in again to open a terminal.')
    with GUARD:
        for key, session in list(SESSIONS.items()):
            if session.closed and (time.monotonic()-session.touched > 120 or len(SESSIONS)>16):
                del SESSIONS[key]
        action = body['action']
        if action == 'terminal-open':
            if not re.fullmatch('[a-zA-Z0-9-]{16,80}', body.get('request_id', '')): raise ControlError('Invalid terminal request.')
            for key, session in SESSIONS.items():
                if session.owner == owner and session.request_id == body['request_id']:
                    if (session.project, session.name) != (body['project'], body['container']): raise ControlError('Terminal request belongs to another container.')
                    return {'terminal_id': key, 'container_id': session.container_id, 'closed': session.closed}
            c, _, _, _ = inspect_target(body['project'], body['container'])
            if c['Id'] != body.get('container_id'): raise ControlError('Container was replaced. Reload its details before connecting.')
            if not c['State']['Running']: raise ControlError('Start this container before opening its terminal.')
            if sum(not s.closed for s in SESSIONS.values()) >= 4: raise ControlError('Four terminals are already open. Close a session before connecting.')
            session = TerminalSession(body, c, DOCKER)
            key = secrets.token_urlsafe(32)
            SESSIONS[key] = session
            db = connect(DATABASE)
            try: audit(db, body['actor'], 'terminal-open', body['project'], body['container'])
            finally: db.close()
            return {'terminal_id': key, 'container_id': c['Id'], 'closed': False}
        session = SESSIONS.get(body.get('terminal_id'))
        if not session or (session.owner, session.project, session.name) != (owner, body.get('project'), body.get('container')):
            raise ControlError('Terminal expired or belongs to another session. Connect again.')
        if action == 'terminal-close':
            session.close()
            return {'closed': True}
        if action != 'terminal-poll': raise ControlError('Unknown terminal operation.')
        try: return session.exchange(body)
        except (OSError, ValueError) as error:
            session.close()
            raise ControlError(str(error) if isinstance(error, ValueError) else 'Terminal disconnected. Connect again.')
