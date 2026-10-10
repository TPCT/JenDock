"""Host-only measurements and bounded history. No synthetic or container fallback."""
import json
import shutil
import time
from pathlib import Path

class HostMetrics:
    def __init__(self, root='/host', disk='/opt/docker-engine/projects', interface='eth0'):
        self.root=Path(root);self.disk=disk;self.interface=interface;self.previous=None
    def read(self, now=None):
        now=time.time() if now is None else now
        stat=(self.root/'proc-stat').read_text().splitlines()
        ticks=list(map(int,stat[0].split()[1:9]));total=sum(ticks);idle=ticks[3]+ticks[4]
        memory={}
        for line in (self.root/'proc-meminfo').read_text().splitlines():
            key,value=line.split(':',1)
            if key in ('MemTotal','MemAvailable','SwapTotal','SwapFree'):memory[key]=int(value.split()[0])*1024
        interface=next((line.split(':',1)[1].split() for line in (self.root/'proc-net-dev').read_text().splitlines() if ':' in line and line.split(':')[0].strip()==self.interface),None)
        if interface is None:raise ValueError('Configured host interface is unavailable')
        rx,tx=int(interface[0]),int(interface[8]);cpu=receive=transmit=None
        if self.previous:
            then,old_total,old_idle,old_rx,old_tx=self.previous;elapsed=now-then;delta=total-old_total
            if delta>0 and idle>=old_idle:cpu=max(0,min(100,100*(1-(idle-old_idle)/delta)))
            if elapsed>0:
                if rx>=old_rx:receive=(rx-old_rx)/elapsed
                if tx>=old_tx:transmit=(tx-old_tx)/elapsed
        self.previous=(now,total,idle,rx,tx)
        disk=shutil.disk_usage(self.disk)
        return dict(observed=now,cpu=cpu,memory_used=memory['MemTotal']-memory['MemAvailable'],memory_total=memory['MemTotal'],swap_used=memory['SwapTotal']-memory['SwapFree'],swap_total=memory['SwapTotal'],disk_used=disk.used,disk_total=disk.total,network_rx=receive,network_tx=transmit,interface=self.interface,load_1m=float((self.root/'proc-loadavg').read_text().split()[0]),cpu_count=sum(line.startswith('cpu') and line[3:4].isdigit() for line in stat),uptime=float((self.root/'proc-uptime').read_text().split()[0]))

def record(db, value):
    now=value['observed']
    previous=db.execute('SELECT MAX(observed) FROM telemetry').fetchone()[0]
    if previous is None or now-previous>=15:
        db.execute('INSERT OR REPLACE INTO telemetry VALUES(?,?)',(now,json.dumps(value)))
        db.execute('DELETE FROM telemetry WHERE observed<?',(now-86400,))

def history(db, now=None):
    now=time.time() if now is None else now
    result={}
    for name,seconds in [('15m',900),('1h',3600),('24h',86400)]:
        start=now-seconds
        rows=db.execute('SELECT value FROM telemetry WHERE observed IN (SELECT MAX(observed) FROM telemetry WHERE observed>=? GROUP BY CAST((observed-?)/? AS INTEGER)) ORDER BY observed',(start,start,seconds/120))
        result[name]=[json.loads(row[0]) for row in rows]
    return result
