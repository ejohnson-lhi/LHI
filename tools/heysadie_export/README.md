# Hey Sadie Data Export

A Python tool that authenticates to `concierge.heysadie.ai`, walks every dashboard page, intercepts all API responses, downloads call recordings, and saves everything to a date-organized local folder.

Designed to be run twice: once now to capture current state, again before cancellation to capture anything new.

## Quick start (Windows .bat files)

For Windows, use the numbered batch files in this folder. Run them in order:

| Batch file | What it does |
|---|---|
| **`setup.bat`** | One-time: creates Python venv, installs dependencies, installs Playwright browser, copies `.env.example` to `.env` (then you edit it) |
| **`1-auth.bat`** | Phase 1: opens browser, logs into Hey Sadie, saves `session.json` |
| **`2-recon.bat`** | Phase 2: visits two key pages, discovers selectors, saves `discovery.json` for review |
| **`3-extract.bat`** | Phase 3: walks every dashboard page, captures all API responses, saves rendered HTML |
| **`4-audio.bat`** | Phase 4: downloads call recordings referenced in the latest export's API captures |
| **`run-all.bat`** | Convenience: full pipeline (auth + recon + extract + audio) in one run |

**Recommended first-run sequence:**

1. Run `setup.bat` (one-time, ~3 minutes including Playwright browser download)
2. Edit `.env` with your real Hey Sadie email + password
3. Run `1-auth.bat` — verify your credentials work and a session is saved
4. Run `2-recon.bat` — open the resulting `exports\<timestamp>\discovery.json` and confirm the discovered selectors look reasonable
5. Run `3-extract.bat` — pull all the data
6. Run `4-audio.bat` — download all the audio recordings

**Subsequent runs (e.g., before cancelling Hey Sadie):**

Just `run-all.bat` — re-uses your saved session and discovery, captures everything again into a new dated folder.

## Manual setup (cross-platform)

```bash
# Create a virtual environment
python -m venv .venv
.venv\Scripts\activate         # Windows
# source .venv/bin/activate    # Mac/Linux

# Install dependencies
pip install -r requirements.txt

# Install Playwright's browser binary (one-time)
playwright install chromium

# Configure credentials
copy .env.example .env         # Windows
# cp .env.example .env         # Mac/Linux
# Then edit .env with your real Hey Sadie email/password
```

## Direct CLI usage

```bash
# Phase control flags (used by the .bat files internally)
python export.py --auth-only --headed   # Phase 1: auth, save session, exit
python export.py --recon-only           # Phase 2: auth + recon, save discovery, exit
python export.py --skip-recon --no-audio  # Phase 3: extract using existing discovery
python export.py --audio-only           # Phase 4: download audio from latest export

# Full pipeline (default behavior)
python export.py --headed               # First run: visible browser
python export.py                        # Subsequent runs: headless

# Other options
python export.py --calls-only           # Skip non-call pages
python export.py --no-audio             # Skip audio downloads (faster)
python export.py --headed --debug       # Maximum visibility for troubleshooting
python export.py --fast                 # Bypass human-pacing — DEV ONLY
python export.py --clear-session        # Force re-login
```

## Human-pacing behavior

The script intentionally throttles its actions to look like a real user, not a bot:

