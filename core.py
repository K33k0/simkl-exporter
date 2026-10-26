"""Read-only Simkl JSON export, without the VIP backup tool."""
import base64
import ctypes
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import request, parse, error

VERSION = '1.2.0'
STATUSES = {'shows': ('watching', 'plantowatch', 'hold', 'completed', 'dropped'),
            'anime': ('watching', 'plantowatch', 'hold', 'completed', 'dropped'),
            'movies': ('plantowatch', 'completed', 'dropped')}

class ExportError(Exception):
    pass

class ApiError(ExportError):
    def __init__(self, status, body):
        self.status = status
        self.code = body.get('error', 'http_error') if isinstance(body, dict) else 'http_error'
        super().__init__(f'Simkl HTTP {status}: {self.code}')

class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, url):
        raise ExportError('Unexpected API redirect. No credentials forwarded.')

class Api:
    def __init__(self, client_id, token='', opener=None, sleep=time.sleep):
        self.client_id, self.token = client_id, token
        self.opener = opener or request.build_opener(NoRedirect())
        self.sleep = sleep

    def call(self, path, params=None, body=None):
        if not path.startswith('/') or '?' in path or '://' in path:
            raise ExportError('Invalid API path')
        query = {'client_id': self.client_id, 'app-name': 'personal-json-exporter',
                 'app-version': VERSION, **(params or {})}
        url = 'https://api.simkl.com' + path + '?' + parse.urlencode(query)
        headers = {'User-Agent': f'PersonalSimklExporter/{VERSION}',
                   'Accept': 'application/json', 'simkl-api-key': self.client_id}
        if self.token and not path.startswith('/oauth2/'):
            headers['Authorization'] = 'Bearer ' + self.token
        data = None
        if body is not None:
            data = parse.urlencode(body).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        req = request.Request(url, data=data, headers=headers)
        for attempt in range(4):
            try:
                with self.opener.open(req, timeout=90) as response:
                    result = json.loads(response.read().decode('utf-8-sig'))
                if isinstance(result, dict) and result.get('error'):
                    raise ApiError(200, result)
                return result
            except error.HTTPError as exc:
                try:
                    payload = json.loads(exc.read().decode('utf-8'))
                except (ValueError, UnicodeError):
                    payload = {}
                if body is None and exc.code in (429, 500, 502, 503, 504) and attempt < 3:
                    retry = exc.headers.get('Retry-After', '')
                    delay = float(retry) if retry.isdigit() else 2 ** (attempt + 1)
                    if delay > 120:
                        raise ExportError('Rate limited. Try later.') from None
                    self.sleep(max(1, delay))
                    continue
                raise ApiError(exc.code, payload) from None
            except (error.URLError, TimeoutError, OSError):
                if body is None and attempt < 3:
                    self.sleep(2 ** (attempt + 1))
                    continue
                raise ExportError('Could not reach Simkl. Check your connection.') from None
            except (ValueError, UnicodeError):
                raise ExportError('Invalid JSON from Simkl; no export saved.') from None

def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent,
                                         suffix='.tmp', delete=False) as f:
            name = f.name
            json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        with open(name, encoding='utf-8') as f:
            json.load(f)
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)

def crypt(data, decrypt=False):
    if os.name != 'nt':
        raise ExportError('Credential storage requires Windows DPAPI.')
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, target = Blob(len(data), buf), Blob()
    dll = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    fn = dll.CryptUnprotectData if decrypt else dll.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ExportError('Windows could not unlock credentials. Reconnect the account.')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)

class Credentials:
    def __init__(self, folder):
        self.path = Path(folder) / 'credentials.json'
    def load(self):
        if not self.path.exists():
            return {}
        try:
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            return json.loads(crypt(base64.b64decode(saved['encrypted']), True))
        except (KeyError, ValueError):
            raise ExportError('Saved credentials damaged. Reconnect the account.') from None
    def save(self, data):
        raw = crypt(json.dumps(data).encode())
        atomic_json(self.path, {'encrypted': base64.b64encode(raw).decode()})

