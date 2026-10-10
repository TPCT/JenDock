import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from collections import namedtuple
from telemetry import HostMetrics,record,history
from store import connect

class TelemetryTests(unittest.TestCase):
    def test_host_deltas_first_sample_and_counter_reset(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            (p/'proc-stat').write_text('cpu 100 0 100 800 0 0 0 0\ncpu0 0\ncpu1 0\n')
            (p/'proc-meminfo').write_text('MemTotal: 1000 kB\nMemAvailable: 400 kB\nSwapTotal: 100 kB\nSwapFree: 75 kB\n')
            (p/'proc-net-dev').write_text('eth0: 1000 0 0 0 0 0 0 0 2000 0 0 0 0 0 0 0\n')
            (p/'proc-loadavg').write_text('1.2 1 1 1/1 1');(p/'proc-uptime').write_text('86401 1')
            m=HostMetrics(d,d)
            a=m.read(100);self.assertIsNone(a['cpu']);self.assertIsNone(a['network_rx']);self.assertEqual(a['memory_used'],600*1024);self.assertEqual(a['cpu_count'],2)
            (p/'proc-stat').write_text('cpu 150 0 100 850 0 0 0 0\ncpu0 0\ncpu1 0\n')
            (p/'proc-net-dev').write_text('eth0: 1200 0 0 0 0 0 0 0 2400 0 0 0 0 0 0 0\n')
            b=m.read(110);self.assertEqual(b['cpu'],50);self.assertEqual(b['network_rx'],20);self.assertEqual(b['network_tx'],40)
            (p/'proc-net-dev').write_text('eth0: 1 0 0 0 0 0 0 0 1 0 0 0 0 0 0 0\n')
            self.assertIsNone(m.read(120)['network_rx'])
    def test_missing_host_mount_does_not_substitute_container_metrics(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(FileNotFoundError):HostMetrics(d,d).read()
    def test_bounded_history_retention_and_sampling(self):
        with tempfile.TemporaryDirectory() as d:
            db=connect(Path(d)/'db')
            for t in range(0,90001,15):record(db,{'observed':t,'cpu':t%100})
            record(db,{'observed':90001,'cpu':1})
            self.assertEqual(db.execute('SELECT MAX(observed) FROM telemetry').fetchone()[0],90000)
            self.assertGreaterEqual(db.execute('SELECT MIN(observed) FROM telemetry').fetchone()[0],3600)
            windows=history(db,90000)
            self.assertEqual(windows['15m'][-1]['observed'],90000)
            self.assertTrue(all(len(v)<=121 for v in windows.values()))
            self.assertTrue(all(r['observed']>=89100 for r in windows['15m']))
            db.close()
