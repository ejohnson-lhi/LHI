"""Playwright-driven Cloudbeds dashboard automation.

WHY: Cloudbeds' Pay-by-Link API is gated behind a Marketplace App we
don't have credentials for. The same feature is available in their
admin dashboard. We log in as a staff user via Playwright, generate
the link, capture the URL, and surface it to the guest as an iframe /
"open in new tab" link.

PHASE 1 (this file): framework + login skeleton + failure-alert path.
The actual selector-driven 'click through the dashboard' code is
stubbed so we can ship the surrounding plumbing and then fill in
selectors during a HEADED-mode walkthrough.

PHASE 2 (next): replace the NotImplementedError in
generate_pay_by_link_for_reservation with real selectors discovered
by running playwright codegen against the live dashboard.

Failure protocol:
  - Every exception path logs a clear error + saves a screenshot to
    backend/logs/cloudbeds_failure_<timestamp>.png
  - SMS alert fires (to settings.cloudbeds_automation_alert_phone or
    fallback to eric_cell_number) deduped against a 5-minute window
    so a flaky session doesn't spam.
  - Caller receives None / error string; surfaces graceful fallback
    to the guest ("call the front desk").

Selector philosophy:
  - Prefer role-based (get_by_role) + accessible-name selectors over
    CSS / XPath. Cloudbeds' frontend is React; CSS classes are
    obfuscated and rotate on builds, but role+name is stable across
    redesigns most of the time.
  - Keep selectors in named module-level constants so a UI change
    only requires editing one spot (and the SMS alert points us right
    at it).
"""
import asyncio
import logging
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import settings

log = logging.getLogger(__name__)

# --- Selectors (Phase 2 will fill these in via codegen) -----------------
# Keep these as named constants so a UI redesign affects ONE spot and the
# SMS alert can include the failing selector name. The "first guess"
# values are reasonable starting points; Phase 2 will validate / replace
# each one against the live UI.

# Login (Okta-hosted)
SEL_LOGIN_EMAIL = "input[type='email'], input[name='username'], input[name='identifier']"
SEL_LOGIN_PASSWORD = "input[type='password'], input[name='password'], input[name='credentials.passcode']"
SEL_LOGIN_NEXT = "input[type='submit'], button[type='submit']"
# 2FA: Okta defaults to push -- we need to switch to TOTP
# The page heading when Okta shows the factor picker ("Select from the
# following options"). Used to recognize we're on that screen rather
# than directly on a TOTP entry page.
SEL_2FA_FACTOR_PICKER_MARKER = "text=Select from the following options"
SEL_2FA_CHOOSE_DIFFERENT = "a:has-text('Verify with something else'), a:has-text('different way'), a:has-text('Try another way')"
# "Select" link/button next to the Google Authenticator row. Okta's
# sign-in widget renders these as <a> tags with descriptive aria-labels,
# but we try a couple of fallbacks too so a UI refresh doesn't kill us.
SEL_2FA_PICK_GOOGLE_AUTH_PRIMARY = "[aria-label*='Google Authenticator']"
SEL_2FA_PICK_GOOGLE_AUTH_FALLBACK = (
    "xpath=//*[normalize-space()='Google Authenticator']"
    "/following::*[normalize-space()='Select'][1]"
)
SEL_2FA_TOTP_INPUT = "input[name='credentials.passcode'], input[name='answer'], input[autocomplete='one-time-code']"
SEL_2FA_VERIFY = "input[value='Verify'], button:has-text('Verify')"

# Post-login markers (Cloudbeds dashboard)
SEL_DASHBOARD_MARKER = ""  # TODO: an element that only appears post-login
SEL_RESERVATION_SEARCH = ""  # TODO
SEL_PAYLINK_BUTTON = ""  # TODO
SEL_PAYLINK_AMOUNT = ""  # TODO
SEL_PAYLINK_AUTH_ONLY = ""  # TODO: checkbox for auth-only mode
SEL_PAYLINK_GENERATE = ""  # TODO
SEL_PAYLINK_URL = ""  # TODO


def _current_totp() -> str | None:
    """Generate the 6-digit code for the configured TOTP secret. Returns
    None when no secret is configured. Uses the standard 30s window."""
    if not settings.cloudbeds_totp_secret:
        return None
    try:
        import pyotp
        return pyotp.TOTP(settings.cloudbeds_totp_secret).now()
    except Exception as ex:
        log.exception("TOTP generation failed: %s", ex)
        return None


def _entry_url() -> str:
    """The URL a human staffer would type into the address bar. Going
    here first (instead of straight to signin) means: (a) if we already
    have a session cookie, Cloudbeds takes us straight to the dashboard
    with zero credential prompts; (b) if we don't, Cloudbeds itself
    redirects us through the Okta signin flow -- which is what a real
    human would experience. Less obviously-scripted than always opening
    the bare signin URL."""
    return f"https://hotels.cloudbeds.com/connect/{settings.cloudbeds_property_id}#/reservations"


# Typo-simulation parameters. ~3% of alphanumeric chars get a typo +
# backspace-correct cycle, matching real-human keystroke error rates.
# Skipped for short fields (<8 chars) and explicitly disabled for TOTP
# (a 6-digit code under time pressure with a typo would just rotate to
# stale; safer to type it cleanly).
_TYPO_RATE = 0.03
_MIN_LEN_FOR_TYPOS = 8


