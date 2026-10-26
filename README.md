# Simkl Exporter

Windows GUI and CLI exporter for all Simkl libraries and statuses, with personal ratings, memos, episode history, genre tags, and movie/TV main cast. Anime actors are excluded. Uses the official read-only API without the VIP backup tool.

## Setup

Requires Windows and Python 3.11+ with Tkinter for the GUI. No extra packages.

Create an AUTH V2 app at https://simkl.com/settings/developer/ using **TV, devices & command line**. Connect once:

```
python exporter.py --login --client-id YOUR_CLIENT_ID
```

Approve read-only access in your browser. Subsequent runs reuse saved encrypted credentials and the client ID. Alternatively set the SIMKL_CLIENT_ID environment variable.

For the GUI run `python exporter.py` or **Export Simkl.cmd**, enter your client ID, and approve. Each later launch automatically exports.

## CLI and scheduling

```
python exporter.py --cli --output-dir exports --log-file logs/scheduled.log --quiet
```

**Export Simkl CLI.cmd** supplies --cli and forwards other arguments. The unattended CLI never opens a browser or GUI. Tokens refresh automatically. The final JSON result goes to stdout and errors to stderr. --login is interactive; --console remains an alias.

Exit codes: 0 success, 1 export/network/local failure, 2 invalid arguments, 3 approval required, 4 another export is running.

For Windows Task Scheduler, set Program to the absolute path of python.exe and Arguments to the absolute quoted path of exporter.py followed by --cli --quiet --log-file and a quoted log path. Use an absolute --output-dir if desired. Choose your trigger and run under the same Windows user that approved Simkl. DPAPI credentials cannot be read by SYSTEM or another account. For signed-out execution use a normal password-backed logon. Disable overlapping instances; the tool also has a lock. Use python.exe rather than pythonw.exe for CLI output and exit codes.

## Privacy and metadata

Private credentials, exports, logs, catalog cache and Python caches are ignored by Git. No account tokens or personal exports are shipped. Credentials are encrypted with Windows-user-bound DPAPI.

Simkl API memo text is capped at 140 characters. Custom-list and rewatch-session access require PRO/VIP; core libraries work with free accounts. Public reviews/comments are excluded. Bulk-completed episodes can have synthesized timestamps; large responses are split by status and can fall back to originally recorded episode rows. Successful JSON is written atomically.

Sources: [Simkl](https://simkl.com/), [Cinemeta](https://v3-cinemeta.strem.io/manifest.json), [TVmaze](https://www.tvmaze.com/api), and [AniList](https://docs.anilist.co/). TVmaze data is licensed under [CC BY-SA](https://www.tvmaze.com/api#licensing). Only public title IDs go to catalog services, never Simkl credentials. Metadata is cached for 30 days. Unavailable genres/cast are empty arrays with explicit source/error states and coverage totals. Anime has genres but no cast field.

Original fields remain in shows, anime and movies. Media objects gain genres and movie/TV main_cast. Activity consistency checks detect library changes. All user library data is refreshed on each export.

## Tests

```
python -m unittest discover -s . -p "test_*.py" -v
```

Tests use local fixtures/mocks and Windows DPAPI roundtrips, without live account access.
