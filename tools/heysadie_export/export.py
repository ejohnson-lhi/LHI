"""
Hey Sadie data export tool.

Authenticates to concierge.heysadie.ai (Clerk-based auth), walks every
dashboard page, intercepts all JSON API responses, downloads audio
recordings, and saves everything to a date-organized local folder.

See README.md for setup and usage.
"""

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, BrowserContext, Page, Response

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL = "https://concierge.heysadie.ai"
SESSION_FILE = Path(__file__).parent / "session.json"
EXPORTS_ROOT = Path(__file__).parent / "exports"

# A normal-looking Chrome User-Agent (Playwright's default Chromium UA is
# recognizable as automation). Update occasionally if it gets stale.
CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/132.0.0.0 Safari/537.36"
)

# Human-pacing settings. All times in seconds.
# Each value is a (min, max) range — actual delay is uniformly random in that range.
PACE_NORMAL = {
    "between_calls": (2.0, 4.5),         # Between clicking different call rows
    "after_navigation": (1.5, 3.5),      # After page.goto completes
    "after_load_more": (1.2, 2.8),       # After clicking a Load-more button
    "after_scroll": (0.8, 2.0),          # After scrolling to bottom
    "after_action": (0.5, 1.5),          # Generic small delay between any two actions
    "long_pause_every_n": 10,            # Insert a long pause every N call clicks
    "long_pause": (8.0, 18.0),           # Duration of "reading" pause
    "min_request_interval": 0.4,         # Hard floor on time between deliberate actions
}

# Faster mode for development/iteration (less polite — don't use for real runs)
PACE_FAST = {
    "between_calls": (0.3, 0.6),
    "after_navigation": (0.5, 1.0),
    "after_load_more": (0.4, 0.8),
    "after_scroll": (0.3, 0.6),
    "after_action": (0.1, 0.3),
    "long_pause_every_n": 50,
    "long_pause": (1.0, 2.0),
    "min_request_interval": 0.0,
}

# Active pace — set in main() based on --fast flag
PACE = PACE_NORMAL
_last_action_at = [0.0]  # mutable singleton for tracking


def human_pause(category: str = "after_action"):
    """Sleep for a random duration in the configured range for this category.

    Also enforces PACE['min_request_interval'] as a hard floor between actions.
    Categories: between_calls, after_navigation, after_load_more, after_scroll,
                after_action, long_pause.
    """
    lo, hi = PACE.get(category, (0.5, 1.5))
    delay = random.uniform(lo, hi)

    # Enforce minimum interval since the last action
    elapsed = time.monotonic() - _last_action_at[0]
    floor = PACE.get("min_request_interval", 0.0)
    if elapsed < floor:
        delay = max(delay, floor - elapsed)

    time.sleep(delay)
    _last_action_at[0] = time.monotonic()

# Admin pages to visit. URLs verified by grepping the user's previously
# saved HTML files for href patterns — see the Confirmed Admin URLs section
# in README.md for the discovery rationale.
#
# Important: /admin/settings is a single page with TABS for the various
# settings categories. The sub-tabs (Hotel, Assistant, etc.) do not have
# separate URLs — switching tabs is in-page state. The settings tab
# extraction is handled separately by extract_settings_tabs().
DASHBOARD_PAGES = [
    ("analytics", "/admin/analytics"),
    ("knowledge-base", "/admin/knowledge-base"),
    ("knowledge-base-draft", "/admin/knowledge-base/draft"),
    ("unanswered-questions", "/admin/unanswered-questions"),
    ("transfer-reasons", "/admin/transfer-reasons"),
    ("sms-reasons", "/admin/sms-reasons"),
    ("sadie-chat", "/admin/sadie-chat"),
    ("users", "/admin/users"),
    ("support-tickets", "/admin/support-tickets"),
    ("settings", "/admin/settings"),  # Settings tabs handled by extract_settings_tabs
]

# Note: settings tab labels are NOT hardcoded — they're discovered at runtime
# by discover_selectors(). The labels found in the user's saved HTML files
# (Hotel, Assistant Settings, Call Analytics Categories, Daily Report
# Recipients, Pause Sadie, Sadie Chat, Schedule After Hours, Transfer Reason
# Scheduling) are listed here only as a hint of what we expect to find.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("heysadie-export")


# ---------------------------------------------------------------------------
# API response capture
# ---------------------------------------------------------------------------

