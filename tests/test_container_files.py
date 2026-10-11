import os
from pathlib import Path
import tempfile
import unittest

import container_files as files


class FileBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.record = {'Id':'a'*64,'Image':'sha256:'+'b'*64,'State':{'Pid':os.getpid(),'Running':True,'StartedAt':'now'},
                       'Config':{'WorkingDir':str(self.root)},'Mounts':[]}
        self.reader = files.FileBrowser(lambda project, name: (self.record, None, None, None), proc_root='/proc')
        self.args = ('app', 'app-web', 'a'*64)

    def tearDown(self): self.temp.cleanup()

    def test_list_pagination_preview_and_mount_metadata(self):
        (self.root/'a.txt').write_text('hello')
        (self.root/'b.txt').write_text('world')
        first = self.reader.list(*self.args, limit=1)
        self.assertEqual(first['entries'][0]['name'], 'a.txt')
        second = self.reader.list(*self.args, cursor=first['next_cursor'], limit=1)
        self.assertEqual(second['entries'][0]['name'], 'b.txt')
        self.assertIsNone(second['next_cursor'])
        self.assertEqual(self.reader.preview(*self.args, path='a.txt')['text'], 'hello')
        self.record['Mounts']=[{'Destination':str(self.root),'Type':'volume','Source':'HOST-SECRET','RW':True}]
        result=self.reader.list(*self.args)
        self.assertEqual(result['entries'][0]['storage'], 'mount')
        self.assertNotIn('HOST-SECRET',str(result))

    def test_paths_secrets_symlinks_binary_and_oversized_denied(self):
        (self.root/'.env').write_text('SECRET')
        (self.root/'binary').write_bytes(b'\x00secret')
        (self.root/'large.txt').write_text('x'*(files.MAX_PREVIEW+1))
        (self.root/'linked').symlink_to('/etc/passwd')
        (self.root/'secret.txt').write_text('password="private-value"')
        for path in ('../etc/passwd','/etc/passwd','.env','linked','binary','large.txt','secret.txt'):
            with self.subTest(path=path), self.assertRaises(files.FileBrowserError):
                self.reader.preview(*self.args,path=path)
        entries=self.reader.list(*self.args)['entries']
        self.assertFalse(next(row for row in entries if row['name']=='.env')['previewable'])

    def test_identity_change_fenced_before_and_after(self):
        (self.root/'safe.txt').write_text('safe')
        with self.assertRaises(files.FileBrowserError): self.reader.list('app','app-web','c'*64)
        count=0
        def inspector(*args):
            nonlocal count
            count+=1
            if count>1: return dict(self.record, Image='different')
            return self.record
        reader=files.FileBrowser(inspector,proc_root='/proc')
        with self.assertRaises(files.FileBrowserError): reader.preview(*self.args,path='safe.txt')

    def test_unsupported_roots_and_missing_proc_are_explicit(self):
        for root in ('/','/etc','/proc/1/root','/run/secrets'):
            self.record['Config']['WorkingDir']=root
            self.assertFalse(self.reader.list(*self.args)['capability']['supported'])
        self.record['Config']['WorkingDir']=str(self.root)
        reader=files.FileBrowser(lambda *args:self.record,proc_root=str(self.root/'absent'))
        self.assertFalse(reader.list(*self.args)['capability']['supported'])

    def test_stale_cursor_and_symlink_directory_denied(self):
        (self.root/'a').write_text('a'); (self.root/'b').write_text('b')
        first=self.reader.list(*self.args,limit=1)
        (self.root/'c').write_text('c')
        with self.assertRaises(files.FileBrowserError): self.reader.list(*self.args,cursor=first['next_cursor'])
        (self.root/'escape').symlink_to('/etc',target_is_directory=True)
        with self.assertRaises(files.FileBrowserError): self.reader.list(*self.args,path='escape')

    def test_hardlink_json_credentials_and_known_secret_values_rejected(self):
        (self.root/'.env').write_text('PRIVATE')
        os.link(self.root/'.env',self.root/'alias.txt')
        (self.root/'config.json').write_text('{"password":"private-value"}')
        (self.root/'source.py').write_text('opaque = "current-secret-value"')
        self.record['Config']['Env']=['API_TOKEN=current-secret-value']
        for path in ('alias.txt','config.json','source.py'):
            with self.subTest(path=path),self.assertRaises(files.FileBrowserError):
                self.reader.preview(*self.args,path=path)

    def test_noncanonical_roots_cannot_bypass_os_directory_deny(self):
        for root in ('//etc','//root','//var/run','/app/../etc','/app//nested','/app/'):
            self.record['Config']['WorkingDir']=root
            self.assertFalse(self.reader.list(*self.args)['capability']['supported'])

    def test_changed_directory_does_not_mark_all_children_writable(self):
        (self.root/'image.txt').write_text('image source')
        (self.root/'changed.txt').write_text('changed source')
        changes=[{'Path':str(self.root),'Kind':0},{'Path':str(self.root/'changed.txt'),'Kind':0}]
        reader=files.FileBrowser(lambda *args:self.record,proc_root='/proc',change_provider=lambda identifier:changes)
        entries={row['name']:row for row in reader.list(*self.args)['entries']}
        self.assertEqual(entries['image.txt']['storage'],'image')
        self.assertEqual(entries['changed.txt']['storage'],'writable-layer')

    def test_special_file_cannot_be_opened_for_preview(self):
        os.mkfifo(self.root/'pipe.txt')
        with self.assertRaises(files.FileBrowserError):self.reader.preview(*self.args,path='pipe.txt')

    def test_directory_scan_limit_is_enforced_before_filtering(self):
        from unittest.mock import patch
        for index in range(3):(self.root/(str(index)+'.txt')).write_text('safe')
        with patch.object(files,'MAX_ENTRIES',2),self.assertRaises(files.FileBrowserError) as error:
            self.reader.list(*self.args,limit=1)
        self.assertEqual(error.exception.code,'directory_too_large')

    def test_unquoted_and_php_secret_assignments_rejected(self):
        for name, content in [('config.ini','password=actualsecret'),
                              ('settings.yaml','password: actualsecret'),
                              ('config.php', "<?php return ['password' => 'actualsecret'];")]:
            (self.root/name).write_text(content)
            with self.subTest(name=name),self.assertRaises(files.FileBrowserError) as error:
                self.reader.preview(*self.args,path=name)
            self.assertEqual(error.exception.code,'protected_content')
