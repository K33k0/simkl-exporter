import argparse
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
import webbrowser
from core import Api, ApiError, ExportError, Credentials, checked_tokens, login, export

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'private'
CLIENT_ID = os.environ.get('SIMKL_CLIENT_ID', '').strip()

class AuthenticationRequired(ExportError):
    pass

class AlreadyRunning(ExportError):
    pass

def run_export(client_id, progress, announce, *, interactive=True, output_dir=None):
    import msvcrt
    STATE.mkdir(exist_ok=True)
    with open(STATE / 'run.lock', 'a+b') as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise AlreadyRunning('Another export is already running.') from None
        store = Credentials(STATE)
        tokens = store.load()
        client_id = client_id or tokens.get('client_id', '')
        if not client_id:
            raise AuthenticationRequired('Provide --client-id or SIMKL_CLIENT_ID for first-time setup.')
        if tokens.get('client_id') != client_id:
            tokens = {}
        api = Api(client_id)
        if tokens.get('refresh_token') and tokens.get('expires_at', 0) <= time.time() + 120:
            progress('Refreshing account connection...')
            try:
                fresh = checked_tokens(api.call('/oauth2/token', body={
                    'client_id': client_id, 'grant_type': 'refresh_token',
                    'refresh_token': tokens['refresh_token']}))
                tokens = {**tokens, **fresh, 'expires_at': time.time() + fresh.get('expires_in', 0)}
                store.save(tokens)
            except ApiError as exc:
                if exc.code not in ('invalid_grant', 'invalid_token'):
                    raise
                tokens = {}
        if not tokens.get('access_token'):
            if not interactive:
                raise AuthenticationRequired('Account approval required. Run exporter.py --login once as this Windows user.')
            fresh = login(api, announce)
            tokens = {**fresh, 'client_id': client_id,
                      'expires_at': time.time() + fresh.get('expires_in', 0)}
            store.save(tokens)
        api.token = tokens['access_token']
        try:
            return export(api, Path(output_dir) if output_dir else ROOT / 'exports', progress, metadata_folder=ROOT / 'metadata-cache')
        except ApiError as exc:
            if exc.status != 401:
                raise
            # Revoked access: don't leave a permanently failing cached token.
            store.save({'client_id': client_id})
            raise AuthenticationRequired('Simkl connection expired or was revoked. Run exporter.py --login to reconnect.') from None