def checked_tokens(tokens):
    if not isinstance(tokens, dict) or not isinstance(tokens.get('access_token'), str) or not tokens['access_token']:
        raise ExportError('Simkl did not return an access token.')
    if 'media:read' not in tokens.get('scope', '').split():
        raise ExportError('Read access was not granted. Reconnect the account.')
    return tokens

def login(api, announce, sleep=time.sleep, clock=time.monotonic):
    device = api.call('/oauth2/device', body={'client_id': api.client_id, 'scope': 'media:read'})
    for key in ('device_code', 'user_code', 'verification_uri_complete', 'expires_in', 'interval'):
        if key not in device:
            raise ExportError('Unexpected device authorization response.')
    url = device['verification_uri_complete']
    parts = parse.urlsplit(url)
    if parts.scheme != 'https' or parts.netloc != 'simkl.com' or parts.path != '/pin':
        raise ExportError('Unexpected Simkl approval URL.')
    deadline = clock() + float(device['expires_in'])
    interval = max(1, float(device['interval']))
    announce(device['user_code'], url)
    while clock() + interval < deadline:
        sleep(interval)
        try:
            return checked_tokens(api.call('/oauth2/token', body={
                'client_id': api.client_id,
                'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
                'device_code': device['device_code']}))
        except ApiError as exc:
            if exc.code == 'authorization_pending':
                continue
            if exc.code == 'slow_down':
                interval += 5
                continue
            if exc.code in ('expired_token', 'access_denied'):
                break
            raise
    raise ExportError('Approval expired or declined. Launch again to reconnect.')

def validate(data, category):
    if not isinstance(data, dict) or set(data) - {category}:
        raise ExportError(f'Unexpected {category} response; refusing partial export.')
    rows = data.get(category, [])
    if not isinstance(rows, list):
        raise ExportError(f'Invalid {category} array.')
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or row.get('status') not in STATUSES[category]:
            raise ExportError(f'Missing or unknown status in {category}.')
        media = row.get('movie' if category == 'movies' else 'show', {})
        identity = media.get('ids', {}).get('simkl')
        if identity is None:
            raise ExportError(f'Missing Simkl ID in {category}.')
        if 'user_rating' not in row:
            raise ExportError(f'Missing personal rating field in {category}.')
        rating = row['user_rating']
        if rating is not None and (type(rating) is not int or not 1 <= rating <= 10):
            raise ExportError(f'Invalid personal rating in {category}.')
        if not isinstance(row.get('memo'), dict):
            raise ExportError(f'Memo field missing in {category}; notes coverage cannot be verified.')
        key = (identity, row.get('rewatch_id') if row.get('is_rewatch') else None)
        if key in seen:
            raise ExportError(f'Duplicate item in {category}.')
        seen.add(key)
    return rows

def fetch_type(api, category, paid=False, status='all', mode='yes'):
    params = {'extended': 'full', 'memos': 'yes', 'include_all_episodes': mode,
              'episode_watched_at': 'yes'}
    if paid:
        params['allow_rewatch'] = 'yes'
    try:
        rows = validate(api.call(f'/sync/all-items/{category}/{status}', params=params), category)
        if status != 'all' and any(r['status'] != status for r in rows if not r.get('is_rewatch')):
            raise ExportError('Simkl ignored the requested status filter.')
        return rows
    except ApiError as exc:
        if exc.code != 'max_items':
            raise
        if status == 'all':
            rows = []
            for bucket in STATUSES[category]:
                rows.extend(fetch_type(api, category, paid, bucket, mode))
            return validate({category: rows}, category)
        if mode == 'yes':
            return fetch_type(api, category, paid, status, 'original')
        raise ExportError(f'{category}/{status} exceeds API limits after splitting; no partial file saved.') from None