def _keystroke_delay_ms(base_ms: float, familiarity: float = 1.0) -> int:
    """One keystroke delay (ms) drawn from a mixture distribution that
    approximates real human typing better than a flat 0.5x-1.5x jitter.

    Real typing is bursty: most keystrokes cluster tightly near the
    median, with a few much faster (muscle-memory bigrams) and a few
    much slower (re-reading source material, thinking). Uniform jitter
    misses both tails, so the signal looks too regular to bot-detection.

    Mixture by probability:
       10% burst   (0.20x - 0.50x of base)  — familiar key sequences
       75% normal  (0.70x - 1.30x of base)  — typical cadence
       12% slow    (1.50x - 2.50x of base)  — re-reading the source
        3% pause   (3.00x - 6.00x of base)  — genuine thinking pause

    `familiarity` scales the entire distribution:
        0.5 = very familiar (own name, your email, well-known password)
        1.0 = average
        1.5 = unfamiliar (numbers off a credit card)
    """
    eff = base_ms * max(0.1, familiarity)
    r = random.random()
    if r < 0.10:
        return max(15, int(eff * random.uniform(0.20, 0.50)))
    if r < 0.85:
        return int(eff * random.uniform(0.70, 1.30))
    if r < 0.97:
        return int(eff * random.uniform(1.50, 2.50))
    return int(eff * random.uniform(3.00, 6.00))

# Failure-alert dedup. Keyed by short signature; values are timestamps.
_last_alert_at: dict[str, float] = {}
_ALERT_DEDUP_WINDOW_SECONDS = 300.0

# Cached browser context so consecutive requests can skip the login dance.
# Reset on any failure (we'd rather pay the login cost than chase a stale
# session). The lock prevents two requests from racing on login.
_session_lock = asyncio.Lock()
_session_storage_state: dict[str, Any] | None = None

# Disk-backed session cache. Without this, every fresh Python process
# starts with no Playwright cookies and has to do the full Okta dance —
# even though Okta itself often skips 2FA via its own "remember device"
# cookie. Storing storage_state on disk lets consecutive script runs
# skip even the email/password retype, going straight to a quick
# "is the session still valid?" navigation check (~5s instead of ~30s).
# Path is in data/ which is already gitignored (lighthouse.db lives there).
# 8-hour TTL — Cloudbeds' session cookie typically lasts much longer
# than that but we expire conservatively so a stale cache doesn't keep
# us limping past a real session-revocation.
_SESSION_CACHE_PATH = Path("data") / ".cloudbeds_session.json"
_SESSION_CACHE_MAX_AGE_SECONDS = 8 * 3600


def _load_cached_session_from_disk() -> dict[str, Any] | None:
    """Return cached storage_state if the on-disk file exists and is
    younger than the TTL. None otherwise (forces a full login)."""
    try:
        if not _SESSION_CACHE_PATH.exists():
            return None
        age = time.time() - _SESSION_CACHE_PATH.stat().st_mtime
        if age > _SESSION_CACHE_MAX_AGE_SECONDS:
            log.info(
                "Cloudbeds session cache is %.1f hours old; ignoring (will re-login).",
                age / 3600,
            )
            return None
        import json
        with _SESSION_CACHE_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as ex:
        log.warning("Couldn't load Cloudbeds session cache: %s", ex)
        return None


def _save_cached_session_to_disk(state: dict[str, Any]) -> None:
    """Persist storage_state for re-use across script runs. Contains
    cookies — treat as a secret. Lives under data/ which is gitignored."""
    try:
        _SESSION_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        import json
        # Write to a temp file then rename, so a crash mid-write doesn't
        # leave a corrupted JSON we'd then fail to parse next time.
        tmp = _SESSION_CACHE_PATH.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state, f)
        tmp.replace(_SESSION_CACHE_PATH)
    except Exception as ex:
        log.warning("Couldn't save Cloudbeds session cache: %s", ex)


def _alert_signature(msg: str) -> str:
    """Short stable hash of the alert body so we dedup identical alerts
    arriving in a burst (UI redesign breaks every guest's attempt within
    minutes; we don't want N SMS messages)."""
    return msg[:60]


def _looks_like_phone(s: str) -> bool:
    """Cheap validation that a string is plausibly an E.164 phone number,
    so we don't try to Twilio-send to a value that's actually a misread
    .env comment line. Real validation happens at Twilio anyway."""
    if not s:
        return False
    s = s.strip()
    if not s.startswith("+"):
        return False
    digits = s[1:].replace(" ", "").replace("-", "")
    return digits.isdigit() and 8 <= len(digits) <= 15


async def _send_failure_alert(msg: str, *, reservation_id: str | None = None) -> None:
    """SMS the operator when automation breaks. Deduped on a 5-minute
    sliding window per error signature."""
    from app.tools.twilio_sms import send_sms

    phone = settings.cloudbeds_automation_alert_phone or settings.eric_cell_number
    if not _looks_like_phone(phone):
        log.warning(
            "Cloudbeds automation failure (no usable alert phone -- got %r): %s",
            phone[:40] if phone else None, msg,
        )
        return

    sig = _alert_signature(msg)
    now = time.time()
    last = _last_alert_at.get(sig)
    if last is not None and (now - last) < _ALERT_DEDUP_WINDOW_SECONDS:
        log.warning("Cloudbeds automation failure (alert deduped): %s", msg)
        return

    body = f"Cloudbeds automation failed"
    if reservation_id:
        body += f" for res {reservation_id}"
    body += f" at {datetime.now().strftime('%H:%M')}: {msg[:140]}"

    try:
        await send_sms(phone, body)
        _last_alert_at[sig] = now
        log.warning("Cloudbeds automation alert SMS sent to %s: %s", phone, msg)
    except Exception as ex:
        log.exception("Failed to send Cloudbeds automation alert SMS: %s", ex)


