"""Verified, encrypted integration credentials. All provider requests are GETs.

Public return values exclude credentials, ciphertext and raw provider responses.
The encryption key must be provisioned separately; this module never creates it.
"""
import json
import os
import re
import stat
import time
import urllib.error
import urllib.request
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from registry import transaction

PROVIDERS = ('github', 'cloudflare')
BASES = {'github': 'https://api.github.com', 'cloudflare': 'https://api.cloudflare.com/client/v4'}
MAX_RESPONSE = 1024 * 1024
SAFE_VERIFICATION_ERRORS = frozenset({
    'Provider response exceeded the supported size.',
    'Provider verification was unavailable or returned an invalid response.',
    'Provider returned invalid metadata.',
    'GitHub user API verification failed.',
    'GitHub repository metadata could not be verified.',
    'Cloudflare did not confirm this read operation.',
    'Cloudflare zone enumeration returned invalid metadata.',
    'Cloudflare zone identity could not be verified.',
    'Cloudflare pagination metadata is invalid.',
    'Token activity could not be verified. For an account-owned token, provide its account ID and check token-verification access.',
})


class IntegrationError(ValueError):
    """Only fixed, sanitized messages may be raised across the API boundary."""


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS integration_connections(
        provider TEXT PRIMARY KEY, revision INTEGER NOT NULL, ciphertext BLOB NOT NULL,
        enabled INTEGER NOT NULL, metadata TEXT NOT NULL, options TEXT NOT NULL,
        updated REAL NOT NULL, tested REAL NOT NULL, actor TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS integration_events(
        id INTEGER PRIMARY KEY, provider TEXT NOT NULL, created REAL NOT NULL,
        actor TEXT NOT NULL, operation TEXT NOT NULL, result TEXT NOT NULL)''')


def _provider(provider):
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise IntegrationError('Unsupported integration provider.')


def _revision(revision):
    if type(revision) is not int or revision < 0:
        raise IntegrationError('Invalid connection revision.')


def _token(token):
    if not isinstance(token, str) or not 1 <= len(token) <= 8192 or not all(33 <= ord(c) <= 126 for c in token):
        raise IntegrationError('Enter a valid credential without whitespace.')


def _options(provider, options):
    options = {} if options is None else options
    if not isinstance(options, dict) or set(options) - ({'account_id'} if provider == 'cloudflare' else set()):
        raise IntegrationError('Unsupported connection options.')
    if 'account_id' in options and (not isinstance(options['account_id'], str) or not re.fullmatch('[a-f0-9]{32}', options['account_id'])):
        raise IntegrationError('Cloudflare account ID must contain 32 hexadecimal characters.')
    return dict(options)


def _actor(actor):
    if not isinstance(actor, str) or not 1 <= len(actor) <= 120 or any(ord(c) < 32 for c in actor):
        raise IntegrationError('Invalid operator identity.')
    return actor


def _event(db, provider, actor, operation, result):
    db.execute('INSERT INTO integration_events(provider,created,actor,operation,result) VALUES(?,?,?,?,?)',
               (provider, time.time(), actor, operation, result))
    db.execute('DELETE FROM integration_events WHERE created<?', (time.time() - 90 * 86400,))


def _public(row):
    if row is None:
        return None
    return {'provider': row['provider'], 'revision': row['revision'], 'configured': True,
            'enabled': bool(row['enabled']), 'metadata': json.loads(row['metadata']),
            'updated': row['updated'], 'tested': row['tested'], 'actor': row['actor']}


def list_connections(db):
    initialize(db)
    result = {provider: {'provider': provider, 'revision': 0, 'configured': False,
                         'enabled': False, 'metadata': {}, 'updated': None, 'tested': None, 'actor': None}
              for provider in PROVIDERS}
    for row in db.execute('SELECT * FROM integration_connections'):
        result[row['provider']] = _public(row)
    return result


def _cipher(key_path):
    try:
        path = Path(key_path)
        with path.open('rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise IntegrationError('Encryption key must be a private, owner-readable file.')
            value = source.read(4097).strip()
        if len(value) > 4096:
            raise ValueError()
        return Fernet(value)
    except (OSError, TypeError, ValueError):
        raise IntegrationError('The external encryption key is missing, insecure or invalid. Existing credentials were not changed.') from None


def _decrypt(cipher, row):
    try:
        value = json.loads(cipher.decrypt(row['ciphertext']))
        if value['provider'] != row['provider']:
            raise ValueError()
        _token(value['token'])
        return value['token']
    except (InvalidToken, ValueError, TypeError, KeyError):
        raise IntegrationError('Stored credentials cannot be decrypted with this key. Restore the correct key; no credential was changed.') from None


def credential_for_use(db, provider, key_path):
    """Internal-only accessor. Never include its return value in API responses."""
    _provider(provider); initialize(db)
    row = db.execute('SELECT * FROM integration_connections WHERE provider=?', (provider,)).fetchone()
    if row is None or not row['enabled']:
        raise IntegrationError('This integration is not enabled.')
    return _decrypt(_cipher(key_path), row)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get_json(provider, path, token):
    """Bounded GET against fixed provider hosts; never follow credential redirects."""
    _provider(provider); _token(token)
    if not isinstance(path, str) or not path.startswith('/') or path.startswith('//') or any(c in path for c in ('\r', '\n', '#')):
        raise IntegrationError('Invalid provider request.')
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json', 'User-Agent': 'Shiftcodes-Deploy-Monitor'}
    if provider == 'github':
        headers.update({'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'})
    request = urllib.request.Request(BASES[provider] + path, headers=headers, method='GET')
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=12) as response:
            raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise IntegrationError('Provider response exceeded the supported size.')
        return json.loads(raw)
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        raise IntegrationError('Provider request was rejected (HTTP %d).' % status) from None
    except IntegrationError:
        raise
    except (OSError, ValueError, urllib.error.URLError):
        raise IntegrationError('Provider verification was unavailable or returned an invalid response.') from None


def _text(value, limit=200):
    if not isinstance(value, str) or not 1 <= len(value) <= limit or any(ord(c) < 32 for c in value):
        raise IntegrationError('Provider returned invalid metadata.')
    return value


def verify_github(token, options=None, get=None):
    get = get or get_json
    user = get('github', '/user', token)
    if not isinstance(user, dict):
        raise IntegrationError('GitHub user API verification failed.')
    identity = _text(user.get('login'), 100)
    repositories = []
    truncated = False
    for page in range(1, 6):
        rows = get('github', '/user/repos?per_page=100&page=' + str(page), token)
        if not isinstance(rows, list) or len(rows) > 100:
            raise IntegrationError('GitHub repository metadata could not be verified.')
        for row in rows:
            if not isinstance(row, dict) or type(row.get('id')) is not int:
                raise IntegrationError('GitHub repository metadata could not be verified.')
            repositories.append({'id': row['id'], 'repository': _text(row.get('full_name')), 'private': row.get('private') is True})
        if len(rows) < 100:
            break
        truncated = page == 5
    return {'api_access': 'verified', 'identity': identity, 'repository_access': 'metadata-verified',
            'repositories': repositories, 'repositories_truncated': truncated,
            'clone_access': 'unverified', 'webhook_management': 'unverified'}


def _cf_result(value):
    if not isinstance(value, dict) or value.get('success') is not True:
        raise IntegrationError('Cloudflare did not confirm this read operation.')
    return value.get('result')


def verify_cloudflare(token, options=None, get=None):
    get = get or get_json
    options = _options('cloudflare', options)
    zones, account_ids = [], []
    truncated = False
    for page in range(1, 6):
        response = get('cloudflare', '/zones?per_page=50&page=' + str(page), token)
        rows = _cf_result(response)
        if not isinstance(rows, list) or len(rows) > 50:
            raise IntegrationError('Cloudflare zone enumeration returned invalid metadata.')
        for row in rows:
            if not isinstance(row, dict):
                raise IntegrationError('Cloudflare zone enumeration returned invalid metadata.')
            zone_id = row.get('id'); account_id = (row.get('account') or {}).get('id')
            if not isinstance(zone_id, str) or not re.fullmatch('[a-f0-9]{32}', zone_id):
                raise IntegrationError('Cloudflare zone identity could not be verified.')
            if isinstance(account_id, str) and re.fullmatch('[a-f0-9]{32}', account_id) and account_id not in account_ids:
                account_ids.append(account_id)
            zones.append({'id': zone_id, 'name': _text(row.get('name'), 253),
                          'status': _text(row.get('status'), 40), 'account_id': account_id if account_id in account_ids else None})
        pages = (response.get('result_info') or {}).get('total_pages', 1)
        if type(pages) is not int or not 0 <= pages <= 1000000:
            raise IntegrationError('Cloudflare pagination metadata is invalid.')
        if page >= pages:
            break
        truncated = page == 5
    kind, verified_account = None, None
    try:
        result = _cf_result(get('cloudflare', '/user/tokens/verify', token))
        if isinstance(result, dict) and result.get('status') == 'active':
            kind = 'user'
    except IntegrationError:
        pass  # Account tokens need their account verification endpoint.
    if kind is None:
        candidates = [options['account_id']] if options.get('account_id') else account_ids[:10]
        for account_id in candidates:
            try:
                result = _cf_result(get('cloudflare', '/accounts/' + account_id + '/tokens/verify', token))
                if isinstance(result, dict) and result.get('status') == 'active':
                    kind, verified_account = 'account', account_id
                    break
            except IntegrationError:
                continue
    if kind is None:
        raise IntegrationError('Token activity could not be verified. For an account-owned token, provide its account ID and check token-verification access.')
    return {'api_access': 'verified', 'token_kind': kind, 'account_id': verified_account,
            'zone_read': 'verified', 'zones': zones, 'zones_truncated': truncated,
            'dns_write': 'unverified', 'origin_routing': 'unverified', 'tls': 'unverified'}


def verify(provider, token, options):
    return verify_github(token, options) if provider == 'github' else verify_cloudflare(token, options)


def _verified_metadata(provider, token, options, verifier):
    try:
        metadata = (verifier or verify)(provider, token, options)
        if not isinstance(metadata, dict) or metadata.get('api_access') != 'verified':
            raise ValueError()
        encoded = json.dumps(metadata, sort_keys=True, allow_nan=False)
        if len(encoded) > 262144 or token in encoded:
            raise ValueError()
        # Provider adapters explicitly construct metadata; never persist their
        # raw responses, headers, credential inputs or unknown top-level fields.
        allowed = ({'api_access', 'identity', 'repository_access', 'repositories', 'repositories_truncated', 'clone_access', 'webhook_management'}
                   if provider == 'github' else {'api_access', 'token_kind', 'account_id', 'zone_read', 'zones', 'zones_truncated', 'dns_write', 'origin_routing', 'tls'})
        if set(metadata) - allowed:
            raise ValueError()
        return encoded
    except IntegrationError as error:
        message = str(error)
        if message in SAFE_VERIFICATION_ERRORS or re.fullmatch(r'Provider request was rejected \(HTTP [1-5][0-9]{2}\)\.', message):
            raise IntegrationError(message + ' The previous connection is unchanged.') from None
        raise IntegrationError('Credential verification failed. Check provider access and connection options. The previous connection is unchanged.') from None
    except Exception:
        raise IntegrationError('Credential verification failed. Check provider access and connection options. The previous connection is unchanged.') from None


def save_connection(db, provider, revision, credential, key_path, options=None, verifier=None, actor='admin'):
    _provider(provider); _revision(revision); _token(credential); _actor(actor)
    options = _options(provider, options)
    initialize(db)
    cipher = _cipher(key_path)
    # Do not create a mixture of records encrypted with an accidental new key.
    for row in db.execute('SELECT * FROM integration_connections'):
        _decrypt(cipher, row)
    if list_connections(db)[provider]['revision'] != revision:
        raise IntegrationError('Connection settings changed. Reload before saving.')
    try:
        metadata = _verified_metadata(provider, credential, options, verifier)
    except IntegrationError:
        _event(db, provider, actor, 'replace', 'verification-failed')
        raise
    encrypted = cipher.encrypt(json.dumps({'provider': provider, 'token': credential}).encode())
    with transaction(db):
        if list_connections(db)[provider]['revision'] != revision:
            raise IntegrationError('Connection settings changed during verification. Reload before saving.')
        for row in db.execute('SELECT * FROM integration_connections'):
            _decrypt(cipher, row)
        now = time.time()
        db.execute('INSERT OR REPLACE INTO integration_connections VALUES(?,?,?,?,?,?,?,?,?)',
                   (provider, revision + 1, encrypted, 1, metadata, json.dumps(options), now, now, actor))
        _event(db, provider, actor, 'replace', 'verified-and-saved')
        result = list_connections(db)[provider]
    return result


def disable_connection(db, provider, revision, actor='admin'):
    _provider(provider); _revision(revision); _actor(actor); initialize(db)
    with transaction(db):
        current = list_connections(db)[provider]
        if not current['configured'] or current['revision'] != revision:
            raise IntegrationError('Connection settings changed. Reload before disabling.')
        db.execute('UPDATE integration_connections SET enabled=0,revision=revision+1,updated=?,actor=? WHERE provider=?',
                   (time.time(), actor, provider))
        _event(db, provider, actor, 'disable', 'disabled-locally')
        result = list_connections(db)[provider]
    return result


def test_connection(db, provider, key_path, verifier=None, actor='admin'):
    _provider(provider); _actor(actor); initialize(db)
    row = db.execute('SELECT * FROM integration_connections WHERE provider=?', (provider,)).fetchone()
    if row is None:
        raise IntegrationError('This integration is not configured.')
    token = _decrypt(_cipher(key_path), row)
    try:
        metadata = _verified_metadata(provider, token, json.loads(row['options']), verifier)
    except IntegrationError:
        _event(db, provider, actor, 'test', 'verification-failed')
        raise
    with transaction(db):
        current = list_connections(db)[provider]
        if current['revision'] != row['revision']:
            raise IntegrationError('Connection changed during verification. Test the current connection again.')
        db.execute('UPDATE integration_connections SET metadata=?,tested=? WHERE provider=?', (metadata, time.time(), provider))
        _event(db, provider, actor, 'test', 'verified')
        result = list_connections(db)[provider]
    return result