class APICapture:
    """Intercepts XHR/fetch responses and saves JSON ones to disk."""

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.responses = []  # index entries
        self.counter = 0

    def attach(self, context: BrowserContext):
        context.on("response", self._on_response)

    def _on_response(self, response: Response):
        url = response.url
        ct = response.headers.get("content-type", "").lower()

        # Only care about JSON/text responses, not assets
        is_json = "application/json" in ct or "text/json" in ct
        looks_like_api = "/api/" in url or "/trpc/" in url or url.endswith(".json")
        if not (is_json or looks_like_api):
            return

        try:
            self.counter += 1
            body_text = response.text()
            try:
                body = json.loads(body_text)
                ext = "json"
                content = json.dumps(body, indent=2, ensure_ascii=False)
            except json.JSONDecodeError:
                content = body_text
                ext = "txt"

            # Build a readable filename
            parsed = urlparse(url)
            url_tail = (parsed.path.rstrip("/").split("/")[-1] or "root")[:60]
            url_tail = re.sub(r"[^a-zA-Z0-9._-]", "_", url_tail)
            filename = f"{self.counter:04d}_{response.request.method}_{url_tail}.{ext}"
            filepath = self.output_dir / filename
            filepath.write_text(content, encoding="utf-8")

            self.responses.append({
                "seq": self.counter,
                "url": url,
                "method": response.request.method,
                "status": response.status,
                "content_type": ct,
                "filepath": str(filepath.relative_to(self.output_dir.parent)),
            })
        except Exception as e:
            log.warning(f"Failed to capture response from {url}: {e}")

    def save_index(self, filepath: Path):
        with filepath.open("w", encoding="utf-8") as f:
            json.dump(self.responses, f, indent=2)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def authenticate(context: BrowserContext, base_url: str, email: str, password: str, headed: bool):
    """Log in via Clerk. Falls back to interactive completion if automation fails."""
    page = context.new_page()
    log.info(f"Navigating to {base_url}")
    page.goto(base_url, wait_until="domcontentloaded", timeout=30000)

    # If saved session worked, we may already be on /admin/...
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass

    if "/admin" in page.url:
        log.info("Already authenticated via saved session")
        page.close()
        return True

    log.info("Not authenticated. Attempting Clerk email + password login.")

    # Wait for an email field; Clerk uses 'identifier' as the input name
    email_selector = 'input[name="identifier"], input[type="email"]'
    try:
        page.wait_for_selector(email_selector, timeout=15000)
    except Exception:
        log.warning("Email field didn't appear. Saving auth_debug.png for inspection.")
        try:
            page.screenshot(path="auth_debug.png")
        except Exception:
            pass

    # Fill email
    try:
        page.locator(email_selector).first.fill(email)
    except Exception as e:
        log.error(f"Could not fill email field: {e}")
        return _wait_for_manual_auth(page, headed)

    # Click continue / next
    _click_continue(page)

    # Wait for password
    pw_selector = 'input[name="password"], input[type="password"]'
    try:
        page.wait_for_selector(pw_selector, timeout=10000)
        page.locator(pw_selector).first.fill(password)
        _click_continue(page)
    except Exception as e:
        log.warning(f"Password step didn't proceed automatically: {e}")
        return _wait_for_manual_auth(page, headed)

    # Wait to land on /admin
    try:
        page.wait_for_url("**/admin/**", timeout=30000)
        log.info("Login successful")
        return True
    except Exception:
        log.warning("Did not reach /admin within 30s after submitting password")
        return _wait_for_manual_auth(page, headed)


def _click_continue(page: Page):
    """Click a 'Continue' / 'Sign in' / 'Next' button."""
    candidates = [
        'button:has-text("Continue")',
        'button:has-text("Sign in")',
        'button:has-text("Next")',
        'button[type="submit"]',
    ]
    for sel in candidates:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=2000):
                btn.click()
                return
        except Exception:
            continue


def _wait_for_manual_auth(page: Page, headed: bool):
    """Pause for the user to complete auth manually if browser is visible."""
    if not headed:
        log.error("Auth requires manual intervention. Re-run with --headed.")
        return False
    log.info("Browser window is open — please complete authentication manually.")
    log.info("Waiting up to 5 minutes for /admin to load...")
    try:
        page.wait_for_url("**/admin/**", timeout=300000)
        log.info("Manual auth completed")
        return True
    except Exception:
        log.error("Timed out waiting for manual auth")
        return False


# ---------------------------------------------------------------------------
# Page extraction
# ---------------------------------------------------------------------------