def pages(api, path, key):
    result, meta = [], None
    page, total = 1, None
    while True:
        body = api.call(path, params={'limit': 500, 'page': page})
        if not isinstance(body, dict) or not isinstance(body.get(key), list):
            raise ExportError('Invalid custom-list response.')
        p = body.get('pagination', {})
        if p.get('page') != page or type(p.get('total_pages')) is not int:
            raise ExportError('Unverifiable custom-list pagination.')
        if total is None:
            total = p.get('total_items')
            meta = {k: v for k, v in body.items() if k not in (key, 'pagination')}
        if p.get('total_items') != total:
            raise ExportError('Custom list changed during export. Retry.')
        result.extend(body[key])
        if page >= p['total_pages']:
            break
        page += 1
        if page * 500 > 10000:
            raise ExportError('Custom list exceeds API paging limit; full export unavailable.')
    if len(result) != total:
        raise ExportError('Custom-list count mismatch; no partial file saved.')
    return result, meta

def snapshot(api, progress=lambda text: None):
    progress('Reading account...')
    account = api.call('/users/settings')
    if not isinstance(account, dict) or not isinstance(account.get('account'), dict):
        raise ExportError('Invalid account settings response.')
    paid = account['account'].get('type', '').lower() in ('pro', 'vip')
    for attempt in range(2):
        before = api.call('/sync/activities')
        if not isinstance(before, dict) or 'all' not in before:
            raise ExportError('Invalid activity response.')
        libraries = {}
        for category in STATUSES:
            progress(f'Exporting {category}: all statuses, ratings, notes and episodes...')
            libraries[category] = fetch_type(api, category, paid)
        custom = {'available': paid, 'lists': []}
        if paid:
            progress('Exporting custom lists and list notes...')
            uid = account['account'].get('id')
            if type(uid) is not int:
                raise ExportError('Account ID missing; cannot export custom lists.')
            lists, _ = pages(api, f'/lists/user/{uid}', 'lists')
            for entry in lists:
                if type(entry.get('id')) is not int:
                    raise ExportError('Invalid custom list ID.')
                items, meta = pages(api, f"/lists/{entry['id']}", 'items')
                custom['lists'].append({'listing': entry, 'metadata': meta, 'items': items})
        else:
            custom['reason'] = 'Custom-list API is PRO/VIP-only. All core watchlists included.'
        after = api.call('/sync/activities')
        if before != after:
            if attempt == 0:
                progress('Library changed during export; retrying once for a consistent snapshot...')
                continue
            raise ExportError('Library keeps changing. Stop editing it and retry; no partial file saved.')
        summary = {}
        for category, rows in libraries.items():
            canonical = [r for r in rows if not r.get('is_rewatch')]
            summary[category] = {'titles': len(canonical), 'rewatch_sessions': len(rows) - len(canonical),
                'statuses': {s: sum(r['status'] == s for r in canonical) for s in STATUSES[category]},
                'rated_titles': sum(r['user_rating'] is not None for r in canonical),
                'titles_with_notes': sum(bool(r['memo'].get('text')) for r in canonical)}
        return {'_export': {'format': 'personal-simkl-export', 'version': VERSION,
                    'created_at': datetime.now(timezone.utc).isoformat(),
                    'source': 'https://simkl.com/', 'method': 'official API (no VIP backup)',
                    'consistent_activity_snapshot': True, 'summary': summary,
                    'notes': 'Memos preserved as returned. API caps memo.text at 140 characters.',
                    'episode_dates': 'yes may synthesize dates for bulk completions; oversized buckets use original recorded rows.'},
                **libraries, 'custom_lists': custom, 'account': account, 'activities': after}
    raise AssertionError('unreachable')

def export(api, folder, progress=lambda text: None, metadata_folder=None):
    data = snapshot(api, progress)
    from metadata import Metadata
    Metadata(Path(metadata_folder) if metadata_folder else Path(folder).parent / 'metadata-cache', api, progress).enrich(data)
    stamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S_%f')
    path = Path(folder) / f'SimklExport_{stamp}.json'
    atomic_json(path, data)
    return path, data['_export']['summary']
