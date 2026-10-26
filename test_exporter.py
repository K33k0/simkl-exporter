import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.parse import urlsplit, parse_qs
from core import Api, ApiError, Credentials, ExportError, STATUSES, atomic_json, export, fetch_type, login, pages, snapshot, validate


def row(category, ident, status, rating=8):
    return {'status': status, 'user_rating': rating, 'user_rated_at': '2026-10-25T12:00:00Z',
            'memo': {'text': '私のメモ\nsecond line', 'is_private': True},
            'movie' if category == 'movies' else 'show': {'ids': {'simkl': ident}},
            'seasons': [{'number': 1, 'episodes': [{'number': 1, 'watched_at': '2020-01-01T01:02:03Z'}]}],
            'future_field': {'preserve': True}}

class Fake:
    def __init__(self):
        self.calls = []
        self.data = {c: [row(c, n + 1, s) for n, s in enumerate(ss)] for c, ss in STATUSES.items()}
    def call(self, path, params=None, body=None):
        self.calls.append((path, copy.deepcopy(params), body))
        if path == '/users/settings':
            return {'account': {'type': 'free', 'id': 1}}
        if path == '/sync/activities':
            return {'all': '2026-10-25T12:00:00Z'}
        c = path.split('/')[3]
        return {c: copy.deepcopy(self.data[c])}