class App:
    def __init__(self):
        import tkinter as tk
        from tkinter import ttk
        self.root = tk.Tk()
        self.root.title('Simkl JSON Exporter')
        self.root.geometry('700x465')
        self.root.minsize(640, 430)
        self.events = queue.Queue()
        self.busy = False
        self.status = tk.StringVar(value='Ready to export your complete library.')
        self.code = tk.StringVar()
        panel = ttk.Frame(self.root, padding=24)
        panel.pack(fill='both', expand=True)
        ttk.Label(panel, text='Simkl JSON Exporter', font=('Segoe UI', 21, 'bold')).pack(anchor='w')
        ttk.Label(panel, text='Movies | TV | Anime | Ratings | Notes | Cast | Genres', font=('Segoe UI', 11)).pack(anchor='w', pady=(6, 18))
        ttk.Label(panel, text='One-time setup: AUTH V2 client ID. No client secret required.').pack(anchor='w')
        saved_id = Credentials(STATE).load().get('client_id', '')
        self.client = tk.StringVar(value=CLIENT_ID or saved_id)
        self.entry = ttk.Entry(panel, textvariable=self.client)
        self.entry.pack(fill='x', pady=8)
        link = ttk.Label(panel, text='Simkl developer settings: TV, devices & command line', foreground='#1466a5', cursor='hand2')
        link.pack(anchor='w')
        link.bind('<Button-1>', lambda e: webbrowser.open('https://simkl.com/settings/developer/'))
        self.button = ttk.Button(panel, text='Export JSON', command=self.start)
        self.button.pack(anchor='w', pady=(20, 12))
        self.bar = ttk.Progressbar(panel, mode='indeterminate')
        self.bar.pack(fill='x')
        ttk.Label(panel, textvariable=self.status, wraplength=640).pack(anchor='w', pady=(12, 5))
        ttk.Label(panel, textvariable=self.code, font=('Segoe UI', 13, 'bold')).pack(anchor='w')
        ttk.Button(panel, text='Open exports folder', command=self.open_folder).pack(anchor='w', pady=10)
        ttk.Label(panel, text='Read-only access. Tokens encrypted for your Windows account.').pack(anchor='w', side='bottom')
        self.root.after(150, self.drain)
        self.root.after(500, self.start)

    def open_folder(self):
        folder = ROOT / 'exports'
        folder.mkdir(exist_ok=True)
        os.startfile(folder)

    def start(self):
        if self.busy:
            return
        client_id = self.client.get().strip()
        if not client_id or any(c.isspace() for c in client_id):
            self.status.set('Enter your AUTH V2 client ID.')
            return
        self.busy = True
        self.entry.configure(state='disabled')
        self.button.configure(state='disabled')
        self.code.set('')
        self.bar.start()
        threading.Thread(target=self.worker, args=(client_id,), daemon=True).start()

    def worker(self, client_id):
        try:
            result = run_export(client_id,
                lambda text: self.events.put(('status', text)),
                lambda code, url: self.events.put(('approval', (code, url))))
            self.events.put(('done', result))
        except Exception as exc:
            message = str(exc) if isinstance(exc, ExportError) else f'Local failure ({type(exc).__name__}). No complete export published.'
            self.events.put(('error', message))

    def drain(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'status':
                    self.status.set(value)
                elif kind == 'approval':
                    code, url = value
                    self.status.set('Approve read-only access in your browser. Export starts automatically afterwards.')
                    self.code.set('Approval code: ' + code)
                    webbrowser.open(url)
                elif kind in ('done', 'error'):
                    self.busy = False
                    self.bar.stop()
                    self.entry.configure(state='normal')
                    self.button.configure(state='normal', text='Export JSON again')
                    self.code.set('')
                    if kind == 'done':
                        path, summary = value
                        count = sum(v['titles'] for v in summary.values())
                        details = json.loads(path.read_text(encoding='utf-8'))['_export'].get('metadata_enrichment', {})
                        coverage = f"Cast: {details.get('with_cast', 0):,}/{details.get('cast_titles', count):,}; genres: {details.get('with_genres', 0):,}/{count:,}."
                        self.status.set(f'Complete: {count:,} titles. {coverage}\nSaved to exports/{path.name}')
                    else:
                        self.status.set(value)
        except queue.Empty:
            pass
        self.root.after(150, self.drain)

    def run(self):
        self.root.mainloop()

def main(argv=None):
    parser = argparse.ArgumentParser(description='Export Simkl libraries, ratings, notes, cast and genres to JSON.')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--cli', action='store_true', help='Non-interactive export; never opens a browser or GUI.')
    mode.add_argument('--login', '--console', dest='login', action='store_true', help='Interactive browser approval if needed, then export.')
    parser.add_argument('--client-id', default=CLIENT_ID, help='Simkl AUTH V2 app client ID (or SIMKL_CLIENT_ID environment variable). Saved connection is reused if omitted.')
    parser.add_argument('--output-dir', type=Path, help='Export folder (default: exports beside this script).')
    parser.add_argument('--log-file', type=Path, help='Append timestamped progress and errors to this UTF-8 log.')
    parser.add_argument('--quiet', action='store_true', help='Suppress stderr progress; final JSON result is still printed.')
    args = parser.parse_args(argv)
    if not (args.cli or args.login):
        if args.output_dir or args.log_file or args.quiet:
            parser.error('Use --cli or --login with command-line options.')
        App().run()
        return 0
    log = None
    try:
        if args.log_file:
            args.log_file.parent.mkdir(parents=True, exist_ok=True)
            log = args.log_file.open('a', encoding='utf-8')
        def progress(text):
            if log:
                from datetime import datetime
                log.write(datetime.now().astimezone().isoformat() + ' ' + text + '\n')
                log.flush()
            if not args.quiet:
                print(text, file=sys.stderr, flush=True)
        def announce(code, url):
            if not args.login:
                raise AuthenticationRequired('Account approval required. Run --login first.')
            # Approval URL/code go to stderr, never to the scheduled task log.
            print('Approve read-only access: ' + url, file=sys.stderr, flush=True)
            webbrowser.open(url)
        path, summary = run_export(args.client_id, progress, announce,
            interactive=args.login, output_dir=args.output_dir)
        details = json.loads(path.read_text(encoding='utf-8'))['_export'].get('metadata_enrichment', {})
        result = {'ok': True, 'path': str(path.resolve()), 'summary': summary,
                  'metadata': {k: v for k, v in details.items() if k not in ('cast_missing', 'genre_missing')}}
        progress('SAVED: ' + str(path.resolve()))
        print(json.dumps(result, ensure_ascii=True), flush=True)
        return 0
    except Exception as exc:
        code = 3 if isinstance(exc, AuthenticationRequired) else 4 if isinstance(exc, AlreadyRunning) else 1
        message = str(exc) if isinstance(exc, ExportError) else f'Local export failure ({type(exc).__name__}).'
        if log:
            log.write('ERROR: ' + message + '\n')
            log.flush()
        print(json.dumps({'ok': False, 'error': message, 'exit_code': code}), file=sys.stderr, flush=True)
        return code
    finally:
        if log:
            log.close()

if __name__ == '__main__':
    sys.exit(main())
