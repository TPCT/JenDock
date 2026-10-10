"""Bounded, owner-authenticated Docker log snapshots; never persisted in audit."""
import os
import re
import selectors
import subprocess
import time

MAX_BYTES = 131072
SECRET_KEY = re.compile(r'password|passwd|secret|token|credential|private.?key|app_key|api[_-]?key|access[_-]?key|authorization|database_url|db_url', re.I)


def redact(text, environment):
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f]', '', text)
    for key, value in sorted(environment.items(), key=lambda item: len(item[1]) if isinstance(item[1], str) else 0, reverse=True):
        if SECRET_KEY.search(key) and isinstance(value, str) and len(value) >= 4:
            text = text.replace(value, '[redacted]')
    for quote in ('"', "'"):
        quoted = (r"(?i)(\b(?:password|passwd|secret|token|authorization|api[_-]?key)[\"']?\s*[:=]\s*"
                  + quote + r")(?:(?:\\.)|[^" + quote + r"\\])*" + quote)
        text = re.sub(quoted, lambda match: match.group(1) + '[redacted]' + quote, text)
    text = re.sub(r'''(?i)(\b(?:password|passwd|secret|token|authorization|api[_-]?key)["']?\s*[:=]\s*["']?)(?:Bearer\s+)?[^\s,;"']+''', r'\1[redacted]', text)
    text = re.sub(r'([a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/@]+:[^\s/@]+@', r'\1[redacted]@', text)
    return text


def capture(args, timeout=10, limit=MAX_BYTES):
    try:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError:
        raise ValueError('Docker logs are unavailable. Retry loading logs.') from None
    chunks, size, truncated = [], 0, False
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError('Log retrieval timed out. Retry loading logs.')
                for key, _ in selector.select(min(remaining, .5)):
                    part = os.read(key.fileobj.fileno(), min(16384, limit + 1 - size))
                    if not part:
                        selector.unregister(key.fileobj)
                        continue
                    chunks.append(part); size += len(part)
                    if size > limit:
                        truncated = True
                        process.kill()
                        selector.unregister(key.fileobj)
            if truncated:
                process.wait(timeout=2)
            elif process.wait(timeout=max(.1, deadline-time.monotonic())):
                raise ValueError('Docker logs are unavailable for this container or logging driver.')
        raw = b''.join(chunks)[:limit]
        if truncated:
            # Never return a cut secret prefix from a partially captured line.
            raw = raw.rpartition(b'\n')[0]
        return raw.decode('utf-8', errors='replace'), truncated
    except subprocess.TimeoutExpired:
        raise ValueError('Log retrieval timed out. Retry loading logs.') from None
    finally:
        if process.poll() is None:
            process.kill(); process.wait()
        process.stdout.close()


def read(body):
    import control
    container, _, _, _ = control.inspect_target(body.get('project'), body.get('container'))
    if container['Id'] != body.get('container_id'):
        raise ValueError('The container was replaced. Reload its details before reading logs.')
    tail = body.get('tail', 200)
    if type(tail) is not int or not 1 <= tail <= 500:
        raise ValueError('Choose between 1 and 500 log lines.')
    text, truncated = capture([control.DOCKER, 'logs', '--timestamps', '--tail', str(tail), container['Id']])
    encoded = redact(text, control.effective_environment(container)).encode('utf-8')
    truncated = truncated or len(encoded) > MAX_BYTES
    return {'container_id': container['Id'], 'observed': time.time(),
            'text': encoded[:MAX_BYTES].decode('utf-8', errors='ignore'), 'truncated': truncated,
            'tail': tail}