class Tests(unittest.TestCase):
    def test_every_library_status_rating_memo_and_unknown_fields_preserved(self):
        api = Fake()
        data = snapshot(api)
        for c in STATUSES:
            self.assertEqual(data[c], api.data[c])
            self.assertEqual(set(data['_export']['summary'][c]['statuses']), set(STATUSES[c]))
        for path, p, body in api.calls:
            if '/all-items/' in path:
                self.assertTrue(path.endswith('/all'))
                self.assertEqual(p['memos'], 'yes')
                self.assertEqual(p['include_all_episodes'], 'yes')
                self.assertEqual(p['extended'], 'full')
                self.assertEqual(p['episode_watched_at'], 'yes')
                self.assertNotIn('date_from', p)
        self.assertFalse(data['custom_lists']['available'])

    def test_no_note_or_rating_silently_lost(self):
        for field in ('memo', 'user_rating', 'status'):
            r = row('shows', 1, 'completed')
            del r[field]
            with self.assertRaises(ExportError):
                validate({'shows': [r]}, 'shows')

    def test_null_rating_empty_memo_and_empty_library(self):
        r = row('movies', 1, 'plantowatch', None)
        r['memo'] = {}
        self.assertEqual(validate({'movies': [r]}, 'movies'), [r])
        self.assertEqual(validate({}, 'movies'), [])
        for body in (None, [], {'movies': None}, {'error': 'oops'}):
            with self.assertRaises(ExportError):
                validate(body, 'movies')

    def test_duplicates_rejected_but_distinct_rewatches_kept(self):
        r = row('shows', 1, 'completed')
        with self.assertRaises(ExportError):
            validate({'shows': [r, r]}, 'shows')
        rewatch = {**r, 'is_rewatch': True, 'rewatch_id': 9}
        self.assertEqual(len(validate({'shows': [r, rewatch]}, 'shows')), 2)

    def test_max_items_split_all_statuses(self):
        class Split:
            def call(self, path, params=None):
                c, s = path.split('/')[-2:]
                if s == 'all':
                    raise ApiError(400, {'error': 'max_items'})
                return {c: [row(c, STATUSES[c].index(s), s)]}
        self.assertEqual(len(fetch_type(Split(), 'shows')), 5)
        self.assertEqual(len(fetch_type(Split(), 'movies')), 3)

    def test_oversize_original_fallback_and_fatal_limit(self):
        class Split:
            def call(self, path, params=None):
                if params['include_all_episodes'] == 'yes':
                    raise ApiError(400, {'error': 'max_items'})
                return {'shows': [row('shows', 1, 'completed')]}
        self.assertEqual(len(fetch_type(Split(), 'shows', status='completed')), 1)
        class TooBig:
            def call(self, *args, **kwargs):
                raise ApiError(400, {'error': 'max_items'})
        with self.assertRaises(ExportError):
            fetch_type(TooBig(), 'shows')

    def test_failed_export_publishes_nothing(self):
        api = Fake()
        del api.data['anime'][0]['memo']
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ExportError):
                export(api, Path(td))
            self.assertEqual(list(Path(td).iterdir()), [])

    def test_atomic_unicode_roundtrip_and_existing_file_survives_invalid_data(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'export.json'
            data = {'memo': '日本語\nnew line'}
            atomic_json(path, data)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), data)
            with self.assertRaises(ValueError):
                atomic_json(path, {'x': float('nan')})
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), data)
            self.assertEqual(len(list(Path(td).iterdir())), 1)

    def test_changed_library_retried_once_then_fails(self):
        class Changing(Fake):
            def call(self, path, params=None, body=None):
                if path == '/sync/activities':
                    return {'all': str(len(self.calls)), 'changes': len(self.calls)}
                return super().call(path, params, body)
        with self.assertRaises(ExportError):
            snapshot(Changing())

    def test_pages_and_notes(self):
        class Paged:
            def call(self, path, params=None):
                return {'items': [{'note': {'public': 'line1\nline2'}, 'id': params['page']}],
                        'pagination': {'page': params['page'], 'total_pages': 2, 'total_items': 2}, 'name': 'List'}
        items, meta = pages(Paged(), '/lists/1', 'items')
        self.assertEqual(len(items), 2)
        self.assertEqual(meta['name'], 'List')
        self.assertEqual(items[0]['note']['public'], 'line1\nline2')

    def test_dpapi_credentials_roundtrip_no_plaintext(self):
        with tempfile.TemporaryDirectory() as td:
            store = Credentials(td)
            value = {'access_token': 'example-secret-token', 'client_id': 'client'}
            store.save(value)
            self.assertEqual(store.load(), value)
            self.assertNotIn('example-secret-token', store.path.read_text())

    def test_device_flow_pending_slowdown_success_readonly(self):
        elapsed, sleeps, calls = [0], [], []
        class Auth:
            client_id = 'client'
            def call(self, path, params=None, body=None):
                calls.append((path, body))
                if path == '/oauth2/device':
                    return {'device_code': 'private', 'user_code': 'ABCD-EFGH',
                            'verification_uri_complete': 'https://simkl.com/pin?user_code=ABCD-EFGH',
                            'expires_in': 900, 'interval': 5}
                if len(calls) == 2:
                    raise ApiError(400, {'error': 'authorization_pending'})
                if len(calls) == 3:
                    raise ApiError(400, {'error': 'slow_down'})
                return {'access_token': 'token', 'scope': 'media:read'}
        def sleep(n):
            sleeps.append(n)
            elapsed[0] += n
        tokens = login(Auth(), lambda code, url: None, sleep, lambda: elapsed[0])
        self.assertEqual(sleeps, [5, 5, 10])
        self.assertEqual(calls[0][1]['scope'], 'media:read')
        self.assertEqual(tokens['access_token'], 'token')

    def test_http_headers_query_retry_and_no_auth_post_replay(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"movies": []}'
        class Opener:
            def __init__(self): self.reqs = []
            def open(self, req, timeout):
                self.reqs.append(req)
                if len(self.reqs) == 1:
                    raise HTTPError(req.full_url, 429, 'rate limit', {'Retry-After': '2'}, io.BytesIO(b'{}'))
                return Response()
        opener, sleeps = Opener(), []
        api = Api('client', 'secret', opener=opener, sleep=sleeps.append)
        api.call('/sync/all-items/movies/all', {'memos': 'yes'})
        req = opener.reqs[-1]
        self.assertEqual(req.get_header('Authorization'), 'Bearer secret')
        self.assertEqual(parse_qs(urlsplit(req.full_url).query)['memos'], ['yes'])
        self.assertNotIn('secret', req.full_url)
        self.assertEqual(sleeps, [2])
        opener = Opener()
        with self.assertRaises(ApiError):
            Api('client', opener=opener).call('/oauth2/token', body={'client_id': 'client'})
        self.assertEqual(len(opener.reqs), 1)

if __name__ == '__main__':
    unittest.main(verbosity=2)
