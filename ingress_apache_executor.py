"""Administrator-configured Apache capability; never accepts caller commands.

Linux pidfds pin graceful signals to the verified existing process. Setup must
provide the complete daemon argv and explicit absolute -f configuration path.
"""
import fnmatch
import ipaddress
import os
from pathlib import Path
import re
import selectors
import shlex
import signal
import socket
import stat
import subprocess
import time

LIMIT = 65536
_NAME = re.compile(r'[a-z0-9*?][a-z0-9.*?-]{0,252}\Z')
_HOST = re.compile(r'(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z')


def parse_inventory(output, alias_reader=None):
    """Parse complete httpd -S output, preserving aliases and effective paths."""
    rows, current, header = [], None, False
    for raw in output.splitlines():
        line = raw.strip()
        if not line: continue
        normalized = ' '.join(line.split())
        if normalized == 'VirtualHost configuration:': header = True; continue
        if normalized.startswith(('ServerRoot:', 'Main DocumentRoot:', 'Main ErrorLog:', 'Mutex ', 'PidFile:', 'Define:', 'User:', 'Group:')):
            continue
        if not header: raise ValueError('Unrecognized Apache inventory')
        if re.fullmatch(r'\S+:\d+\s+is\s+a\s+NameVirtualHost', line): current = None; continue
        match = re.fullmatch(r'(?:default\s+server\s+|port\s+\d+\s+namevhost\s+|\S+:\d+\s+)([^\s]+)\s+\((/[^\n]+):(\d+)\)', line)
        if match:
            name, path, source_line = match.groups()
            name = name.lower().rstrip('.')
            if not _NAME.fullmatch(name) or '..' in Path(path).parts: raise ValueError('Invalid Apache vhost identity')
            # Default server repeats the first namevhost; retain one row.
            existing = next((r for r in rows if r['path'] == path and r['names'][0] == name), None)
            current = existing or {'names':[name], 'path':path}
            if alias_reader is not None:
                for alias in alias_reader(path, int(source_line), name):
                    if alias not in current['names']: current['names'].append(alias)
            if existing is None: rows.append(current)
            continue
        match = re.fullmatch(r'(?:alias|wild\s+alias)\s+(\S+)', line)
        if match and current is not None:
            name = match.group(1).lower().rstrip('.')
            if not _NAME.fullmatch(name): raise ValueError('Invalid Apache alias')
            if name not in current['names']: current['names'].append(name)
            continue
        raise ValueError('Unrecognized Apache vhost output')
    if not header: raise ValueError('Missing Apache inventory header')
    return rows


def bounded_run(argv, timeout, *, env=None):
    """Drain merged output incrementally; kill on size/deadline, never shell."""
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, env=env, start_new_session=True)
    output = bytearray(); deadline = time.monotonic()+timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline-time.monotonic()
                if remaining <= 0: raise ValueError('Apache command deadline exceeded')
                events = selector.select(remaining)
                if not events: raise ValueError('Apache command deadline exceeded')
                chunk = os.read(process.stdout.fileno(), min(4096, LIMIT+1-len(output)))
                if not chunk: selector.unregister(process.stdout); break
                output.extend(chunk)
                if len(output) > LIMIT: raise ValueError('Apache output exceeds bound')
        code = process.wait(timeout=max(.001, deadline-time.monotonic()))
        if code: raise ValueError('Apache configuration command failed')
        return output.decode('utf-8', errors='strict')
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(); process.stdout.close()


