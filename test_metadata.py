import copy
import json
from pathlib import Path
import tempfile
import unittest
from core import ExportError
from metadata import Metadata

class Simkl:
    token = 'never-send-this-token'
    def call(self, path, **kwargs):
        return {'genres': ['Drama', 'Fantasy']}

class Stub(Metadata):
    def __init__(self, folder):
        super().__init__(folder, Simkl())
        self.urls = []
    def graphql(self, query, variables):
        return {'Page': {'media': [{'id':n,'idMal':n,'genres':['Adventure'],
            'characters':{'pageInfo':{'hasNextPage':False},'edges':[
                {'node':{'name':{'full':'Hero'}},'voiceActors':[{'id':2,'name':{'full':'Actor'}}]}]}}
            for n in variables['ids']]}}
    def public(self, url, jikan=False):
        self.urls.append(url)
        if jikan:
            return {'data': [
                {'role':'Main','character':{'name':'Hero','mal_id':1},
                 'voice_actors':[{'language':'Japanese','person':{'name':'Actor','mal_id':2}},
                                 {'language':'English','person':{'name':'Dub','mal_id':3}}]},
                {'role':'Supporting','character':{'name':'Side','mal_id':4},
                 'voice_actors':[{'language':'Japanese','person':{'name':'Other','mal_id':5}}]}]}
        return {'meta': {'id':url.split('/')[-1].split('.')[0], 'genres':['Action','Drama'],
                         'cast':['Actor 1','Actor 2','Actor 1']}}

def dataset():
    return {'_export':{}, 'shows':[{'user_rating':9,'memo':{'text':'keep'},
        'show':{'title':'Show','ids':{'simkl':1,'imdb':'tt123'}}}],
        'movies':[{'movie':{'title':'Movie','ids':{'simkl':2}}}],
        'anime':[{'show':{'title':'Anime','ids':{'simkl':3,'mal':'10'}}}]}

class Tests(unittest.TestCase):
    def test_all_libraries_fields_and_sources_preserve_user_data(self):
        with tempfile.TemporaryDirectory() as folder:
            provider=Stub(folder)
            d=dataset()
            summary=provider.enrich(d)
            self.assertEqual(summary['titles'],3)
            self.assertEqual(summary['with_genres'],3)
            self.assertEqual(summary['with_cast'],1)
            self.assertEqual(summary['cast_titles'],2)
            self.assertEqual(d['shows'][0]['user_rating'],9)
            self.assertEqual(d['shows'][0]['memo'],{'text':'keep'})
            self.assertEqual(d['shows'][0]['show']['main_cast'],[{'name':'Actor 1'},{'name':'Actor 2'}])
            self.assertNotIn('main_cast',d['anime'][0]['show'])
            self.assertEqual(d['anime'][0]['show']['genres'],['Adventure'])
            self.assertEqual(d['movies'][0]['movie']['metadata_enrichment']['cast_status'],'no_imdb_id')
            self.assertNotIn(Simkl.token, json.dumps(d))
            for url in provider.urls:
                self.assertNotIn('token',url)

    def test_cache_reused_no_catalog_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Stub(folder)
            p.enrich(dataset())
            n=len(p.urls)
            p.enrich(dataset())
            self.assertEqual(len(p.urls),n)

    def test_rewatch_shares_same_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            d=dataset()
            d['shows'].append(copy.deepcopy(d['shows'][0]))
            d['shows'][-1]['is_rewatch']=True
            p=Stub(folder)
            summary=p.enrich(d)
            self.assertEqual(summary['titles'],3)
            self.assertEqual(d['shows'][0]['show']['main_cast'],d['shows'][1]['show']['main_cast'])

    def test_identity_mismatch_never_attached(self):
        with tempfile.TemporaryDirectory() as folder:
            class Bad(Stub):
                def public(self,*args,**kwargs):
                    return {'meta':{'id':'ttWRONG','cast':['Wrong Actor'],'genres':['Wrong']}}
            d=dataset()
            p=Bad(folder)
            p.enrich(d)
            self.assertEqual(d['shows'][0]['show']['main_cast'],[])
            self.assertEqual(d['shows'][0]['show']['metadata_enrichment']['cast_status'],'error')

    def test_source_outage_explicit_and_not_cached(self):
        with tempfile.TemporaryDirectory() as folder:
            class Down(Stub):
                def graphql(self,*args,**kwargs):
                    raise ExportError('unavailable')
                def public(self,*args,**kwargs):
                    raise ExportError('unavailable')
            d=dataset()
            Down(folder).enrich(d)
            self.assertEqual(d['shows'][0]['show']['metadata_enrichment']['cast_status'],'error')
            self.assertFalse(any(Path(folder).glob('cinemeta*')))

if __name__=='__main__':
    unittest.main()