async def _capture_screenshot(page, tag: str) -> str | None:
    """Save a forensic screenshot of the current page state. Returns the
    saved path on success, None on failure (don't let screenshot failures
    obscure the real automation failure)."""
    try:
        out_dir = Path("logs")
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"cloudbeds_failure_{tag}_{int(time.time())}.png"
        await page.screenshot(path=str(path), full_page=True)
        log.info("Cloudbeds failure screenshot: %s", path)
        return str(path)
    except Exception as ex:
        log.warning("Couldn't capture failure screenshot: %s", ex)
        return None


class CloudbedsBrowser:
    """Manages a Playwright browser context against the Cloudbeds dashboard.
    Use as an async context manager; will reuse a logged-in session across
    calls when possible.

    Phase 1: __aenter__ / __aexit__ work; login() works (Phase 2 needs to
    verify the real selectors). The actual per-page actions
    (generate_pay_by_link, etc.) are NotImplementedError until Phase 2.
    """

    def __init__(self):
        self._pw = None
        self._browser = None
        self._context = None
        self.page = None  # public for diagnostic access during selector-discovery

    async def __aenter__(self):
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=settings.cloudbeds_browser_headless,
            slow_mo=settings.cloudbeds_browser_slow_mo_ms,
            # Hide the most obvious "this is automation" flag. Chromium
            # normally adds --enable-automation which sets the
            # navigator.webdriver=true (auth flows often check for this
            # and silently reject).
            args=["--disable-blink-features=AutomationControlled"],
        )
        # Reuse stored auth state across requests when possible. Two-tier:
        # in-memory module-level cache for fast access within a process,
        # disk-backed cache for re-use across fresh script runs.
        global _session_storage_state
        if _session_storage_state is None:
            _session_storage_state = _load_cached_session_from_disk()
            if _session_storage_state:
                log.info("Cloudbeds: loaded session cache from disk; will skip credential entry.")
        ctx_kwargs: dict[str, Any] = {
            "viewport": {"width": 1400, "height": 900},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        }
        if _session_storage_state:
            ctx_kwargs["storage_state"] = _session_storage_state
        self._context = await self._browser.new_context(**ctx_kwargs)
        # Inject script that masks navigator.webdriver and a few other
        # easy "is this a bot?" tells. Runs in EVERY frame before site
        # scripts. Not a full stealth bundle -- enough to get past Okta's
        # basic checks. If we ever need more (canvas fingerprinting, etc.)
        # consider the `playwright-stealth` package.
        await self._context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            // Spoof a small plugin list (default = 0 plugins = headless tell)
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5],
            });
            // Spoof languages (default empty in some headless modes)
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en'],
            });
        """)
        # Start tracing -- records every navigation, click, screenshot, and
        # network call. Hugely useful when something silently fails. Open
        # with: playwright show-trace logs/cloudbeds_trace_<ts>.zip
        try:
            await self._context.tracing.start(
                screenshots=True, snapshots=True, sources=True,
            )
        except Exception as ex:
            log.warning("Couldn't start Playwright tracing: %s", ex)
        self.page = await self._context.new_page()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        # Save the trace EVERY run (success or fail). Cheap insurance.
        try:
            if self._context is not None:
                out = Path("logs") / f"cloudbeds_trace_{int(time.time())}.zip"
                out.parent.mkdir(exist_ok=True)
                await self._context.tracing.stop(path=str(out))
                log.info("Playwright trace: %s", out)
        except Exception as ex:
            log.warning("Couldn't save Playwright trace: %s", ex)
        # Save the post-action auth state so the next call can skip login.
        # Unconditional: cookies are just cookies, and the TTL check on the
        # load side rejects stale ones. Worst case after a crashed run is a
        # cache with partial Okta cookies; next run loads it, validation
        # fails on the dashboard nav, login() re-runs the credential flow
        # and replaces the cache. One extra login. Not worth gating on
        # exc_type and missing legitimate "user pressed Ctrl+C after a
        # successful save" cases.
        try:
            if self._context is not None:
                global _session_storage_state
                _session_storage_state = await self._context.storage_state()
                _save_cached_session_to_disk(_session_storage_state)
                log.info("Cloudbeds: saved session cache to disk.")
        except Exception as ex:
            log.warning("Couldn't save session cache on exit: %s", ex)
        try:
            if self._context: await self._context.close()
        except Exception: pass
        try:
            if self._browser: await self._browser.close()
        except Exception: pass
        try:
            if self._pw: await self._pw.stop()
        except Exception: pass

    async def _checkpoint(self, tag: str) -> None:
        """Log + screenshot at a named step. Cheap diagnostics so we can
        reconstruct what the page looked like at each stage."""
        try:
            log.info("Cloudbeds checkpoint [%s]: url=%s", tag, self.page.url)
            await _capture_screenshot(self.page, f"step_{tag}")
        except Exception:
            pass

    async def _is_logged_in(self) -> bool:
        """Heuristic: are we already authenticated against Cloudbeds?
        Navigates to the dashboard entry URL (same one a real staffer
        would open) and checks whether we got bounced to signin/auth."""
        if not self.page:
            return False
        try:
            await self.page.goto(_entry_url(), wait_until="domcontentloaded", timeout=20000)
        except Exception:
            return False
        # Give any client-side redirect a beat to finish (Cloudbeds'
        # dashboard runs a quick auth check on load and pushes you to
        # signin if the cookie's gone). Then check the host -- the
        # dashboard runs on hotels.cloudbeds.com, but other cloudbeds.com
        # subdomains (signin.*, auth.*) are intermediate auth steps.
        try:
            await self.page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        try:
            host = (await self.page.evaluate("location.hostname")).lower()
        except Exception:
            return False
        return (
            host.endswith("cloudbeds.com")
            and not host.startswith("signin.")
            and not host.startswith("auth.")
        )

    async def _click_first_visible(self, *selectors: str, timeout: int = 5000) -> bool:
        """Try each selector in order; click the first one that's visible.
        Returns True on success, False if none matched. Useful for the
        Okta flow where step names ("Verify with something else") might
        be slightly different across UI revisions."""
        for sel in selectors:
            if not sel:
                continue
            try:
                el = self.page.locator(sel).first
                await el.wait_for(state="visible", timeout=timeout)
                await el.click()
                return True
            except Exception:
                continue
        return False

    async def _fill_first_visible(self, value: str, *selectors: str, timeout: int = 5000) -> bool:
        for sel in selectors:
            if not sel:
                continue
            try:
                el = self.page.locator(sel).first
                await el.wait_for(state="visible", timeout=timeout)
                await el.fill(value)
                return True
            except Exception:
                continue
        return False

    async def _humanlike_type(
        self,
        value: str,
        *selectors: str,
        timeout: int = 5000,
        allow_typos: bool = True,
        familiarity: float = 1.0,
    ) -> bool:
        """Like _fill_first_visible but types one char at a time with
        mixture-distribution per-char delays (see _keystroke_delay_ms) and
        an occasional typo-and-backspace cycle.

        `familiarity` scales the cadence:
            0.5 = own name / well-known email (fast, with bursts)
            1.0 = default
            1.5 = card-from-physical-card (slower, more mid-typing pauses)

        Auth flows (Okta in particular) treat instant `.fill()` as
        scripted input and silently reject. This mimics human cadence well
        enough to get past the gate. The mixture distribution is what
        actually gets us past Stripe-Elements-grade bot detection: a flat
        uniform jitter is itself a tell.

        When `allow_typos` is True AND `value` is long enough (>= 8 chars),
        each alphanumeric char has a small chance of producing a wrong
        character followed by Backspace before the correct one. Disable
        for short fixed-format values (TOTP codes, CVV, expiry) where a
        typo+backspace would look more bot-like than typing cleanly."""
        for sel in selectors:
            if not sel:
                continue
            try:
                el = self.page.locator(sel).first
                ok = await self._humanlike_type_into(
                    el, value,
                    allow_typos=allow_typos,
                    familiarity=familiarity,
                    clear_first=True,
                    timeout=timeout,
                )
                if ok:
                    return True
            except Exception:
                continue
        return False

    async def _humanlike_keyboard_into(
        self,
        container_locator,
        value: str,
        *,
        allow_typos: bool = True,
        familiarity: float = 1.0,
        timeout: int = 10000,
        break_after_chars: list[int] | None = None,
    ) -> bool:
        """Click a PAGE-LEVEL container element, then type via Page.keyboard
        with humanlike cadence. Use this when an input lives inside a
        cross-origin iframe (notably Stripe Elements) where `frame_locator`
        chains fight Playwright's actionability checks and cause
        scroll-jumping / visibility timeouts.

        Mechanics: clicking the page-level container puts focus on it. For
        Stripe Elements containers (#cardNumber / #cardCvv / .CardExpireContainer),
        Stripe.js intercepts the click and routes focus into its iframe's
        internal input. Subsequent keystrokes via Page.keyboard go to
        whatever has focus — so they land in Stripe's input without us
        ever entering the iframe via Playwright."""
        base_ms = max(1, settings.cloudbeds_typing_delay_ms)
        do_typos = allow_typos and len(value) >= _MIN_LEN_FOR_TYPOS
        f_clamped = max(0.4, familiarity)

        try:
            await container_locator.wait_for(state="visible", timeout=timeout)
            # force=True skips Playwright's repeat actionability check
            # that was the source of the scroll-jumping when Stripe's
            # iframe re-layouts mid-stabilization.
            await container_locator.click(force=True)
            await asyncio.sleep(random.uniform(0.20, 0.40) * f_clamped)

            for i, c in enumerate(value):
                if do_typos and c.isalnum() and random.random() < _TYPO_RATE:
                    if c.isalpha():
                        wrong = random.choice("abcdefghijklmnopqrstuvwxyz")
                        if c.isupper():
                            wrong = wrong.upper()
                    else:
                        wrong = random.choice("0123456789")
                    await self.page.keyboard.type(wrong, delay=0)
                    await asyncio.sleep(random.uniform(0.18, 0.42))
                    await self.page.keyboard.press("Backspace")
                    await asyncio.sleep(random.uniform(0.08, 0.20))

                # IMPORTANT: Playwright's `type(text, delay=N)` only uses
                # delay BETWEEN chars within one call. For single-char calls,
                # the delay is essentially ignored. So we always pass
                # delay=0 here and impose the inter-keystroke gap with our
                # own asyncio.sleep. Without this, our mixture-distribution
                # cadence wouldn't actually be applied -- the keys would
                # fire as fast as the event loop could schedule them, which
                # is what was breaking Stripe's expiration-field auto-format
                # (race between "1" processing and "2" arrival).
                delay_ms = _keystroke_delay_ms(base_ms, familiarity)
                await self.page.keyboard.type(c, delay=0)
                await asyncio.sleep(delay_ms / 1000.0)

                if c in " .,@-_":
                    await asyncio.sleep(random.uniform(0.02, 0.12))
                if familiarity > 1.2 and (i + 1) % 4 == 0 and i + 1 < len(value):
                    if random.random() < 0.35:
                        await asyncio.sleep(random.uniform(0.25, 0.70))
                # DETERMINISTIC mid-field pause -- specifically for fields
                # that auto-format mid-typing (Stripe expiration inserts "/"
                # after MM; if the next keystroke arrives during that
                # re-focus animation it gets dropped). Callers pass
                # break_after_chars=[2] for "MMYY" to force a deliberate
                # pause matching how a human looks at the YY part of their
                # card after typing the month.
                if (
                    break_after_chars
                    and (i + 1) in break_after_chars
                    and i + 1 < len(value)
                ):
                    await asyncio.sleep(random.uniform(0.40, 0.90))

            base_pause = settings.cloudbeds_action_pause_ms / 1000
            await asyncio.sleep(base_pause * random.uniform(0.75, 1.3))
            return True
        except Exception as ex:
            log.warning("_humanlike_keyboard_into failed: %s", ex)
            return False

    async def _humanlike_type_into(
        self,
        locator,
        value: str,
        *,
        allow_typos: bool = True,
        familiarity: float = 1.0,
        clear_first: bool = True,
        timeout: int = 5000,
        break_after_chars: list[int] | None = None,
    ) -> bool:
        """Type into an already-resolved Playwright Locator with humanlike
        cadence. Use this for iframe-internal targets (e.g.
        `page.frame_locator('#cardNumber iframe').locator('input').first`)
        where a page-level selector can't reach. Same cadence + typo +
        familiarity semantics as `_humanlike_type`."""
        base_ms = max(1, settings.cloudbeds_typing_delay_ms)
        do_typos = allow_typos and len(value) >= _MIN_LEN_FOR_TYPOS
        # Scale the "thinking" pauses (initial + space-after) by familiarity
        # too, but clamp the lower end so bursts of speed don't collapse
        # these pauses to zero.
        f_clamped = max(0.4, familiarity)

        try:
            await locator.wait_for(state="visible", timeout=timeout)
            # Auto-detect masked / hidden fields and force typos off. A real
            # person can't see what they typed in a `type=password` field, so
            # they can't typo-then-backspace -- they just type carefully and
            # accept whatever lands. Doing the typo dance on a masked field
            # is a script tell. Same applies to fields explicitly tagged for
            # password autofill (autocomplete=current-password / new-password).
            try:
                input_type = (await locator.get_attribute("type") or "").lower()
                autocomp = (await locator.get_attribute("autocomplete") or "").lower()
                if input_type == "password" or autocomp in ("current-password", "new-password"):
                    do_typos = False
            except Exception:
                pass
            # Initial "look at the field" pause -- longer when the value is
            # something we need to read off a card vs something we know cold.
            await locator.click()
            await asyncio.sleep(random.uniform(0.12, 0.30) * f_clamped)
            if clear_first:
                # Stripe Elements iframes sometimes refuse programmatic
                # fill(""). Don't let a clear failure abort the typing.
                try:
                    await locator.fill("")
                except Exception:
                    pass
                await asyncio.sleep(random.uniform(0.04, 0.10) * f_clamped)

            for i, c in enumerate(value):
                if do_typos and c.isalnum() and random.random() < _TYPO_RATE:
                    # Wrong char of the same kind (letters -> letters,
                    # digits -> digits). Fat-finger pattern, not random key.
                    if c.isalpha():
                        wrong = random.choice("abcdefghijklmnopqrstuvwxyz")
                        if c.isupper():
                            wrong = wrong.upper()
                    else:
                        wrong = random.choice("0123456789")
                    await locator.type(wrong, delay=0)
                    await asyncio.sleep(random.uniform(0.18, 0.42))
                    await locator.press("Backspace")
                    await asyncio.sleep(random.uniform(0.08, 0.20))

                # Same caveat as _humanlike_keyboard_into: `type(text, delay=N)`
                # only applies delay BETWEEN chars in one call, not within a
                # single-char call. Always pass delay=0 here and impose the
                # inter-keystroke gap with asyncio.sleep so the mixture
                # distribution actually drives timing.
                delay_ms = _keystroke_delay_ms(base_ms, familiarity)
                await locator.type(c, delay=0)
                await asyncio.sleep(delay_ms / 1000.0)

                # Micro-pause after a space or punctuation -- the eye
                # tends to dwell at word boundaries.
                if c in " .,@-_":
                    await asyncio.sleep(random.uniform(0.02, 0.12))
                # For unfamiliar data (card number etc.), ~35% chance of a
                # "look back at the card" pause after every 4-char block.
                if familiarity > 1.2 and (i + 1) % 4 == 0 and i + 1 < len(value):
                    if random.random() < 0.35:
                        await asyncio.sleep(random.uniform(0.25, 0.70))
                # Deterministic mid-field pause for auto-formatting inputs
                # (see _humanlike_keyboard_into for the rationale -- Stripe
                # expiration "/" insertion is the canonical case).
                if (
                    break_after_chars
                    and (i + 1) in break_after_chars
                    and i + 1 < len(value)
                ):
                    await asyncio.sleep(random.uniform(0.40, 0.90))

            # Post-typing review pause before whatever's next.
            base_pause = settings.cloudbeds_action_pause_ms / 1000
            await asyncio.sleep(base_pause * random.uniform(0.75, 1.3))
            return True
        except Exception as ex:
            log.warning("_humanlike_type_into failed: %s", ex)
            return False

    async def _pause(self, label: str = "") -> None:
        """Insert a humanlike pause between major steps. Called after
        clicks that trigger navigation or DOM updates."""
        await asyncio.sleep(settings.cloudbeds_action_pause_ms / 1000)
        if label:
            log.debug("Cloudbeds pause [%s]", label)

    async def _handle_okta_2fa(self) -> bool:
        """After password submit, Okta may prompt for 2FA. Several flavors:
          (a) TOTP input already on the page (last-used factor remembered)
          (b) Factor picker page ('Select from the following options')
              with a 'Select' button next to each factor's label
          (c) Push-default page with 'Verify with something else' link

        We try (a), then (b), then (c). In all cases we end up with the
        TOTP input visible and enter a fresh 6-digit code from pyotp.

        Returns True if we posted a TOTP, False if we couldn't navigate
        to the TOTP entry step."""
        totp = _current_totp()
        if not totp:
            log.warning("Okta 2FA prompted but CLOUDBEDS_TOTP_SECRET is unset; can't proceed")
            await _capture_screenshot(self.page, "2fa_no_secret")
            return False

        # (a) TOTP input directly visible? (Okta remembered our last factor)
        try:
            await self.page.locator(SEL_2FA_TOTP_INPUT).first.wait_for(state="visible", timeout=3000)
            log.info("Okta 2FA: TOTP input visible, entering code directly")
            await self._checkpoint("2fa_totp_direct")
            return await self._submit_totp_and_verify(totp)
        except Exception:
            pass

        # (b) Factor picker page: "Select from the following options"
        try:
            await self.page.locator(SEL_2FA_FACTOR_PICKER_MARKER).first.wait_for(
                state="visible", timeout=3000,
            )
            log.info("Okta 2FA: on factor picker page, clicking Google Authenticator")
            await self._checkpoint("2fa_picker_visible")
            # The Select link next to the Google Authenticator label.
            clicked = await self._click_first_visible(
                SEL_2FA_PICK_GOOGLE_AUTH_PRIMARY,
                SEL_2FA_PICK_GOOGLE_AUTH_FALLBACK,
                timeout=5000,
            )
            if not clicked:
                log.warning("Couldn't click Google Authenticator 'Select' button")
                await _capture_screenshot(self.page, "2fa_picker_no_ga_button")
                return False
            await self._checkpoint("2fa_clicked_ga_select")
            # Now wait for the TOTP input on the next page
            try:
                await self.page.locator(SEL_2FA_TOTP_INPUT).first.wait_for(
                    state="visible", timeout=10000,
                )
            except Exception:
                log.warning("Picked Google Authenticator but TOTP input never appeared")
                await _capture_screenshot(self.page, "2fa_ga_picked_no_input")
                return False
            await self._checkpoint("2fa_totp_input_visible")
            return await self._submit_totp_and_verify(totp)
        except Exception:
            pass

        # (c) Legacy "Verify with something else" link path
        clicked = await self._click_first_visible(SEL_2FA_CHOOSE_DIFFERENT, timeout=5000)
        if clicked:
            await self._click_first_visible(
                SEL_2FA_PICK_GOOGLE_AUTH_PRIMARY, SEL_2FA_PICK_GOOGLE_AUTH_FALLBACK,
                timeout=10000,
            )
        if not await self._fill_first_visible(totp, SEL_2FA_TOTP_INPUT, timeout=10000):
            log.warning("Okta 2FA: couldn't reach TOTP input via any known path")
            await _capture_screenshot(self.page, "2fa_no_totp_input")
            return False
        return await self._submit_totp_and_verify(totp)

    async def _submit_totp_and_verify(self, prepared_code: str | None = None) -> bool:
        """Fill in a fresh TOTP code, click Verify, and confirm the page
        actually navigated AWAY from the TOTP entry step. Returns True
        only if the URL changed or the form was clearly accepted. Failure
        returns False (with screenshot + log) instead of optimistically
        claiming success.

        Why this matters: Okta's TOTP page sometimes silently rejects
        codes (clock skew, replay) and the form stays put. If we just
        return True after click(), the dashboard wait spins for 30s
        before timing out -- not actionable. Catching it here gives us a
        concrete error point + screenshot."""
        url_before = self.page.url
        # ALWAYS regenerate -- codes rotate every 30s.
        code = _current_totp() or prepared_code
        if not code:
            log.warning("No TOTP code available to submit")
            return False

        # Humanlike typing -- TOTP entry is the most common place Okta
        # bot-blocks instant-fill input. allow_typos=False: the code is
        # 6 digits on a 30s rotation; a typo-and-correct cycle eats into
        # the window and is overkill for a numeric field.
        await self._humanlike_type(code, SEL_2FA_TOTP_INPUT, allow_typos=False)
        await self._checkpoint("2fa_totp_filled")
        # Try the labeled Verify button first; fall back to pressing Enter
        # inside the TOTP input (standard HTML form submission). The Enter
        # path is robust against Okta button-markup changes that have
        # broken us before -- works as long as Okta marks the input as
        # type=submit / part of a real <form>.
        clicked = await self._click_first_visible(
            SEL_2FA_VERIFY, SEL_LOGIN_NEXT, timeout=3000,
        )
        if not clicked:
            log.info("Verify button selector missed -- pressing Enter on TOTP input as fallback")
            try:
                await self.page.locator(SEL_2FA_TOTP_INPUT).first.press("Enter")
            except Exception as ex:
                log.warning("Couldn't press Enter on TOTP input: %s", ex)

        # Wait for the page to ACT on the submission: URL change OR error
        # text appearing OR navigation. If none of those happen in 15s, the
        # Verify button probably didn't fire (wrong selector) or the form
        # is stuck (network issue). Either way, real diagnostic > silent OK.
        try:
            await self.page.wait_for_function(
                f"""() => {{
                    if (location.href !== {url_before!r}) return 'navigated';
                    const t = (document.body.innerText || '').toLowerCase();
                    if (t.includes('invalid') || t.includes('incorrect')
                        || t.includes('does not match') || t.includes("doesn't match"))
                        return 'rejected';
                    return null;
                }}""",
                timeout=15000,
            )
        except Exception:
            log.warning("Verify click had no effect within 15s (selector miss?)")
            await _capture_screenshot(self.page, "2fa_verify_no_effect")
            return False
        await self._checkpoint("2fa_after_verify")

        # If the URL is still on signin/auth, the TOTP was likely rejected.
        # Don't lie about success.
        host = (await self.page.evaluate("location.hostname")).lower()
        if host.startswith("signin.") or host.startswith("auth."):
            # Did Okta show an error? Capture for diagnosis.
            txt = (await self.page.evaluate("document.body.innerText") or "").lower()
            if "invalid" in txt or "incorrect" in txt or "match" in txt:
                log.warning("Okta rejected the TOTP (page shows error text)")
                await _capture_screenshot(self.page, "2fa_totp_rejected")
                return False
            # Else: we navigated but stayed on the auth domain -- could be
            # OAuth flow still in progress. Let the dashboard wait take it.
        return True

    async def login(self) -> bool:
        """Submit credentials + 2FA. Returns True on success, False on
        failure (caller decides whether to alert).

        Mimics what a real staffer does: type the dashboard URL into the
        address bar, let Cloudbeds redirect to signin if needed. Flow:
          1. Open hotels.cloudbeds.com/connect/<prop>#/reservations
          2. If still on cloudbeds.com (non-signin host) -> we're in
          3. Otherwise Cloudbeds bounced us to Okta signin:
             a. Enter email -> Next
             b. Enter password -> Verify / Next
             c. 2FA challenge (default push; we switch to TOTP)
             d. Redirects back to the dashboard"""
        if not settings.cloudbeds_admin_email or not settings.cloudbeds_admin_password:
            log.warning("Cloudbeds admin credentials not configured; skipping login")
            return False

        # Start at the dashboard URL a human would type. If our stored
        # session is good, this lands us straight on the dashboard with
        # no Okta prompts at all -- exactly what a returning staffer
        # sees. Otherwise Cloudbeds itself redirects us through Okta.
        try:
            await self.page.goto(_entry_url(), wait_until="domcontentloaded", timeout=20000)
        except Exception as ex:
            log.warning("Initial navigation to dashboard URL failed: %s", ex)
            # Fall back to the signin URL directly -- worst case we still
            # complete the credential flow.
            try:
                await self.page.goto(
                    settings.cloudbeds_login_url,
                    wait_until="domcontentloaded", timeout=20000,
                )
            except Exception:
                log.exception("Cloudbeds: couldn't reach signin URL either")
                await _capture_screenshot(self.page, "initial_nav_failed")
                return False

        # Let any client-side redirect to signin / Okta complete before
        # we evaluate where we are.
        try:
            await self.page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        await self._pause("post-entry-nav")
        await self._checkpoint("entry_url_loaded")

        # Did Cloudbeds let us straight in? (cached session, remembered
        # device, etc.) If so, no credentials needed -- skip to the end.
        try:
            host = (await self.page.evaluate("location.hostname")).lower()
        except Exception:
            host = ""
        if (
            host.endswith("cloudbeds.com")
            and not host.startswith("signin.")
            and not host.startswith("auth.")
        ):
            log.info("Cloudbeds: dashboard reached without credentials (cached session). url=%s",
                     self.page.url)
            return True

        try:
            # Step 1: email. Use humanlike typing -- Okta's bot detection
            # treats instant fill() as scripted and may silently reject.
            await self._humanlike_type(
                settings.cloudbeds_admin_email, SEL_LOGIN_EMAIL, timeout=10000,
            )
            await self._click_first_visible(SEL_LOGIN_NEXT, timeout=5000)
            await self._pause("post-email")
            await self._checkpoint("after_email_submit")

            # Step 2: password
            await self._humanlike_type(
                settings.cloudbeds_admin_password, SEL_LOGIN_PASSWORD, timeout=15000,
            )
            await self._click_first_visible(SEL_LOGIN_NEXT, timeout=5000)
            await self._pause("post-password")
            await self._checkpoint("after_password_submit")

            # Step 3: figure out what page we landed on after password+Next.
            # Could be: TOTP input, factor picker, "different way" link,
            # or already-on-the-dashboard (remembered device). Single poll
            # so we don't burn time waiting separately for each.
            POST_PASSWORD_STATE_JS = """
            () => {
                const h = location.hostname.toLowerCase();
                // Final dashboard? Any cloudbeds.com host that isn't the
                // sign-in (signin.*) or OAuth-broker (auth.*) host.
                if (h.endsWith('cloudbeds.com')
                    && !h.startsWith('signin.')
                    && !h.startsWith('auth.'))
                    return 'dashboard';
                // Specific 2FA states (text content checks are cheaper +
                // more stable than CSS selectors here).
                const txt = document.body.innerText || '';
                if (document.querySelector(
                        "input[name='credentials.passcode'], "
                        + "input[name='answer'], "
                        + "input[autocomplete='one-time-code']"))
                    return 'totp';
                if (txt.includes('Select from the following options'))
                    return 'picker';
                if (txt.toLowerCase().includes('verify with something else')
                    || txt.toLowerCase().includes('try another way'))
                    return 'choose_diff';
                return null;
            }
            """
            try:
                state = await self.page.wait_for_function(
                    POST_PASSWORD_STATE_JS, timeout=30000,
                )
                state_value = await state.json_value()
            except Exception as ex:
                log.warning("Cloudbeds: couldn't determine post-password state: %s", ex)
                await _capture_screenshot(self.page, "post_password_unknown")
                return False
            log.info("Cloudbeds: post-password state = %r (url=%s)",
                     state_value, self.page.url)
            if state_value != "dashboard":
                # Any of {totp, picker, choose_diff} -> hand off to 2FA handler
                ok = await self._handle_okta_2fa()
                if not ok:
                    return False

            # Step 4: confirm final landing on the dashboard. Strict check --
            # the URL must NOT be on signin.cloudbeds.com or auth.cloudbeds.com
            # (those are intermediate OAuth steps, not the dashboard).
            FINAL_DASHBOARD_JS = """
            () => {
                const h = location.hostname.toLowerCase();
                return h.endsWith('cloudbeds.com')
                    && !h.startsWith('signin.')
                    && !h.startsWith('auth.');
            }
            """
            await self.page.wait_for_function(FINAL_DASHBOARD_JS, timeout=30000)
            log.info("Cloudbeds: login successful as %s -> %s",
                     settings.cloudbeds_admin_email, self.page.url)
            return True
        except Exception as ex:
            log.exception("Cloudbeds: login failed")
            await _capture_screenshot(self.page, "login")
            await _send_failure_alert(f"Login failed: {ex!s}")
            return False

    async def generate_pay_by_link_for_reservation(
        self,
        reservation_id: str,
        amount_cents: int,
        description: str,
    ) -> dict:
        """STUB until Phase 2: click through Cloudbeds dashboard to create
        a Pay-by-Link for the given reservation, return the URL + expiry.

        Returns {"success": True, "url": "...", "expires_at": datetime} or
        {"success": False, "error": "..."}.

        Phase 2 will replace the NotImplementedError with selector-driven
        steps captured via `playwright codegen` against the live UI."""
        raise NotImplementedError(
            "Pay-by-Link UI automation selectors are not yet wired up. "
            "Run Phase 2 (selector discovery) before calling this."
        )


async def generate_pay_by_link(
    reservation_id: str,
    *,
    amount_cents: int | None = None,
    description: str | None = None,
    client_ip: str | None = None,
) -> dict:
    """Top-level entry point: log in, generate the link, return the URL.
    Handles the full failure protocol (screenshot + SMS + graceful return).

    Returns {"success": True, "url": "...", "expires_at": dt} or
    {"success": False, "error": "..."}. Caller (the portal POST handler)
    decides how to surface to the guest.
    """
    amount = amount_cents if amount_cents is not None else settings.cloudbeds_paylink_amount_cents
    desc = description or settings.cloudbeds_paylink_description

    async with _session_lock:  # serialize concurrent requests against one browser
        try:
            async with CloudbedsBrowser() as cb:
                if not await cb.login():
                    return {"success": False, "error": "Cloudbeds login failed."}
                return await cb.generate_pay_by_link_for_reservation(
                    reservation_id, amount, desc,
                )
        except NotImplementedError as ex:
            # Expected during Phase 1 -- not a real failure, don't SMS.
            log.warning("Cloudbeds automation: %s", ex)
            return {"success": False, "error": "Card-link automation is being set up. Please call the front desk."}
        except Exception as ex:
            log.exception("Cloudbeds automation: unexpected error")
            await _send_failure_alert(f"Unexpected: {ex!s}", reservation_id=reservation_id)
            return {"success": False, "error": "We couldn't generate the card link right now. Please call the front desk."}
