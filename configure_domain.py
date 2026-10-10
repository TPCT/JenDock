"""Prepare and validate only monitoring.shiftcodes.net, using trusted containers."""
import subprocess
SSH=['ssh','-S','/tmp/codex-docker-retry-%C','-p','22','-o','BatchMode=yes','-o','ConnectTimeout=10','-o','StrictHostKeyChecking=yes','root@176.118.198.49']
D='/opt/docker-engine/29.8.2/docker/docker'
B='/root/incident-containment-20261009-cron/busybox'
ROOT='/opt/docker-engine/projects/deploy-monitor'
http='''<VirtualHost 176.118.198.49:80>
    ServerName monitoring.shiftcodes.net
    Include /opt/docker-engine/projects/certificates/acme-vhost.conf
    RewriteEngine On
    RewriteCond %{REQUEST_URI} !^/.well-known/acme-challenge/
    RewriteRule ^ https://monitoring.shiftcodes.net%{REQUEST_URI} [R=301,L,NE]
</VirtualHost>
'''
https='''<VirtualHost 176.118.198.49:443>
    ServerName monitoring.shiftcodes.net
    Include /opt/docker-engine/projects/certificates/acme-vhost.conf
    SSLEngine on
    SSLCertificateFile /etc/letsencrypt/live/monitoring.shiftcodes.net/fullchain.pem
    SSLCertificateKeyFile /etc/letsencrypt/live/monitoring.shiftcodes.net/privkey.pem
    Include /etc/letsencrypt/options-ssl-apache.conf
    ProxyRequests Off
    ProxyPreserveHost On
    RequestHeader set X-Forwarded-Proto "https"
    RequestHeader set X-Real-IP "expr=%{REMOTE_ADDR}"
    ProxyPass /.well-known/acme-challenge/ !
    ProxyPass /api/action http://127.0.0.1:19190/api/action timeout=600
    ProxyPass / http://127.0.0.1:19190/ timeout=30
    ProxyPassReverse / http://127.0.0.1:19190/
    Header always set Strict-Transport-Security "max-age=31536000"
    Header always set X-Content-Type-Options "nosniff"
    ErrorLog ${APACHE_LOG_DIR}/deploy-monitor-error.log
    CustomLog ${APACHE_LOG_DIR}/deploy-monitor-access.log combined
</VirtualHost>
'''
def run(command,script=None):
    return subprocess.run(SSH+['export DOCKER_HOST=unix:///run/docker-isolation.sock; '+command],input=script.encode() if script else None,check=True)
def prepare(filename,text):
    script='from pathlib import Path\np=Path('+repr(ROOT+'/'+filename)+');p.write_text('+repr(text)+');p.chmod(0o644)\n'
    run(D+' exec -i deploy-monitor-worker-1 python -',script)
def validate(filename):
    script='''from pathlib import Path
root=Path('/opt/docker-engine/projects/deploy-monitor')
s=(root/'apache-validation.conf').read_text().replace('Include /config/apache-vhost.candidate.conf','Include /config/'''+filename+'''')
(root/'monitoring-validation.conf').write_text(s)
'''
    run(D+' exec -i deploy-monitor-worker-1 python -',script)
    run(D+' run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp -v '+ROOT+':/config:ro -v /etc/letsencrypt:/etc/letsencrypt:ro -v /opt/docker-engine/projects/certificates:/opt/docker-engine/projects/certificates:ro httpd:2.4-alpine httpd -t -f /config/monitoring-validation.conf')
def install(filename):
    run(B+' cp '+ROOT+'/'+filename+' /etc/apache2/sites-available/'+filename)
    run(B+' ln -sf /etc/apache2/sites-available/'+filename+' /etc/apache2/sites-enabled/'+filename)
    run('pid=$('+B+' cat /run/apache2/apache2.pid); [ "$('+B+' readlink /proc/$pid/exe)" = /usr/sbin/apache2 ] && '+B+' kill -USR1 "$pid"')
if __name__=='__main__':
    import sys
    phase=sys.argv[1]
    filename='monitoring.shiftcodes.net'+('-le-ssl' if phase=='https' else '')+'.conf'
    prepare(filename,https if phase=='https' else http)
    validate(filename)
    install(filename)
