"""Receiver import smoke with only the Python files its Docker stage copies."""
import fnmatch
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


def copied_files(stage):
    current=None;result=[]
    for line in (ROOT/'Dockerfile').read_text().splitlines():
        words=shlex.split(line)
        if words and words[0]=='FROM':current=words[-1]
        if current==stage and words and words[0]=='COPY' and not words[1].startswith('--'):
            result.extend(name for name in words[1:-1] if name.endswith('.py'))
    return result


class LifecyclePackagingTests(unittest.TestCase):
    def test_every_copied_python_module_is_in_build_context(self):
        patterns=(ROOT/'.dockerignore').read_text().splitlines()
        for name in set(copied_files('receiver')+copied_files('worker')):
            with self.subTest(module=name):
                self.assertTrue((ROOT/name).is_file())
                ignored=False
                for line in patterns:
                    pattern=line.strip()
                    if not pattern or pattern.startswith('#'):continue
                    include=pattern.startswith('!');pattern=pattern.removeprefix('!')
                    if fnmatch.fnmatchcase(name,pattern):ignored=not include
                self.assertFalse(ignored,'Copied module is excluded from Docker context')

    def test_lifecycle_in_receiver_worker_and_mcp_inherits_receiver(self):
        for stage in ('receiver','worker'):
            self.assertIn('managed_lifecycle.py',copied_files(stage),stage)
        ignored=False
        for line in (ROOT/'.dockerignore').read_text().splitlines():
            pattern=line.strip()
            if not pattern or pattern.startswith('#'):continue
            include=pattern.startswith('!');pattern=pattern.removeprefix('!')
            if fnmatch.fnmatchcase('managed_lifecycle.py',pattern):ignored=not include
        self.assertFalse(ignored,'Lifecycle module is excluded from the build context')
        self.assertIn('COPY --from=receiver /app/*.py ./',(ROOT/'Dockerfile').read_text())

    def test_receiver_only_copy_can_import_lifecycle_read_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in copied_files('receiver'):shutil.copyfile(ROOT/name,Path(directory)/name)
            script="""import sys,sqlite3
sys.path.insert(0,sys.argv[1])
import managed_lifecycle,managed_services,service_runtime_operations
db=sqlite3.connect(':memory:',isolation_level=None);db.row_factory=sqlite3.Row
managed_lifecycle.initialize(db)
assert managed_lifecycle.desired(db,'service','stopped')=='stopped'
assert service_runtime_operations.pending(db,'project','development') is False
"""
            result=subprocess.run([sys.executable,'-I','-c',script,directory],cwd=directory,capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