def discover_selectors(page: Page, base_url: str, output_dir: Path, capture: "APICapture | None" = None) -> dict:
    """One-pass DOM reconnaissance.

    Visits the settings and analytics pages (with normal pacing) and runs all
    structure-discovery in single page.evaluate() calls — JavaScript-side,
    no per-selector network back-and-forth. The result is a config dict
    containing the actual selectors that work for THIS Hey Sadie instance.

    If `capture` is provided, also extracts the /api/calls URL template from
    captured responses so extract_calls can hit the API directly instead of
    scraping the DOM.

    Saves discovery.json for human review.
    """
    log.info("Reconnaissance: discovering selectors for this Hey Sadie instance")
    discoveries = {
        "discovered_at": datetime.now().isoformat(),
        "settings": None,
        "analytics": None,
    }

    # Settings tabs
    settings_url = urljoin(base_url, "/admin/settings")
    log.info(f"  Visiting {settings_url} for tab discovery")
    page.goto(settings_url, wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    human_pause("after_navigation")

    discoveries["settings"] = page.evaluate(
        """async () => {
            // Scope tab discovery to the MAIN content area only — never the
            // global navigation in <aside> (which is the same on every page
            // and would falsely match here).
            const root = document.querySelector('main, [role="main"]') || document.body;
            const sleep = (ms) => new Promise(r => setTimeout(r, ms));

            // Helper: an element is "navigation away" if it's a link to a
            // different /admin path.
            const isNavLink = (el) => {
                if (el.tagName !== 'A') return false;
                const href = el.getAttribute('href') || '';
                return href.startsWith('/admin/') || href.startsWith('http');
            };

            // Patterns ordered most-specific (Hey Sadie's actual tab markup) first.
            const patterns = [
                { sel: 'button.border-b-2', name: 'hey-sadie-tab-button' },
                { sel: '[role="tab"]', name: 'aria-role-tab' },
                { sel: '[role="tablist"] button', name: 'tablist-button' },
                { sel: 'button[aria-selected]', name: 'aria-selected-button' },
                { sel: 'button', name: 'main-button' },
            ];

            let chosen = null;
            for (const p of patterns) {
                const els = Array.from(root.querySelectorAll(p.sel)).filter(el => !isNavLink(el));
                if (els.length >= 3) {
                    chosen = p;
                    break;
                }
            }
            if (!chosen) {
                return { selector_pattern: null, method: 'none', tab_labels: [], scroll_right_aria_label: null };
            }

            const collect = () => Array.from(root.querySelectorAll(chosen.sel))
                .filter(el => !isNavLink(el))
                .map(el => (el.textContent || '').trim())
                .filter(t => t.length > 0 && t.length < 80);

            // The Hey Sadie settings tab strip is a horizontal scroller — only
            // 5 tabs render at once, with arrow buttons to reveal more.
            // Find a right-arrow button (chevron icon, ">"-style text, or
            // "scroll right" / "next" aria-label) and click it repeatedly
            // until no new tabs appear.
            const findScrollRight = () => {
                for (const btn of root.querySelectorAll('button')) {
                    const text = (btn.textContent || '').trim();
                    const aria = (btn.getAttribute('aria-label') || '').toLowerCase();
                    const innerHTML = btn.innerHTML.toLowerCase();
                    if (/^[\\u203a>\\u2192]$/.test(text)) return btn;  // ›, >, →
                    if (/next|scroll[\\s-]*right|right[\\s-]*arrow/.test(aria)) return btn;
                    if (innerHTML.includes('chevron-right') || innerHTML.includes('arrow-right')) return btn;
                    // Lucide / Radix often use lucide-chevron-right class
                    if (btn.querySelector('svg.lucide-chevron-right, [data-icon="chevron-right"]')) return btn;
                }
                return null;
            };

            const seen = new Set();
            const allTabs = [];
            for (const t of collect()) {
                if (!seen.has(t)) { seen.add(t); allTabs.push(t); }
            }

            let scrollArrow = findScrollRight();
            let scrollAria = scrollArrow ? (scrollArrow.getAttribute('aria-label') || '') : null;

            // Click the right-arrow up to 8 times (more than enough for any reasonable
            // tab count). Stop early when no new tabs appear after a click.
            for (let i = 0; i < 8 && scrollArrow; i++) {
                try { scrollArrow.click(); } catch (e) { break; }
                await sleep(400);
                const beforeCount = allTabs.length;
                for (const t of collect()) {
                    if (!seen.has(t)) { seen.add(t); allTabs.push(t); }
                }
                if (allTabs.length === beforeCount) break;
                scrollArrow = findScrollRight();  // refresh in case DOM changed
            }

            return {
                selector_pattern: chosen.sel,
                method: chosen.name,
                tab_labels: allTabs,
                scroll_right_aria_label: scrollAria,
            };
        }"""
    )
    log.info(
        f"    Settings tabs: pattern='{discoveries['settings']['selector_pattern']}' "
        f"({discoveries['settings']['method']}), {len(discoveries['settings']['tab_labels'])} tabs found"
    )

    # Analytics: call row selector, load-more button, pagination style
    analytics_url = urljoin(base_url, "/admin/analytics")
    log.info(f"  Visiting {analytics_url} for call-list discovery")
    page.goto(analytics_url, wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    human_pause("after_navigation")

    discoveries["analytics"] = page.evaluate(
        """() => {
            const result = {};

            // Find call rows by looking for the most-repeated clickable element
            // type that contains a phone-number-like pattern.
            const phoneRegex = /\\(\\d{3}\\)\\s*\\d{3}[-\\s]?\\d{4}|\\+?1?[-\\s.]?\\d{3}[-\\s.]?\\d{3}[-\\s.]?\\d{4}/;

            const allWithPhones = [];
            for (const el of document.querySelectorAll('*')) {
                if (el.children.length > 40) continue;  // skip large containers
                const text = el.textContent || '';
                if (text.length > 0 && text.length < 1000 && phoneRegex.test(text)) {
                    allWithPhones.push(el);
                }
            }

            // Walk each phone-containing element up to find a clickable ancestor
            const sigCounts = {};
            const sigExamples = {};
            for (const el of allWithPhones) {
                let target = el;
                for (let i = 0; i < 6 && target; i++) {
                    const role = target.getAttribute && target.getAttribute('role');
                    const tag = target.tagName;
                    const isClickable =
                        tag === 'BUTTON' || tag === 'A' ||
                        role === 'button' || role === 'row' || role === 'link' ||
                        (target.getAttribute && target.getAttribute('onclick'));
                    if (isClickable) break;
                    target = target.parentElement;
                }
                if (!target) continue;

                // Build a stable signature for this element
                const tag = target.tagName.toLowerCase();
                const role = target.getAttribute && target.getAttribute('role');
                const sig = role ? `${tag}[role="${role}"]` : tag;
                sigCounts[sig] = (sigCounts[sig] || 0) + 1;
                if (!sigExamples[sig]) {
                    sigExamples[sig] = (target.outerHTML || '').slice(0, 200);
                }
            }

            const ranked = Object.entries(sigCounts).sort((a, b) => b[1] - a[1]);
            if (ranked.length > 0) {
                result.call_row_selector = ranked[0][0];
                result.call_row_count_at_discovery = ranked[0][1];
                result.call_row_example = sigExamples[ranked[0][0]];
            } else {
                result.call_row_selector = null;
                result.call_row_count_at_discovery = 0;
            }

            // Look for a load-more button
            for (const btn of document.querySelectorAll('button')) {
                const text = (btn.textContent || '').trim();
                if (/^(load|show)\\s+(more|older)/i.test(text)) {
                    result.load_more_text = text;
                    break;
                }
            }

            // Detect pagination type
            if (result.load_more_text) {
                result.pagination_type = 'load-more-button';
            } else if (document.querySelector('nav[aria-label*="agination" i], [role="navigation"][aria-label*="age" i]')) {
                result.pagination_type = 'numbered-pagination';
            } else {
                result.pagination_type = 'infinite-scroll';
            }

            // Look for date-range widening control
            for (const btn of document.querySelectorAll('button, [role="button"]')) {
                const text = (btn.textContent || '').trim();
                if (/^(all\\s+time|last\\s+year|max(imum)?\\s+range)$/i.test(text)) {
                    result.date_widener_text = text;
                    break;
                }
            }

            return result;
        }"""
    )
    log.info(
        f"    Analytics: call-row selector='{discoveries['analytics'].get('call_row_selector')}' "
        f"(found {discoveries['analytics'].get('call_row_count_at_discovery', 0)} at discovery time), "
        f"pagination={discoveries['analytics'].get('pagination_type')}"
    )

    # Extract the /api/calls URL template from captured responses.
    # This is the high-value finding: with a known API endpoint we can
    # paginate calls directly via offset/limit, no DOM scraping needed.
    if capture is not None:
        api_template = _find_calls_api_url(capture)
        if api_template:
            discoveries["calls_api"] = api_template
            log.info(
                f"    Calls API: {api_template['endpoint']} "
                f"(orgId={api_template.get('organization_id', '?')[:24]}…)"
            )
        else:
            log.warning("    Couldn't find /api/calls URL in captured responses")

    # Save discovery for human review and next-run reuse
    out = output_dir / "discovery.json"
    out.write_text(json.dumps(discoveries, indent=2), encoding="utf-8")
    log.info(f"  Saved {out}")

    return discoveries


def _find_calls_api_url(capture: "APICapture") -> dict | None:
    """Scan captured responses for a /api/calls request and extract the URL template."""
    from urllib.parse import parse_qs

    for entry in capture.responses:
        url = entry.get("url", "")
        if "/api/calls" not in url:
            continue
        # Skip per-call detail endpoints (those have a call ID after /calls/)
        if re.search(r"/api/calls/[\w-]+", url):
            continue
        parsed = urlparse(url)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        return {
            "endpoint": f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
            "default_params": params,
            "organization_id": params.get("organizationId"),
            "captured_at_seq": entry.get("seq"),
        }
    return None


def discover_nav_links(page: Page, output_dir: Path):
    """Scrape the nav menu after auth to discover real admin URLs."""
    log.info("Discovering navigation links from current page")
    try:
        links = page.evaluate(
            """() => Array.from(document.querySelectorAll('a[href]'))
                .map(a => ({ href: a.getAttribute('href'), text: a.textContent.trim() }))
                .filter(l => l.href && l.href.startsWith('/admin'))
                .filter((v, i, a) => a.findIndex(x => x.href === v.href) === i)"""
        )
        out = output_dir / "nav_links.json"
        out.write_text(json.dumps(links, indent=2), encoding="utf-8")
        log.info(f"  Found {len(links)} admin links → {out.name}")
        return links
    except Exception as e:
        log.warning(f"Failed to discover nav links: {e}")
        return []


def extract_page(page: Page, name: str, path: str, base_url: str, output_dir: Path):
    """Visit a page, wait for it to load, save the rendered HTML."""
    url = urljoin(base_url, path)
    log.info(f"Extracting page: {name} → {url}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        human_pause("after_navigation")

        html = page.content()
        out_file = output_dir / f"{name}.html"
        out_file.write_text(html, encoding="utf-8")
        log.info(f"  Saved {out_file.name} ({len(html):,} bytes)")
        return True
    except Exception as e:
        log.error(f"  Failed to extract {name}: {e}")
        return False


def extract_calls(page: Page, base_url: str, output_dir: Path, discoveries: dict):
    """Extract calls using the discovered /api/calls endpoint.

    Recon found the API URL pattern Hey Sadie's UI uses. We replicate those
    requests directly with a wide date range and offset-based pagination.
    Cleaner than DOM clicking, looks like normal app traffic.

    Falls back to DOM-based extraction only if the calls API wasn't discovered.
    """
    calls_dir = output_dir / "calls"
    calls_dir.mkdir(parents=True, exist_ok=True)
    (calls_dir / "audio").mkdir(parents=True, exist_ok=True)

    calls_api = (discoveries or {}).get("calls_api")
    if calls_api and calls_api.get("endpoint"):
        _extract_calls_via_api(page, calls_api, calls_dir)
    else:
        log.warning("Calls API URL not discovered; falling back to DOM-based extraction")
        _extract_calls_via_dom(page, base_url, calls_dir, discoveries)


def _extract_calls_via_api(page: Page, calls_api: dict, calls_dir: Path):
    """Paginate through /api/calls with a wide date range, save each response."""
    from urllib.parse import urlencode

    endpoint = calls_api["endpoint"]
    base_params = dict(calls_api.get("default_params") or {})

    # Widen the date range. Default to 5 years back; user can adjust if needed.
    today = datetime.now().date()
    five_years_ago = today.replace(year=today.year - 5)
    base_params["fromDate"] = five_years_ago.isoformat()
    base_params["toDate"] = today.isoformat()

    page_size = 100
    base_params["limit"] = str(page_size)

    log.info(f"Fetching calls via API: {endpoint}")
    log.info(f"  Date range: {base_params['fromDate']} to {base_params['toDate']}")
    log.info(f"  Page size: {page_size}")

    all_calls = []
    offset = 0
    page_idx = 0
    api_request = page.context.request

    while True:
        page_idx += 1
        base_params["offset"] = str(offset)
        url = f"{endpoint}?{urlencode(base_params)}"
        try:
            resp = api_request.get(url, timeout=30000)
        except Exception as e:
            log.error(f"  API request failed at offset={offset}: {e}")
            break

        if resp.status != 200:
            log.error(f"  API returned status {resp.status} at offset={offset}; stopping pagination")
            break

        try:
            data = resp.json()
        except Exception as e:
            log.error(f"  Failed to parse JSON at offset={offset}: {e}")
            break

        # Save the raw page response for reference
        out = calls_dir / f"calls_page_{page_idx:04d}.json"
        out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

        calls = data.get("data", {}).get("calls", []) or []
        total = data.get("data", {}).get("filteredTotal") or data.get("data", {}).get("total")
        all_calls.extend(calls)

        log.info(
            f"  Page {page_idx}: {len(calls)} calls (offset={offset}, "
            f"running total={len(all_calls)}, server-reported total={total})"
        )

        if len(calls) < page_size:
            break  # last page
        offset += page_size
        human_pause("after_action")

    # Save aggregated all-calls file (the most useful single artifact)
    aggregate = {
        "exported_at": datetime.now().isoformat(),
        "endpoint": endpoint,
        "params": {k: v for k, v in base_params.items() if k != "offset"},
        "total_calls": len(all_calls),
        "calls": all_calls,
    }
    (calls_dir / "all_calls.json").write_text(
        json.dumps(aggregate, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    log.info(f"  Total calls exported: {len(all_calls)} → calls/all_calls.json")

    # For each call that has an ID, also fetch its detail (if a detail endpoint exists)
    if all_calls:
        _fetch_call_details(api_request, endpoint, all_calls, calls_dir)


def _fetch_call_details(api_request, calls_endpoint: str, calls: list, calls_dir: Path):
    """For each call, attempt GET /api/calls/<id> to capture the detailed payload.

    The list endpoint may return summary info; the detail endpoint typically
    has full transcript, recording URL, etc.
    """
    details_dir = calls_dir / "details"
    details_dir.mkdir(parents=True, exist_ok=True)

    log.info(f"Fetching detail for {len(calls)} calls")
    succeeded = 0
    failed = 0
    long_pause_every = PACE.get("long_pause_every_n", 10)

    for i, call in enumerate(calls):
        call_id = call.get("id") or call.get("callId") or call.get("_id")
        if not call_id:
            continue
        out = details_dir / f"call_{call_id}.json"
        if out.exists():
            continue
        url = f"{calls_endpoint}/{call_id}"
        try:
            resp = api_request.get(url, timeout=30000)
            if resp.status == 200:
                out.write_text(json.dumps(resp.json(), indent=2, ensure_ascii=False), encoding="utf-8")
                succeeded += 1
            elif resp.status == 404:
                # Detail endpoint may not exist; stop trying after first 404
                log.info(f"  No detail endpoint at {url} (404); skipping rest")
                break
            else:
                failed += 1
        except Exception as e:
            log.warning(f"  Detail fetch failed for {call_id}: {e}")
            failed += 1

        human_pause("between_calls")
        if long_pause_every and (i + 1) % long_pause_every == 0:
            pause_s = random.uniform(*PACE["long_pause"])
            log.info(f"  Reading pause ({pause_s:.1f}s) after {i + 1} call details")
            time.sleep(pause_s)

    log.info(f"Call details: {succeeded} succeeded, {failed} failed")


def _extract_calls_via_dom(page: Page, base_url: str, calls_dir: Path, discoveries: dict):
    """Fallback: DOM-based extraction (clicking each row).

    Used only when the /api/calls URL wasn't discovered. Same approach as the
    earlier version of this script.
    """
    analytics = (discoveries or {}).get("analytics") or {}
    call_row_selector = analytics.get("call_row_selector")
    pagination_type = analytics.get("pagination_type", "infinite-scroll")
    load_more_text = analytics.get("load_more_text")
    date_widener_text = analytics.get("date_widener_text")

    url = urljoin(base_url, "/admin/analytics")
    log.info(f"DOM fallback — loading analytics page: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    human_pause("after_navigation")

    if date_widener_text:
        log.info(f"  Widening date range via: '{date_widener_text}'")
        try:
            page.locator(f'button:has-text("{date_widener_text}")').first.click()
            human_pause("after_action")
        except Exception as e:
            log.warning(f"    Date-range widening failed: {e}")

    if pagination_type == "load-more-button" and load_more_text:
        _load_more_until_done(page, load_more_text)
    elif pagination_type == "infinite-scroll":
        _scroll_until_done(page)
    else:
        _scroll_until_done(page)

    list_html = page.content()
    (calls_dir / "calls_list.html").write_text(list_html, encoding="utf-8")

    if not call_row_selector:
        log.warning("No call row selector discovered; only list HTML saved")
        return

    rows = page.locator(call_row_selector).all()
    log.info(f"  Found {len(rows)} call rows with selector: {call_row_selector}")
    long_pause_every = PACE.get("long_pause_every_n", 10)
    for i, row in enumerate(rows):
        try:
            row.scroll_into_view_if_needed()
            row.click()
            human_pause("between_calls")
            if long_pause_every and (i + 1) % long_pause_every == 0:
                time.sleep(random.uniform(*PACE["long_pause"]))
        except Exception as e:
            log.warning(f"  Failed to click row #{i}: {e}")


def _load_more_until_done(page: Page, button_text: str, max_clicks: int = 200):
    """Click the discovered Load-more button repeatedly until it disappears."""
    selector = f'button:has-text("{button_text}")'
    clicked = 0
    while clicked < max_clicks:
        try:
            btn = page.locator(selector).first
            if not btn.is_visible(timeout=2000):
                break
            btn.click()
            clicked += 1
            human_pause("after_load_more")
        except Exception:
            break
    log.info(f"  Clicked load-more {clicked} times")


def _scroll_until_done(page: Page, max_iterations: int = 200, no_change_threshold: int = 4):
    """Scroll to the bottom repeatedly until the page stops growing."""
    last_height = -1
    no_change_streak = 0
    iterations = 0
    while no_change_streak < no_change_threshold and iterations < max_iterations:
        iterations += 1
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        human_pause("after_scroll")
        new_height = page.evaluate("document.body.scrollHeight")
        if new_height == last_height:
            no_change_streak += 1
        else:
            no_change_streak = 0
            last_height = new_height
    log.info(f"  Scrolled {iterations} times, page height stabilized")


def extract_settings_tabs(page: Page, base_url: str, output_dir: Path, discoveries: dict):
    """Visit /admin/settings, click each tab using the selector pattern discovered
    during reconnaissance, save the rendered HTML for each.

    The Settings page is a single URL with in-page tabs — sub-tabs do not have
    distinct URLs. The reconnaissance phase discovered both the selector
    pattern that works and the actual tab labels present.
    """
    settings = (discoveries or {}).get("settings") or {}
    selector_pattern = settings.get("selector_pattern")
    tab_labels = settings.get("tab_labels") or []

    if not selector_pattern or not tab_labels:
        log.warning("Settings tab discovery returned nothing; skipping tab extraction")
        log.warning("Default settings view is still saved by extract_page() (settings.html)")
        return

    scroll_aria = settings.get("scroll_right_aria_label") or ""
    log.info(f"Extracting {len(tab_labels)} settings tabs using pattern: {selector_pattern}")
    if scroll_aria:
        log.info(f"  Tab strip is a horizontal scroller; right-arrow aria-label: '{scroll_aria}'")

    settings_url = urljoin(base_url, "/admin/settings")
    page.goto(settings_url, wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    human_pause("after_navigation")

    for label in tab_labels:
        log.info(f"  Tab: {label}")
        tab_selector = f'{selector_pattern}:has-text("{label}")'
        # If the tab isn't in the DOM (virtualized scroller has it off-screen),
        # click the right-arrow to reveal it. Up to 5 scrolls.
        for attempt in range(6):
            try:
                if page.locator(tab_selector).first.is_visible(timeout=800):
                    break
            except Exception:
                pass
            if attempt == 5:
                break
            if not _scroll_tab_strip_right(page, scroll_aria):
                break  # no scroll arrow found; can't reveal more tabs
            human_pause("after_action")

        try:
            tab_locator = page.locator(tab_selector).first
            tab_locator.scroll_into_view_if_needed()
            tab_locator.click()
        except Exception as e:
            log.warning(f"    Failed to click '{label}': {e}")
            continue

        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        human_pause("after_action")

        slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "tab"
        html = page.content()
        out = output_dir / f"settings-{slug}.html"
        out.write_text(html, encoding="utf-8")
        log.info(f"    Saved {out.name} ({len(html):,} bytes)")


def _scroll_tab_strip_right(page: Page, aria_label: str = "") -> bool:
    """Click the right-arrow scroll button in a horizontal tab strip. Returns True if clicked."""
    # Prefer the discovered aria-label if available — most stable selector
    if aria_label:
        try:
            btn = page.locator(f'button[aria-label="{aria_label}"]').first
            if btn.is_visible(timeout=500):
                btn.click()
                return True
        except Exception:
            pass
    # Fallback heuristic: find a chevron-right or arrow-right button via JS
    try:
        clicked = page.evaluate(
            """() => {
                const root = document.querySelector('main, [role="main"]') || document.body;
                for (const btn of root.querySelectorAll('button')) {
                    const text = (btn.textContent || '').trim();
                    const aria = (btn.getAttribute('aria-label') || '').toLowerCase();
                    const innerHTML = btn.innerHTML.toLowerCase();
                    const matches =
                        /^[\\u203a>\\u2192]$/.test(text) ||
                        /next|scroll[\\s-]*right|right[\\s-]*arrow/.test(aria) ||
                        innerHTML.includes('chevron-right') ||
                        innerHTML.includes('arrow-right') ||
                        btn.querySelector('svg.lucide-chevron-right, [data-icon="chevron-right"]');
                    if (matches) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            }"""
        )
        return bool(clicked)
    except Exception:
        return False




# ---------------------------------------------------------------------------
# Audio download (uses session cookies via Playwright's APIRequestContext)
# ---------------------------------------------------------------------------

_AUDIO_URL_PATTERNS = [
    re.compile(r'https?://[^\s"\']+\.mp3[^\s"\']*'),
    re.compile(r'https?://[^\s"\']+\.wav[^\s"\']*'),
    re.compile(r'https?://[^\s"\']+\.m4a[^\s"\']*'),
    re.compile(r'https?://[^\s"\']+/recordings?/[^\s"\']+'),
    re.compile(r'https?://[^\s"\']+/audio/[^\s"\']+'),
]


def _scan_text_for_audio_urls(text: str) -> set:
    """Extract candidate audio URLs from a JSON/text blob."""
    urls = set()
    for pat in _AUDIO_URL_PATTERNS:
        for match in pat.findall(text):
            urls.add(match.rstrip(',"\'}]'))
    return urls


def _download_audio_url(api_request, url: str, audio_dir: Path) -> str:
    """Download a single audio URL using session-authenticated request. Returns "ok", "skip", or "fail"."""
    # Build a sensible local filename. The S3 keys are long; preserve the
    # extension by splitting it out before truncating the base name.
    raw_name = urlparse(url).path.split("/")[-1] or "audio"
    if "." in raw_name:
        base, ext = raw_name.rsplit(".", 1)
        ext = "." + ext[:8]  # protect against malformed extensions
    else:
        base = raw_name
        ext = ".bin"
    base = re.sub(r"[^a-zA-Z0-9._-]", "_", base)[:80]
    tail = base + ext
    out = audio_dir / tail
    if out.exists():
        return "skip"
    try:
        resp = api_request.get(url)
        if resp.status == 200:
            out.write_bytes(resp.body())
            return "ok"
        log.warning(f"    HTTP {resp.status} — skipped {url}")
        return "fail"
    except Exception as e:
        log.warning(f"    Failed {url}: {e}")
        return "fail"


def download_audio(context: BrowserContext, search_paths: list, audio_dir: Path):
    """Scan all JSON files under the given search paths for audio URLs and download them.

    `search_paths` is a list of Path objects — directories to recursively scan
    for *.json files. Typically [exports/<ts>/api_captures, exports/<ts>/calls].
    Idempotent: already-downloaded files are skipped.
    """
    audio_dir.mkdir(parents=True, exist_ok=True)
    api_request = context.request

    json_files = []
    for path in search_paths:
        if path and path.exists():
            json_files.extend(sorted(path.rglob("*.json")))

    log.info(f"Scanning {len(json_files)} JSON files across {len(search_paths)} paths for audio URLs")
    seen = set()
    downloaded = 0
    failed = 0
    skipped_existing = 0
    for filepath in json_files:
        try:
            text = filepath.read_text(encoding="utf-8")
        except Exception:
            continue
        for url in _scan_text_for_audio_urls(text):
            if url in seen:
                continue
            seen.add(url)
            result = _download_audio_url(api_request, url, audio_dir)
            if result == "ok":
                downloaded += 1
                log.info(f"  Downloaded: {url[:100]}")
            elif result == "skip":
                skipped_existing += 1
            elif result == "fail":
                failed += 1
    log.info(
        f"Audio downloads: {downloaded} new, {skipped_existing} already on disk, "
        f"{failed} failed (from {len(seen)} unique URLs found)"
    )


def find_latest_export_with(filename_or_subdir: str) -> Path | None:
    """Find the most recent export directory that contains the given file or subdir.

    Important: skips directories that don't contain it. This is what's needed
    for --skip-recon (find latest with discovery.json) and --audio-only (find
    latest with api_captures), so we don't accidentally pick the just-created
    empty directory for the current run.
    """
    if not EXPORTS_ROOT.exists():
        return None
    candidates = sorted(
        [d for d in EXPORTS_ROOT.iterdir() if d.is_dir()],
        key=lambda d: d.name,
        reverse=True,
    )
    for candidate in candidates:
        if (candidate / filename_or_subdir).exists():
            return candidate
    return None


def find_latest_export_dir() -> Path | None:
    """Return the most recent exports/<timestamp>/ directory, no filtering."""
    if not EXPORTS_ROOT.exists():
        return None
    candidates = sorted(
        [d for d in EXPORTS_ROOT.iterdir() if d.is_dir()],
        key=lambda d: d.name,
        reverse=True,
    )
    return candidates[0] if candidates else None


def load_latest_discovery() -> dict | None:
    """Load discovery.json from the most recent export that has one."""
    latest = find_latest_export_with("discovery.json")
    if not latest:
        return None
    try:
        return json.loads((latest / "discovery.json").read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"Failed to load discovery.json from {latest}: {e}")
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Export Hey Sadie dashboard data")
    parser.add_argument("--headed", action="store_true", help="Show browser window")
    parser.add_argument("--debug", action="store_true", help="Verbose logging")
    parser.add_argument("--calls-only", action="store_true", help="Only extract call data")
    parser.add_argument("--no-audio", action="store_true", help="Skip audio downloads")
    parser.add_argument("--clear-session", action="store_true", help="Force re-login")
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Use faster (less polite) pacing — for development only, NOT for real runs",
    )
    # Phase control flags — these let the .bat wrappers run pieces independently
    parser.add_argument(
        "--auth-only",
        action="store_true",
        help="Authenticate, save session, exit (Phase 1 only)",
    )
    parser.add_argument(
        "--recon-only",
        action="store_true",
        help="Auth + reconnaissance, save discovery.json, exit (Phases 1-2)",
    )
    parser.add_argument(
        "--skip-recon",
        action="store_true",
        help="Skip reconnaissance, reuse discovery.json from most recent export",
    )
    parser.add_argument(
        "--audio-only",
        action="store_true",
        help="Skip extraction; only download audio from most recent export's captures",
    )
    args = parser.parse_args()

    if args.debug:
        log.setLevel(logging.DEBUG)

    # Pick pacing profile
    global PACE
    if args.fast:
        PACE = PACE_FAST
        log.warning("Using FAST pace — bot-detectable. Use only for development.")
    else:
        PACE = PACE_NORMAL
        log.info("Using normal (human-like) pace with randomized delays")

    load_dotenv(Path(__file__).parent / ".env")
    email = os.getenv("HEYSADIE_EMAIL")
    password = os.getenv("HEYSADIE_PASSWORD")
    base_url = os.getenv("HEYSADIE_BASE_URL", DEFAULT_BASE_URL)

    if not email or not password:
        log.error("Set HEYSADIE_EMAIL and HEYSADIE_PASSWORD in .env (see .env.example)")
        sys.exit(1)

    if args.clear_session and SESSION_FILE.exists():
        SESSION_FILE.unlink()
        log.info("Removed saved session")

    # --audio-only does not create a new export — it works against the latest one
    if args.audio_only:
        latest = find_latest_export_with("api_captures")
        if not latest:
            log.error("No previous export with api_captures found. Run a full export first.")
            sys.exit(1)
        log.info(f"Audio-only mode: targeting most recent export → {latest.name}")
        api_dir = latest / "api_captures"
        audio_dir = latest / "calls" / "audio"
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not args.headed)
            context_kwargs = {
                "user_agent": CHROME_USER_AGENT,
                "viewport": {"width": 1440, "height": 900},
                "locale": "en-US",
                "timezone_id": "America/Los_Angeles",
            }
            if SESSION_FILE.exists():
                context = browser.new_context(storage_state=str(SESSION_FILE), **context_kwargs)
            else:
                log.error("No saved session — run auth.bat first")
                browser.close()
                sys.exit(1)
            ok = authenticate(context, base_url, email, password, headed=args.headed)
            if not ok:
                browser.close()
                sys.exit(1)
            context.storage_state(path=str(SESSION_FILE))
            # Scan both api_captures/ (recon-time responses) AND calls/ (the
            # bulk per-page response files containing the actual recording URLs)
            download_audio(context, [api_dir, latest / "calls"], audio_dir)
            browser.close()
        return

    today = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    output_root = EXPORTS_ROOT / today
    pages_dir = output_root / "pages"
    api_dir = output_root / "api_captures"
    pages_dir.mkdir(parents=True, exist_ok=True)
    api_dir.mkdir(parents=True, exist_ok=True)

    log.info(f"Output directory: {output_root}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)

        # Browser context with a normal Chrome User-Agent and a real-looking viewport
        context_kwargs = {
            "user_agent": CHROME_USER_AGENT,
            "viewport": {"width": 1440, "height": 900},
            "locale": "en-US",
            "timezone_id": "America/Los_Angeles",
        }
        if SESSION_FILE.exists():
            log.info(f"Loading saved session: {SESSION_FILE.name}")
            context = browser.new_context(storage_state=str(SESSION_FILE), **context_kwargs)
        else:
            context = browser.new_context(**context_kwargs)

        capture = APICapture(api_dir)
        capture.attach(context)

        # ---- Phase 1: Authentication ----
        ok = authenticate(context, base_url, email, password, headed=args.headed)
        if not ok:
            log.error("Authentication failed")
            browser.close()
            sys.exit(1)
        context.storage_state(path=str(SESSION_FILE))
        log.info(f"Session saved to {SESSION_FILE.name}")

        if args.auth_only:
            log.info("Auth-only mode: stopping after authentication")
            browser.close()
            return

        page = context.new_page()
        page.goto(urljoin(base_url, "/admin/analytics"), wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        # Discover real nav URLs (sanity check vs DASHBOARD_PAGES)
        discover_nav_links(page, output_root)

        # ---- Phase 2: Reconnaissance ----
        if args.skip_recon:
            log.info("Skip-recon mode: loading discovery.json from latest export")
            discoveries = load_latest_discovery()
            if not discoveries:
                log.error("No previous discovery.json found. Run recon first.")
                browser.close()
                sys.exit(1)
            # Persist discovery into the new export folder for traceability
            (output_root / "discovery.json").write_text(
                json.dumps(discoveries, indent=2), encoding="utf-8"
            )
        else:
            discoveries = discover_selectors(page, base_url, output_root, capture=capture)

        if args.recon_only:
            log.info("Recon-only mode: stopping after reconnaissance")
            capture.save_index(output_root / "api_index.json")
            browser.close()
            return

        # ---- Phase 3: Bulk extraction ----
        extract_calls(page, base_url, output_root, discoveries)

        if not args.calls_only:
            for name, path in DASHBOARD_PAGES:
                if name == "analytics":
                    continue  # already done above
                extract_page(page, name, path, base_url, pages_dir)
            extract_settings_tabs(page, base_url, pages_dir, discoveries)

        # ---- Phase 4: Audio downloads ----
        if not args.no_audio:
            log.info("Scanning captured API responses + extracted calls for audio URLs")
            audio_dir = output_root / "calls" / "audio"
            # Scan both the recon-time captures AND the bulk extracted calls/
            # files, since /api/calls responses (with recording_url fields) live
            # in calls/calls_page_*.json after extract_calls runs.
            download_audio(context, [api_dir, output_root / "calls"], audio_dir)

        capture.save_index(output_root / "api_index.json")

        manifest = {
            "exported_at": datetime.now().isoformat(),
            "base_url": base_url,
            "pages_attempted": len(DASHBOARD_PAGES),
            "api_responses_captured": len(capture.responses),
            "options": {
                "headed": args.headed,
                "calls_only": args.calls_only,
                "audio_downloaded": not args.no_audio,
                "skip_recon": args.skip_recon,
            },
        }
        (output_root / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

        log.info(f"Export complete → {output_root}")
        browser.close()


if __name__ == "__main__":
    main()