- **Randomized delays** (uniform random within configured ranges) between every action — no two actions happen at exactly the same interval
- **Floor on request frequency** (~0.4s minimum between deliberate actions)
- **"Reading pauses"** every ~10 calls (8–18 seconds) to mimic a human glancing through detail
- **Real Chrome User-Agent** (Playwright's default Chromium UA is recognizable as automation)
- **Standard viewport, US locale, Pacific timezone** to match a typical user profile

Default delays (configurable in `PACE_NORMAL` at the top of `export.py`):

| Action | Delay range |
|---|---|
| Between call-row clicks | 2.0–4.5 sec |
| After page navigation | 1.5–3.5 sec |
| After "Load more" click | 1.2–2.8 sec |
| After scrolling | 0.8–2.0 sec |
| Reading pause (every 10 calls) | 8–18 sec |

For 100 calls, that's roughly 8–12 minutes of total runtime in normal mode. The `--fast` flag bypasses all of this and is intended only for development; using it for a real run is more likely to trigger anti-bot detection.

## Output structure

```
exports/2026-05-02/
├── pages/              # Rendered HTML for each dashboard page
│   ├── analytics.html
│   ├── knowledge-base.html
│   ├── settings-hotel.html
│   └── ...
├── api_captures/       # All XHR/fetch JSON responses captured during the run
│   ├── 0001_GET_calls.json
│   ├── 0002_GET_call_abc123.json
│   └── ...
├── api_index.json      # Index of all captured API responses
├── calls/
│   ├── calls_list.html
│   └── audio/
│       ├── call_abc123.mp3
│       └── ...
├── nav_links.json      # Discovered navigation links from the dashboard
└── manifest.json       # Summary: what was extracted, when, errors
```

## Three-phase design

1. **Authentication** — Clerk login, save session for reuse.

2. **Reconnaissance** — politely visit `/admin/settings` and `/admin/analytics`, run all selector discovery inside a single `page.evaluate()` call per page. The JavaScript runs in one round-trip, identifies the working selector pattern from a candidate list, and returns the result. This is the only "try multiple patterns" step, and it happens entirely client-side in one request.
   - Output: `discovery.json` — the selectors and labels we discovered, for human review.

3. **Bulk extraction** — uses the discovered selectors deterministically. No fallbacks, no brute-force trying. Pagination type, call-row selector, settings tab selector pattern and labels are all known up front.

Why this matters: trying selector after selector at the action layer (click, fail, click, fail, click, succeed) is a fingerprint of bot behavior and unkind to the host. Doing the discovery once in JavaScript looks like a single normal page load, then subsequent actions look intentional.

A separate post-processing step (or manual review) turns the raw captures into structured exports if needed. By capturing everything raw, we don't need to know Hey Sadie's exact API structure ahead of time, and we don't lose data if the UI changes.

## Notes

**Terms of Service**: Automated access to a SaaS dashboard may be restricted by Hey Sadie's terms. This tool is for retrieving your own data before service cancellation. Review the relevant terms if in doubt.

**Credential security**: `.env` is plaintext on disk. Make sure your local machine has appropriate access controls. The `.env` file is gitignored.

**Authentication**: Hey Sadie uses Clerk for auth. The tool attempts automated email + password login. If 2FA, captcha, or another challenge is presented, run with `--headed` and complete it manually; the session will be saved for subsequent automated runs.

**Page URL list**: verified by grepping the user's previously-saved HTML files for `/admin/...` href patterns. The actual confirmed routes are:

- `/admin/analytics`
- `/admin/knowledge-base` and `/admin/knowledge-base/draft`
- `/admin/unanswered-questions` (this is what the "Knowledge Base Gaps" page is actually called)
- `/admin/transfer-reasons`
- `/admin/sms-reasons`
- `/admin/sadie-chat`
- `/admin/users`
- `/admin/support-tickets`
- `/admin/settings` (single URL with in-page tabs — sub-tabs do NOT have separate URLs)

The Settings page is a tab interface — Hotel, Assistant Settings, Call Analytics Categories, Daily Report Recipients, Pause Sadie, Sadie Chat, Schedule After Hours, and Transfer Reason Scheduling all live at `/admin/settings`. The script handles this with a separate `extract_settings_tabs()` function that clicks each tab and saves the resulting view as `settings-<tab-slug>.html`.

The script also saves a runtime-discovered nav link list to `nav_links.json` as a sanity check — if Hey Sadie ever adds new admin pages, they'll show up there for us to add to `DASHBOARD_PAGES`.

## Troubleshooting

- **Login fails silently**: run with `--headed --debug` and watch what happens. Most likely the Clerk selectors have changed.
- **Calls page is empty after extraction**: check `nav_links.json` — the URL may differ from `/admin/analytics`. Update `DASHBOARD_PAGES` in `export.py`.
- **Audio downloads fail**: the audio URLs may be presigned/expiring or require specific headers. Check the captured API responses for the exact URL format.
- **Session expired**: delete `session.json` and re-run with `--headed`.
