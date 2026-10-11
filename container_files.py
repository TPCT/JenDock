"""Bounded read-only application files via pinned Linux proc-root descriptors.

Requires visibility and permission to read the container process root. No binaries,
shell, archive extraction or writes run inside a target. Unsupported deployments fail
closed. The trusted inspector must authorize exact managed/adopted container ownership.
"""
import base64
from contextlib import contextmanager
import json
import os
from pathlib import PurePosixPath
import re
import stat
import time

MAX_PREVIEW = 65536
MAX_ENTRIES = 4096
MAX_PATH = 1024
DENIED_ROOTS = ('/proc','/sys','/dev','/etc','/run','/var/run','/root','/boot')
SECRET_NAMES = re.compile(r'(^\.env($|\.)|^\.(ssh|aws|azure|kube|docker|git|gnupg)$|^\.(npmrc|pypirc|netrc)$|^(auth|credentials|secrets?)(\.|$)|^id_(rsa|dsa|ecdsa|ed25519)|\.(pem|key|p12|pfx|jks|keystore|sqlite|sqlite3|db|sql|dump)$)', re.I)
SECRET_ENV = re.compile(r'password|passwd|secret|token|api[_-]?key|access[_-]?key|credential|private[_-]?key',re.I)
TEXT_SUFFIXES = frozenset({'.py','.js','.mjs','.cjs','.ts','.tsx','.jsx','.php','.json','.yaml','.yml','.toml','.ini','.conf','.txt','.md','.css','.scss','.html','.htm','.vue','.xml','.sh','.rb','.go','.rs','.java','.properties','.lock','.example'})
TEXT_NAMES = frozenset({'Dockerfile','Makefile','README','LICENSE','artisan','requirements.txt','Gemfile','Procfile'})
SECRET_CONTENT = re.compile(r'''-----BEGIN (?:[A-Z ]*PRIVATE KEY|PGP PRIVATE)|(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)["']?\s*(?:=>|=|:)\s*[^\s,;#}\]]{3,}''', re.I)
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class FileBrowserError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def _error(code, message, status=400):
    raise FileBrowserError(code, message, status)


def _path(value):
    if (not isinstance(value,str) or len(value)>MAX_PATH or value.startswith('/')
            or any(ord(c)<32 or ord(c)==127 for c in value)):
        _error('invalid_path','Choose a relative application path.')
    if value and any(part in ('', '.', '..') for part in value.split('/')):
        _error('invalid_path','Path traversal and empty path components are not allowed.')
    if any(SECRET_NAMES.search(part) for part in value.split('/')):
        _error('protected_file','This path is protected and cannot be inspected.',403)
    return value.split('/') if value else []


def _inside(path, root):
    return path == root or path.startswith(root.rstrip('/')+'/')


def _approved_root(value):
    if (not isinstance(value,str) or not value.startswith('/') or value=='/' or len(value)>MAX_PATH
            or any(ord(c)<32 or ord(c)==127 for c in value)
            or value != '/' + '/'.join(part for part in value.split('/') if part)
            or any(part in ('.','..') for part in value.split('/'))): return False
    return not any(_inside(value, root) for root in DENIED_ROOTS) and not any(SECRET_NAMES.search(part) for part in value.split('/'))


def _identity(record):
    return (record.get('Id'),record.get('Image'),record.get('State',{}).get('Pid'),
            record.get('State',{}).get('StartedAt'),record.get('Config',{}).get('WorkingDir'),
            tuple(sorted((m.get('Destination',''),m.get('Type',''),bool(m.get('RW'))) for m in record.get('Mounts',[]))))


