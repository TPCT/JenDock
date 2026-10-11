"""Local Apache route check against the existing loopback MCP preview; no tokens."""
import json
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


def command(args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT)


def main():
    root = Path(__file__).resolve().parents[1]
    base = command(['docker', 'run', '--rm', 'httpd:2.4-alpine', 'cat', '/usr/local/apache2/conf/httpd.conf'])
    base = base.replace('Listen 80', 'Listen 127.0.0.1:19193')
    for module in ('proxy_module', 'proxy_http_module', 'headers_module'):
        base = base.replace('#LoadModule ' + module, 'LoadModule ' + module)
    # Mirror the real vhost's inherited forwarding header, then include our route.
    base += ('\nServerName localhost\nPidFile /tmp/httpd.pid\nErrorLog /proc/self/fd/2\n'
             'RequestHeader set X-Forwarded-Proto "https"\n')
    base += (root / 'deployment/apache-mcp.conf').read_text()
    with tempfile.TemporaryDirectory(prefix='deploy-monitor-proxy-') as directory:
        config = Path(directory) / 'httpd.conf'
        config.write_text(base)
        config.chmod(0o644)
        args = ['docker', 'run', '--rm', '--read-only', '--user', '1000:1000', '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges:true', '--network', 'host', '--tmpfs', '/tmp',
                '--tmpfs', '/usr/local/apache2/logs:mode=1777', '-v', str(config) + ':/usr/local/apache2/conf/httpd.conf:ro']
        command(args + ['httpd:2.4-alpine', 'httpd', '-t'])
        identifier = command(args[:2] + ['-d', '--name', 'deploy-monitor-proxy-check', '--label',
            'com.docker.compose.project=deploy-monitor'] + args[2:] + ['httpd:2.4-alpine']).strip()

        def status(headers):
            request = urllib.request.Request('http://127.0.0.1:19193/_deploy/mcp', data=b'{}', headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=2) as response:
                    return response.status
            except urllib.error.HTTPError as error:
                return error.code

        try:
            for attempt in range(40):
                try:
                    initial = status({'Host': '127.0.0.1:19192'})
                    break
                except urllib.error.URLError:
                    if attempt == 39: raise
                    time.sleep(.1)
            checks = {'unauthenticated': initial,
                'forwarded_headers_stripped': status({'Host': '127.0.0.1:19192',
                    'X-Forwarded-For': 'untrusted', 'Forwarded': 'for=untrusted'}),
                'wrong_host': status({'Host': 'bad.example'}),
                'wrong_origin': status({'Host': '127.0.0.1:19192', 'Origin': 'https://bad.example'})}
            assert checks == {'unauthenticated': 401, 'forwarded_headers_stripped': 401,
                              'wrong_host': 421, 'wrong_origin': 403}, checks
            (root / '.runtime/mcp-proxy-last.json').write_text(json.dumps(checks, indent=2))
            print(json.dumps(checks))
        finally:
            command(['docker', 'rm', '-f', identifier])


if __name__ == '__main__':
    main()
