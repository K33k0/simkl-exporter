import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import exporter
from core import atomic_json

class Tests(unittest.TestCase):
    def invoke(self,args):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
            code=exporter.main(args)
        return code,out.getvalue(),err.getvalue()

    def test_noninteractive_missing_credentials_never_launches_auth(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(exporter,'STATE',Path(td)),patch.object(exporter,'login') as login,patch.object(exporter,'Api') as api:
                with self.assertRaises(exporter.AuthenticationRequired):
                    exporter.run_export('client',lambda s:None,lambda *x:None,interactive=False)
                login.assert_not_called()
                api.return_value.call.assert_not_called()

    def test_cli_output_directory_log_and_json_stdout(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td)/'output folder'
            path=folder/'result.json'
            atomic_json(path,{'_export':{'metadata_enrichment':{'with_cast':2}}})
            log=Path(td)/'logs'/'scheduled.log'
            def run(client,progress,announce,**kwargs):
                self.assertFalse(kwargs['interactive'])
                self.assertEqual(kwargs['output_dir'],folder)
                progress('Exporting...')
                return path,{'shows':{'titles':2}}
            with patch.object(exporter,'run_export',side_effect=run),patch.object(exporter,'App') as app,patch.object(exporter.webbrowser,'open') as browser:
                code,out,err=self.invoke(['--cli','--output-dir',str(folder),'--log-file',str(log),'--quiet'])
                self.assertEqual(code,0)
                result=json.loads(out)
                self.assertTrue(result['ok'])
                self.assertEqual(result['path'],str(path.resolve()))
                self.assertEqual(err,'')
                self.assertIn('SAVED:',log.read_text())
                app.assert_not_called()
                browser.assert_not_called()

    def test_documented_failure_codes(self):
        for exc,expected in [(exporter.AuthenticationRequired('reconnect'),3),(exporter.AlreadyRunning('busy'),4),(OSError('secret details'),1)]:
            with self.subTest(expected=expected),patch.object(exporter,'run_export',side_effect=exc):
                code,out,err=self.invoke(['--cli'])
                self.assertEqual(code,expected)
                self.assertEqual(out,'')
                self.assertEqual(json.loads(err)['exit_code'],expected)
                self.assertNotIn('secret details',err)

    def test_refresh_still_works_without_interaction(self):
        with tempfile.TemporaryDirectory() as td:
            store=unittest.mock.Mock()
            store.load.return_value={'client_id':'client','access_token':'old','refresh_token':'refresh','expires_at':0}
            api=unittest.mock.Mock()
            api.call.return_value={'access_token':'new','scope':'media:read','expires_in':600}
            with patch.object(exporter,'STATE',Path(td)),patch.object(exporter,'Credentials',return_value=store),patch.object(exporter,'Api',return_value=api),patch.object(exporter,'export',return_value=('path',{})) as export,patch.object(exporter,'login') as login:
                exporter.run_export('client',lambda s:None,lambda *x:None,interactive=False,output_dir=Path(td)/'out')
                self.assertEqual(api.token,'new')
                store.save.assert_called_once()
                login.assert_not_called()
                self.assertEqual(export.call_args.kwargs['metadata_folder'],exporter.ROOT/'metadata-cache')

if __name__=='__main__': unittest.main()