class ApacheExecutor:
    def __init__(self, *, executable, config, pid_file, listener, expected_argv, owner_uid=0, timeout=5, source_roots=None, server_root=None):
        self.executable, self.config, self.pid_file = map(Path, (executable, config, pid_file))
        if type(owner_uid) is not int or owner_uid < 0: raise ValueError('Invalid administrator UID')
        if isinstance(timeout, bool) or not isinstance(timeout,(int,float)) or not 0 < timeout <= 60: raise ValueError('Invalid deadline')
        self.owner_uid, self.timeout = owner_uid, timeout
        if not isinstance(listener, (list,tuple)) or len(listener) != 2 or not ipaddress.ip_address(listener[0]).is_loopback or type(listener[1]) is not int or not 1 <= listener[1] <= 65535:
            raise ValueError('Observation listener must be loopback')
        self.listener = tuple(listener)
        self.expected_argv = tuple(expected_argv)
        if not self.expected_argv or self.expected_argv[0] != str(self.executable) or any(not isinstance(a,str) or '\x00' in a for a in self.expected_argv):
            raise ValueError('Invalid expected Apache argv')
        if self.expected_argv.count('-f') != 1 or self.expected_argv.index('-f')+1 >= len(self.expected_argv) or self.expected_argv[self.expected_argv.index('-f')+1] != str(self.config):
            raise ValueError('Daemon configuration must be explicit and exact')
        self.config_args = []
        arguments = iter(self.expected_argv[1:])
        for argument in arguments:
            if argument in ('-f', '-d', '-D'):
                value = next(arguments, None)
                if value is None: raise ValueError('Incomplete Apache argument')
                self.config_args.extend((argument, value))
            elif argument.startswith('-D') and len(argument) > 2:
                self.config_args.append(argument)
            else:
                raise ValueError('Unsupported daemon startup argument')
        self._safe(self.executable, executable=True); self._safe(self.config)
        self.executable_identity = self._file_identity(self.executable)
        # Administrative read allowlist, never supplied by a route request.
        self.source_roots = tuple(Path(root) for root in (source_roots or (self.config.parent,)))
        for root in self.source_roots:
            if not root.is_absolute() or '..' in root.parts or root.resolve() != root or root == Path('/'):
                raise ValueError('Invalid trusted source root')
        self.server_root = Path(server_root) if server_root is not None else None
        if self.server_root is None and '-d' in self.config_args:
            self.server_root = Path(self.config_args[self.config_args.index('-d')+1])
        if self.server_root is not None and (not self.server_root.is_absolute() or '..' in self.server_root.parts):
            raise ValueError('Invalid explicit ServerRoot')

    def _safe(self, path, *, executable=False):
        if not path.is_absolute() or '..' in path.parts: raise ValueError('Invalid administrator path')
        for parent in path.parents:
            s = parent.lstat()
            sticky_tmp = parent == Path('/tmp') and s.st_uid == 0 and stat.S_IMODE(s.st_mode) == 0o1777
            if not stat.S_ISDIR(s.st_mode) or s.st_uid not in (0,self.owner_uid) or (s.st_mode & 0o022 and not sticky_tmp): raise ValueError('Unsafe ancestor')
        s = path.lstat()
        if not stat.S_ISREG(s.st_mode) or s.st_uid not in ((0,self.owner_uid) if executable else (self.owner_uid,)) or s.st_mode & 0o022 or s.st_nlink != 1: raise ValueError('Unsafe administrator file')

    @staticmethod
    def _file_identity(path):
        s = path.stat(); return s.st_dev, s.st_ino

    def _command(self, flag):
        self._safe(self.executable, executable=True); self._safe(self.config)
        if self._file_identity(self.executable) != self.executable_identity: raise ValueError('Apache executable changed')
        return bounded_run([str(self.executable), *self.config_args, flag], self.timeout,
                           env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C','LC_ALL':'C'})

    def _source_path(self, path, roots, depth=0):
        if depth > 16 or not path.is_absolute() or '..' in path.parts or not any(path.is_relative_to(root) for root in roots):
            raise ValueError('Source escapes trusted configuration roots')
        # Validate every lexical component, including symlink owners and their
        # canonical targets. Writable parent directories cannot redirect reads.
        current = Path('/')
        for index, part in enumerate(path.parts[1:]):
            current /= part
            info = current.lstat()
            sticky = current == Path('/tmp') and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o1777
            if info.st_uid not in (0, self.owner_uid) or (not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022 and not sticky):
                raise ValueError('Untrusted configuration source')
            if stat.S_ISLNK(info.st_mode):
                target = Path(os.readlink(current))
                target = Path(os.path.normpath(str(target if target.is_absolute() else current.parent / target)))
                remainder = path.parts[index+2:]
                return self._source_path(target.joinpath(*remainder), roots, depth+1)
            if current != path and not stat.S_ISDIR(info.st_mode):
                raise ValueError('Invalid source ancestor')
        self._safe(path, executable=True)
        return path

    def _aliases(self, source, line_number, expected_name):
        # -S omits single-vhost aliases. Expand only explicitly trusted literal
        # includes, sharing byte/file/depth budgets over the entire source block.
        roots = getattr(self, 'source_roots', (Path(source).parent,))
        budget = {'bytes': 0, 'files': 0}
        def read(path):
            path = self._source_path(path, roots)
            budget['files'] += 1
            if budget['files'] > 64: raise ValueError('Include file count exceeds bound')
            with path.open('rb') as stream: raw = stream.read(LIMIT+1)
            budget['bytes'] += len(raw)
            if budget['bytes'] > LIMIT: raise ValueError('Vhost source exceeds bound')
            return path, raw.decode('utf-8').splitlines()
        path, lines = read(Path(source))
        if not 1 <= line_number <= len(lines) or not re.fullmatch(r'\s*<VirtualHost\s+[^>]+>\s*', lines[line_number-1], re.I):
            raise ValueError('Apache source location is not a literal VirtualHost')
        names = []
        def scan(lines, stack, top=False):
            if len(stack) > 16: raise ValueError('Include depth exceeds bound')
            for line in lines:
                stripped = line.strip()
                if not stripped or stripped.startswith('#'): continue
                if stripped.endswith('\\'): raise ValueError('Continued vhost source requires administrative resolution')
                if re.fullmatch(r'</VirtualHost>\s*', stripped, re.I):
                    if top: return
                    raise ValueError('Include cannot close source VirtualHost')
                words = shlex.split(stripped, comments=False)
                if not words: continue
                directive = words[0].lower()
                if directive in ('use','macro') or directive.startswith(('<if','<macro','<virtualhost')):
                    raise ValueError('Conditional vhost aliases cannot be proven')
                if directive in ('include','includeoptional'):
                    if len(words) != 2 or '${' in words[1]: raise ValueError('Invalid literal include')
                    target = Path(words[1])
                    if not target.is_absolute():
                        root = getattr(self, 'server_root', None)
                        if root is None: raise ValueError('Relative include requires explicit ServerRoot')
                        target = root / target
                    if '..' in target.parts or any(c in str(target.parent) for c in '*?['):
                        raise ValueError('Unsupported include traversal')
                    if not any(target.is_relative_to(root) for root in roots): raise ValueError('Include escapes trusted roots')
                    matches = []
                    if any(c in target.name for c in '*?['):
                        # Validate directory without reading its contents first.
                        self._source_path_directory(target.parent, roots)
                        with os.scandir(target.parent) as entries:
                            for count, entry in enumerate(entries):
                                if count >= 1024: raise ValueError('Include directory exceeds bound')
                                if fnmatch.fnmatchcase(entry.name, target.name) and not entry.name.startswith('.'):
                                    matches.append(Path(entry.path))
                    elif target.exists() or target.is_symlink(): matches = [target]
                    if not matches and directive == 'include': raise ValueError('Required include is missing')
                    for match in sorted(matches):
                        included, content = read(match)
                        if included in stack: raise ValueError('Include cycle')
                        scan(content, (*stack, included))
                elif directive in ('servername','serveralias'):
                    if len(words)<2 or (directive == 'servername' and len(words)!=2): raise ValueError('Invalid vhost names')
                    for name in words[1:]:
                        name = name.lower().rstrip('.')
                        if not _NAME.fullmatch(name): raise ValueError('Invalid literal vhost alias')
                        names.append(name)
            if top: raise ValueError('Unclosed VirtualHost')
        scan(lines[line_number:], (path,), top=True)
        if expected_name not in names: raise ValueError('Apache source identity differs from inventory')
        return names

    def _source_path_directory(self, path, roots):
        # No wildcard-directory traversal or symlink directories: ordinary enabled
        # site symlinks are supported at the leaf; directory authority stays fixed.
        if not any(path.is_relative_to(root) for root in roots): raise ValueError('Untrusted include directory')
        for current in (path, *path.parents):
            info = current.lstat()
            sticky = current == Path('/tmp') and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o1777
            if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0,self.owner_uid) or (info.st_mode & 0o022 and not sticky):
                raise ValueError('Unsafe include directory')

    def inventory(self): return parse_inventory(self._command('-S'), self._aliases)
    def configtest(self): self._command('-t')

    def _pid(self):
        self._safe(self.pid_file)
        with self.pid_file.open('rb') as f: raw = f.read(32)
        if not re.fullmatch(rb'[1-9][0-9]{0,9}\n?', raw): raise ValueError('Invalid Apache PID')
        pid = int(raw)
        if pid <= 1: raise ValueError('Invalid host daemon PID')
        return pid

    def _verify_pid(self, pid):
        proc = Path('/proc')/str(pid)
        if proc.stat().st_uid != self.owner_uid or self._file_identity(proc/'exe') != self.executable_identity:
            raise ValueError('Apache executable identity mismatch')
        with (proc/'cmdline').open('rb') as f: raw = f.read(LIMIT+1)
        if len(raw)>LIMIT or raw.rstrip(b'\x00').split(b'\x00') != [s.encode() for s in self.expected_argv]:
            raise ValueError('Apache configuration/argv identity mismatch')
        if self._pid() != pid: raise ValueError('Apache PID changed')

    def running(self):
        try:
            pid = self._pid(); self._verify_pid(pid); return True
        except (OSError, ValueError): return False

    def reload(self):
        # pidfd prevents a verified process exiting and its PID being reused.
        pid = self._pid(); fd = os.pidfd_open(pid)
        try:
            self._verify_pid(pid)
            signal.pidfd_send_signal(fd, signal.SIGUSR1)
        finally: os.close(fd)

    def _headers(self, hostname):
        if not isinstance(hostname,str) or not _HOST.fullmatch(hostname): raise ValueError('Invalid observation hostname')
        deadline = time.monotonic()+self.timeout
        with socket.create_connection(self.listener, timeout=self.timeout) as connection:
            connection.sendall(('GET / HTTP/1.1\r\nHost: '+hostname+'\r\nConnection: close\r\n\r\n').encode('ascii'))
            data = bytearray()
            while b'\r\n\r\n' not in data:
                remaining = deadline-time.monotonic()
                if remaining <= 0: raise ValueError('HTTP deadline exceeded')
                connection.settimeout(remaining)
                chunk = connection.recv(min(4096, LIMIT+1-len(data)))
                if not chunk: raise ValueError('Truncated HTTP headers')
                data.extend(chunk)
                if len(data)>LIMIT: raise ValueError('HTTP headers exceed bound')
            lines = bytes(data).split(b'\r\n\r\n',1)[0].decode('ascii').split('\r\n')
            if not re.fullmatch(r'HTTP/1\.[01] [2-5][0-9]{2}(?: [^\r\n]*)?',lines[0]): raise ValueError('Invalid HTTP status')
            values = []
            for line in lines[1:]:
                if ':' not in line or line.startswith((' ','\t')): raise ValueError('Invalid HTTP header')
                key,value = line.split(':',1)
                if key.lower() == 'x-deploy-monitor-route': values.append(value.strip())
            if len(values)>1: raise ValueError('Ambiguous route headers')
            return values[0] if values else None

    def observe(self, hostname, token):
        if not isinstance(token,str) or not re.fullmatch(r'[0-9a-f]{64}',token): raise ValueError('Invalid route token')
        try: return self._headers(hostname) == token
        except (OSError, ValueError, UnicodeError): return None

    def observe_absence(self, hostname):
        try:
            if self._headers(hostname) is not None: return False
            rows = self.inventory()
            if any(fnmatch.fnmatchcase(hostname,name) for row in rows for name in row['names']): return False
            return True
        except (OSError, ValueError, UnicodeError): return None
