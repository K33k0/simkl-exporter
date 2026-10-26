"""Public catalog enrichment. Never sends Simkl credentials to metadata services."""
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib import request, error, parse
from core import ExportError, NoRedirect, atomic_json

class Metadata:
    def __init__(self, folder, simkl, progress=lambda text: None):
        self.folder = Path(folder)
        self.simkl = simkl
        self.progress = progress

    def public(self, url, jikan=False):
        if parse.urlsplit(url).netloc not in ('v3-cinemeta.strem.io', 'api.tvmaze.com'):
            raise ExportError('Unexpected metadata host')
        class PublicRedirect(request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                target = parse.urlsplit(newurl)
                if target.scheme != 'https' or target.netloc != 'api.tvmaze.com':
                    raise ExportError('Unexpected catalog redirect')
                return request.Request(newurl, headers={'User-Agent':'PersonalSimklExporter/1.1','Accept':'application/json'})
        opener = request.build_opener(PublicRedirect())
        for attempt in range(3):
            try:
                req = request.Request(url, headers={'User-Agent': 'PersonalSimklExporter/1.1', 'Accept': 'application/json'})
                with opener.open(req, timeout=30) as response:
                    return json.loads(response.read().decode('utf-8'))
            except error.HTTPError as exc:
                if exc.code == 404:
                    return None
                if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                    retry = exc.headers.get('Retry-After', '')
                    time.sleep(min(60, max(3, int(retry) if retry.isdigit() else 3 ** (attempt + 1))))
                    continue
                raise ExportError(f'Metadata HTTP {exc.code}') from None
            except (error.URLError, TimeoutError, OSError, ValueError):
                if attempt < 2:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise ExportError('Metadata service unavailable') from None

    def cached(self, key, loader):
        path = self.folder / (key + '.json')
        try:
            saved = json.loads(path.read_text(encoding='utf-8'))
            if time.time() - saved['saved_at'] < 30 * 86400:
                return saved['data']
        except (OSError, ValueError, KeyError, TypeError):
            pass
        data = loader()
        atomic_json(path, {'saved_at': time.time(), 'data': data})
        return data

    def cinemeta(self, media, category):
        imdb = media.get('ids', {}).get('imdb', '')
        if not re.fullmatch(r'tt\d+', str(imdb)):
            return {'genres': [], 'main_cast': [], 'cast_status': 'no_imdb_id', 'sources': []}
        kind = 'series' if category == 'shows' else 'movie'
        url = f'https://v3-cinemeta.strem.io/meta/{kind}/{imdb}.json'
        def load():
            body = self.public(url)
            obj = body.get('meta') if isinstance(body, dict) else None
            if obj is None:
                return {'genres': [], 'main_cast': [], 'cast_status': 'not_found', 'sources': [url]}
            if not isinstance(obj, dict) or obj.get('id') != imdb:
                raise ExportError('Cinemeta identity mismatch')
            genres = self.strings(obj.get('genres', []))
            names = self.strings(obj.get('cast', []))
            if not names:
                names = self.strings([x.get('name') for x in obj.get('links', []) if x.get('category') == 'actor'])
            if not genres:
                genres = self.strings([x.get('name') for x in obj.get('links', []) if x.get('category') == 'genre'])
            return {'genres': genres, 'main_cast': [{'name': n} for n in names],
                    'cast_status': 'available' if names else 'no_cast_listed', 'sources': [url],
                    'cast_definition': 'Cinemeta featured cast, in provider order'}
        return self.cached(f'cinemeta-{kind}-{imdb}', load)

    @staticmethod
    def strings(values):
        if not isinstance(values, list):
            raise ExportError('Malformed metadata list')
        return list(dict.fromkeys(x.strip() for x in values if isinstance(x, str) and x.strip()))

    def graphql(self, query, variables):
        for attempt in range(3):
            try:
                req = request.Request('https://graphql.anilist.co',
                    data=json.dumps({'query': query, 'variables': variables}).encode(),
                    headers={'Content-Type':'application/json', 'User-Agent':'PersonalSimklExporter/1.1'})
                with request.build_opener(NoRedirect()).open(req, timeout=30) as response:
                    body = json.loads(response.read().decode('utf-8'))
                if not isinstance(body, dict) or body.get('errors'):
                    raise ExportError('AniList returned GraphQL errors')
                return body['data']
            except error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                    retry = exc.headers.get('Retry-After', '')
                    time.sleep(min(60, int(retry) if retry.isdigit() else 10))
                    continue
                raise ExportError(f'AniList HTTP {exc.code}') from None
            except (error.URLError, TimeoutError, OSError, ValueError, KeyError):
                if attempt < 2:
                    time.sleep(3)
                    continue
                raise ExportError('AniList unavailable') from None

    @staticmethod
    def anime_metadata(obj):
        return {'genres': Metadata.strings(obj.get('genres', [])), 'main_cast': [],
                'cast_status': 'not_requested', 'sources': [f"https://anilist.co/anime/{obj['id']}"]}

    def anime_batch(self, jobs):
        results, pending = {}, []
        for key, media in jobs:
            ids = media.get('ids', {})
            field = 'idMal' if str(ids.get('mal', '')).isdigit() else 'id'
            ident = str(ids.get('mal' if field == 'idMal' else 'anilist', ''))
            if not ident.isdigit():
                results[key] = {'genres': [], 'main_cast': [], 'cast_status': 'not_requested', 'sources': []}
                continue
            cache_key = f'anilist-genres-{field}-{ident}'
            path = self.folder / (cache_key + '.json')
            try:
                saved = json.loads(path.read_text(encoding='utf-8'))
                if time.time() - saved['saved_at'] < 30 * 86400:
                    results[key] = saved['data']
                    continue
            except (OSError, ValueError, KeyError, TypeError):
                pass
            pending.append((key, field, int(ident), cache_key))
        fields = 'id idMal genres'
        for field in ('idMal', 'id'):
            group = [x for x in pending if x[1] == field]
            for offset in range(0, len(group), 25):
                batch = group[offset:offset+25]
                self.progress(f'Fetching anime genres: batch {offset//25+1}/{(len(group)+24)//25}...')
                query = 'query($ids:[Int]) { Page(perPage:50) { media(type:ANIME,'+field+'_in:$ids) { '+fields+' } } }'
                try:
                    response = self.graphql(query, {'ids':[x[2] for x in batch]})
                    objects = response['Page']['media']
                    if not isinstance(objects, list):
                        raise ExportError('Malformed anime metadata response')
                    by_id = {obj[field]:obj for obj in objects}
                    for key, _, ident, cache_key in batch:
                        obj = by_id.get(ident)
                        if obj is None:
                            value = {'genres': [], 'main_cast': [], 'cast_status':'not_requested', 'sources':[]}
                        else:
                            value = self.anime_metadata(obj)
                        results[key] = value
                        atomic_json(self.folder/(cache_key+'.json'), {'saved_at':time.time(),'data':value})
                except (ExportError, KeyError, TypeError) as exc:
                    for key, _, _, _ in batch:
                        results[key] = {'genres':[], 'main_cast':[], 'cast_status':'error',
                                        'error':str(exc), 'sources':[]}
        return results

    def genres(self, media, category):
        ident = media.get('ids', {}).get('simkl')
        if not str(ident).isdigit():
            return []
        kind = {'shows': 'tv', 'movies': 'movies', 'anime': 'anime'}[category]
        def load():
            obj = self.simkl.call(f'/{kind}/{ident}')
            if not isinstance(obj, dict):
                raise ExportError('Malformed Simkl genre response')
            return self.strings(obj.get('genres', []))
        return self.cached(f'simkl-genres-{kind}-{ident}', load)

    def tvmaze(self, media):
        imdb = media.get('ids', {}).get('imdb', '')
        if not re.fullmatch(r'tt\d+', str(imdb)):
            return None
        def load():
            obj = self.public('https://api.tvmaze.com/lookup/shows?imdb='+imdb)
            if obj is None:
                return None
            if obj.get('externals', {}).get('imdb') != imdb or type(obj.get('id')) is not int:
                raise ExportError('TVmaze identity mismatch')
            url = f"https://api.tvmaze.com/shows/{obj['id']}/cast"
            credits = self.public(url)
            if not isinstance(credits, list):
                raise ExportError('Malformed TVmaze cast')
            cast = [{'name':c['person']['name'],'character':c.get('character',{}).get('name')}
                    for c in credits if isinstance(c.get('person',{}).get('name'), str)]
            return {'genres':self.strings(obj.get('genres',[])), 'main_cast':cast,
                    'cast_status':'available' if cast else 'no_cast_listed',
                    'cast_definition':'TVmaze primary cast', 'sources':[obj['url'],url]}
        return self.cached('tvmaze-'+imdb, load)

    def enrich(self, data):
        # Work on unique media identities, so rewatch rows reuse the same metadata.
        jobs = {}
        for category in ('shows', 'movies', 'anime'):
            for row in data[category]:
                media = row.get('movie' if category == 'movies' else 'show')
                key = (category, str(media['ids']['simkl']))
                jobs.setdefault(key, media)
        results = {}
        def get_cinema(pair):
            key, media = pair
            try:
                return key, self.cinemeta(media, key[0])
            except ExportError as exc:
                if key[0] == 'shows':
                    try:
                        fallback = self.tvmaze(media)
                        if fallback is not None:
                            return key, fallback
                    except ExportError:
                        pass
                return key, {'genres': [], 'main_cast': [], 'cast_status': 'error', 'error': str(exc), 'sources': []}
        cinema_jobs = [(k, m) for k, m in jobs.items() if k[0] != 'anime']
        with ThreadPoolExecutor(max_workers=4) as pool:
            for index, (key, value) in enumerate(pool.map(get_cinema, cinema_jobs), 1):
                results[key] = value
                if index % 25 == 0:
                    self.progress(f'Fetching cast and genres: {index}/{len(cinema_jobs)} movie/TV titles...')
        anime_jobs = [(k, m) for k, m in jobs.items() if k[0] == 'anime']
        results.update(self.anime_batch(anime_jobs))
        summary = {'titles': len(jobs), 'with_genres': 0, 'cast_titles': len(cinema_jobs), 'cast_scope': 'movies and TV; anime excluded', 'with_cast': 0, 'cast_missing': [], 'genre_missing': []}
        for key, media in jobs.items():
            value = results[key]
            if not value['genres']:
                try:
                    value['genres'] = self.genres(media, key[0])
                    value['sources'].append(f"https://simkl.com/{'tv' if key[0]=='shows' else key[0]}/{key[1]}")
                except ExportError as exc:
                    value['genre_error'] = str(exc)
            value['genre_status'] = 'available' if value['genres'] else 'not_available'
            value['attached_at'] = datetime.now(timezone.utc).isoformat()
            if value['genres']:
                summary['with_genres'] += 1
            else:
                summary['genre_missing'].append({'library': key[0], 'simkl_id': key[1], 'title': media.get('title')})
            if value['main_cast']:
                summary['with_cast'] += 1
            elif key[0] != 'anime':
                summary['cast_missing'].append({'library': key[0], 'simkl_id': key[1], 'title': media.get('title'), 'reason': value['cast_status']})
        for category in ('shows', 'movies', 'anime'):
            for row in data[category]:
                media = row.get('movie' if category == 'movies' else 'show')
                value = results[(category, str(media['ids']['simkl']))]
                media['genres'] = value['genres']
                if category != 'anime':
                    media['main_cast'] = value['main_cast']
                media['metadata_enrichment'] = {k: v for k, v in value.items() if k not in ('genres', 'main_cast')}
        data['_export']['metadata_enrichment'] = summary
        return summary