class FileBrowser:
    def __init__(self, inspector, *, proc_root=None, change_provider=None):
        self.inspector = inspector
        self.proc_root = proc_root or os.environ.get('CONTAINER_FILES_PROC_ROOT','/host/proc')
        self.change_provider = change_provider

    def _inspect(self, project, container, expected):
        if not isinstance(expected,str) or not re.fullmatch('[a-f0-9]{64}',expected):
            _error('invalid_identity','Refresh the container identity before browsing.',409)
        value=self.inspector(project,container)
        record=value[0] if isinstance(value,tuple) else value
        if not isinstance(record,dict) or record.get('Id') != expected:
            _error('identity_changed','Container identity changed. Refresh and retry.',409)
        return record

    def _starttime(self,pid):
        with open(os.path.join(self.proc_root,str(pid),'stat'),encoding='ascii') as source:
            value=source.read(4096)
        return value.rsplit(')',1)[1].split()[19]

    @contextmanager
    def _access(self,project,container,expected):
        record=self._inspect(project,container,expected)
        root=record.get('Config',{}).get('WorkingDir')
        pid=record.get('State',{}).get('Pid')
        if not _approved_root(root):
            _error('unsupported','This container has no supported application working directory.',409)
        if not record.get('State',{}).get('Running') or type(pid) is not int or pid<=0:
            _error('unsupported','Start the container before browsing its application files.',409)
        descriptors=[]
        try:
            try:
                started=self._starttime(pid)
                # This one kernel proc magic link identifies the already-authorized process root.
                rootfd=os.open(os.path.join(self.proc_root,str(pid),'root'),os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC)
                descriptors.append(rootfd); pinned=os.fstat(rootfd)
                current=rootfd
                for part in PurePosixPath(root).parts[1:]:
                    current=os.open(part,_DIR_FLAGS,dir_fd=current);descriptors.append(current)
            except (OSError,ValueError,IndexError):
                _error('unsupported','Application files are unavailable: the worker needs safe process-root access and a real application directory.',409)
            yield current,record,root
            after=self._inspect(project,container,expected)
            try:
                root_after=os.stat(os.path.join(self.proc_root,str(pid),'root'))
                unchanged=(self._starttime(pid)==started and (root_after.st_dev,root_after.st_ino)==(pinned.st_dev,pinned.st_ino))
            except (OSError,ValueError,IndexError): unchanged=False
            if not unchanged or _identity(record)!=_identity(after):
                _error('identity_changed','Container runtime changed while reading. Refresh and retry.',409)
        finally:
            for descriptor in reversed(descriptors):os.close(descriptor)

    @contextmanager
    def _directory(self,rootfd,parts):
        descriptors=[];current=rootfd
        try:
            for part in parts:
                current=os.open(part,_DIR_FLAGS,dir_fd=current);descriptors.append(current)
            yield current
        except OSError:
            _error('path_unavailable','Directory is missing, inaccessible or contains a symbolic link.',404)
        finally:
            for descriptor in reversed(descriptors):os.close(descriptor)

    def _storage(self,record,root,path,changes):
        absolute=root.rstrip('/')+('/'+path if path else '')
        mounts=[m for m in record.get('Mounts',[]) if isinstance(m.get('Destination'),str) and _inside(absolute,m['Destination'])]
        if mounts:
            mount=max(mounts,key=lambda m:len(m['Destination']))
            return {'storage':'mount','mount':{'type':mount.get('Type','unknown'),'destination':mount['Destination'],'read_only':not bool(mount.get('RW'))}}
        if changes is None:return {'storage':'unknown'}
        return {'storage':'writable-layer' if any(absolute==changed['Path'] or changed['Kind']==1 and _inside(absolute,changed['Path']) for changed in changes) else 'image'}

    def _changes(self,identifier):
        if self.change_provider is None:return None
        try:
            values=self.change_provider(identifier)
            if not isinstance(values,list) or len(values)>MAX_ENTRIES:return None
            if any(not isinstance(row,dict) or not isinstance(row.get('Path'),str) or len(row['Path'])>MAX_PATH or row.get('Kind') not in (0,1,2) for row in values):return None
            return values
        except Exception:return None

    def list(self,project,container,container_id,path='',cursor=None,limit=100):
        parts=_path(path)
        if type(limit) is not int or not 1<=limit<=100:_error('invalid_limit','Choose a page size from 1 to 100.')
        base={'container_id':container_id,'root':None,'path':path,'observed':time.time(),'entries':[],'next_cursor':None,
              'capability':{'supported':True,'reason':None}}
        try:
            with self._access(project,container,container_id) as (rootfd,record,root):
                base['root']=root;changes=self._changes(container_id)
                with self._directory(rootfd,parts) as directory:
                    before=os.fstat(directory);stamp=[before.st_dev,before.st_ino,before.st_mtime_ns,path]
                    offset=0
                    if cursor is not None:
                        try:
                            if not isinstance(cursor,str) or len(cursor)>2048:raise ValueError()
                            decoded=json.loads(base64.urlsafe_b64decode(cursor.encode()))
                            offset=decoded['offset']
                            if decoded['stamp']!=stamp or type(offset) is not int or not 0<=offset<=MAX_ENTRIES:raise ValueError()
                        except (ValueError,TypeError,KeyError):_error('stale_cursor','Directory changed or cursor is invalid. Reload the directory.',409)
                    names=[]
                    with os.scandir(directory) as scan:
                        for entry in scan:
                            names.append(entry.name)
                            if len(names)>MAX_ENTRIES:_error('directory_too_large','This directory exceeds the safe browsing limit. Choose a narrower application subdirectory.',413)
                    names.sort();selected=names[offset:offset+limit]
                    for name in selected:
                        if any(ord(c)<32 or ord(c)==127 for c in name) or len(name)>255:continue
                        try:info=os.stat(name,dir_fd=directory,follow_symlinks=False)
                        except OSError:_error('directory_changed','Directory changed while listing. Reload it.',409)
                        kind='directory' if stat.S_ISDIR(info.st_mode) else 'file' if stat.S_ISREG(info.st_mode) else 'symlink' if stat.S_ISLNK(info.st_mode) else 'special'
                        relative='/'.join(parts+[name]);blocked=None
                        if SECRET_NAMES.search(name):blocked='Protected file or directory'
                        elif kind in ('symlink','special'):blocked='Symbolic links and special files are unavailable'
                        elif kind=='file' and PurePosixPath(name).suffix.lower() not in TEXT_SUFFIXES and name not in TEXT_NAMES:blocked='File type is unavailable for text preview'
                        elif kind=='file' and info.st_nlink>1:blocked='Hard-linked files are unavailable'
                        elif kind=='file' and info.st_size>MAX_PREVIEW:blocked='File exceeds the 64 KiB preview limit'
                        base['entries'].append({'name':name,'path':relative,'type':kind,'size':info.st_size if kind=='file' else None,
                            'previewable':kind=='file' and blocked is None,'blocked_reason':blocked,**self._storage(record,root,relative,changes)})
                    after=os.fstat(directory)
                    if after.st_mtime_ns!=before.st_mtime_ns:_error('directory_changed','Directory changed while listing. Reload it.',409)
                    if offset+limit<len(names):base['next_cursor']=base64.urlsafe_b64encode(json.dumps({'stamp':stamp,'offset':offset+limit},separators=(',',':')).encode()).decode()
        except FileBrowserError as error:
            if error.code!='unsupported':raise
            base['capability']={'supported':False,'reason':str(error)}
        return base

    def preview(self,project,container,container_id,path):
        parts=_path(path)
        if not parts:_error('invalid_path','Choose a text file to preview.')
        if PurePosixPath(parts[-1]).suffix.lower() not in TEXT_SUFFIXES and parts[-1] not in TEXT_NAMES:
            _error('unsupported_type','This file type is unavailable for text preview.',415)
        with self._access(project,container,container_id) as (rootfd,record,root):
            with self._directory(rootfd,parts[:-1]) as directory:
                try:descriptor=os.open(parts[-1],os.O_PATH|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=directory)
                except OSError:_error('path_unavailable','File is missing, inaccessible or a symbolic link.',404)
                try:
                    before=os.fstat(descriptor)
                    if not stat.S_ISREG(before.st_mode):_error('unsupported_type','Only regular text files can be previewed.',415)
                    if before.st_nlink>1:_error('protected_file','Hard-linked files cannot be previewed.',403)
                    if before.st_size>MAX_PREVIEW:_error('file_too_large','File exceeds the 64 KiB preview limit.',413)
                    pinned=descriptor
                    try:descriptor=os.open('/proc/self/fd/'+str(pinned),os.O_RDONLY|os.O_NONBLOCK|os.O_CLOEXEC)
                    except OSError:_error('path_unavailable','File permissions do not allow a preview.',403)
                    finally:
                        if descriptor!=pinned:os.close(pinned)
                    content=b''
                    while len(content)<=MAX_PREVIEW:
                        chunk=os.read(descriptor,MAX_PREVIEW+1-len(content))
                        if not chunk:break
                        content+=chunk
                    after=os.fstat(descriptor)
                    if len(content)>MAX_PREVIEW:_error('file_too_large','File exceeds the 64 KiB preview limit.',413)
                    if (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                        _error('file_changed','File changed while reading. Reload the preview.',409)
                    try:text=content.decode('utf-8')
                    except UnicodeDecodeError:_error('unsupported_type','Only UTF-8 text files can be previewed.',415)
                    if any(ord(c)<32 and c not in '\n\r\t' for c in text):_error('unsupported_type','Binary files cannot be previewed.',415)
                    secret_values=[]
                    for entry in record.get('Config',{}).get('Env',[]) or []:
                        if isinstance(entry,str) and '=' in entry:
                            key,value=entry.split('=',1)
                            if SECRET_ENV.search(key) and len(value)>=3:secret_values.append(value)
                    if SECRET_CONTENT.search(text) or any(value in text for value in secret_values):_error('protected_content','This file may contain credentials and cannot be previewed.',403)
                    result={'container_id':container_id,'root':root,'path':path,'text':text,'size':len(content),'truncated':False,
                            'observed':time.time(),**self._storage(record,root,path,self._changes(container_id))}
                finally:os.close(descriptor)
        return result
