# PCI runbook — in-call DTMF card capture

This document describes the controls around Iris's `capture_card_dtmf` flow.
It is the reference for staff training, periodic reviews, and any
post-incident analysis. Updated whenever the flow's components change.

## Architecture summary

```
Caller's phone keypad
   ↓ (DTMF tones over the PSTN)
Twilio Elastic SIP Trunk
   ↓ (RFC 4733 RTP events + audio over SIP/RTP)
LiveKit SIP container
   ↓ (sip_dtmf_received room event + audio track — track muted via admin API
      during capture, so neither STT nor egress sees the audio)
Iris agent process
   ↓ (PAN+exp+CVV assembled in local variables; never logged, never persisted)
HTTPS POST → api.stripe.com/v1/tokens (publishable-key Basic auth)
   ↓ (Stripe returns tok_xxx + token_card metadata)
Iris agent — IMMEDIATE SCRUB: PAN and CVV variables overwritten with ""
   ↓ (POST {reservation_id, token_id, token_card} → backend)
backend /tools/save_card_via_token
   ↓ (cloudbeds_dashboard.get_booking_id, then dashboard_save_credit_card)
hotels.cloudbeds.com/hotel/save_credit_card
   ↓ (Cloudbeds attaches the card to the reservation using their own
      Stripe Connect account acct_1RYA4xEJ572tmEoR)
Card on file — chargeable by hotel staff via Cloudbeds UI
```

## Where raw card data exists

| Location | Form | Duration |
|---|---|---|
| Caller's keypad → Twilio RTP | DTMF events + audio tones | Real-time |
| LiveKit SIP container | RTP frames in flight | Real-time (audio dropped on muted track) |
| Iris agent — `pan_local` / `cvc_local` / `buffers["pan"]` / `buffers["cvc"]` local variables in `capture_card_dtmf` | Plaintext PAN/CVC | ≤ ~10 seconds (from collection through Stripe tokenize) |
| HTTPS body to api.stripe.com/v1/tokens | TLS-encrypted in transit | Single request |
| Stripe Connect account `acct_1RYA4xEJ572tmEoR` | Stripe's vault | Indefinite (out of our PCI scope per Stripe SAQ A) |

**The Iris agent process is the only place in our infrastructure where raw
PAN/CVC briefly exist in plaintext.** This places us in SAQ A-EP scope for
that process.

## Controls

### C1. Audio path control
- **What**: caller's audio track is muted via `RoomService.MutePublishedTrack(muted=True)` BEFORE the first DTMF prompt and unmuted only after capture completes (success or failure).
- **Why**: DTMF tones leak ~50ms per first-press into the audio track in our Twilio/LiveKit setup (confirmed via `detect_dtmf_in_ogg.py` 5/28). Muting at the LiveKit server boundary blocks all audio frames from the caller's published track from reaching subscribers — STT, egress recording, and Iris's session input. DTMF arrives on a separate signaling channel (`sip_dtmf_received` room event) and is unaffected by the mute.
- **Verification**: `admin_dtmf_mute_test` tool + `backend/scripts/detect_dtmf_in_ogg.py` confirmed both halves on 5/28.
- **Failure mode**: if `mute_published_track(True)` fails, `capture_card_dtmf` refuses to start and returns `status: error`. The caller is not asked for card data.

### C2. STT pause + LLM gating
- **What**: during capture, `session.input.set_audio_enabled(False)` is set, and `self._silent = True` causes `on_user_turn_completed` to raise `StopResponse`. Both run as belt-and-suspenders behind the audio mute.
- **Why**: even if a frame slipped past the mute, neither STT nor the LLM would process it.
- **Verification**: existing self-test (silent-mode flow), no change needed.

### C3. Ephemeral PAN/CVC handling
- **What**: PAN and CVC live in local variables in `capture_card_dtmf` only. The variables are overwritten with `""` immediately after the Stripe tokenize call returns, in both success and failure paths. The orchestrator's `try/finally` block guarantees the scrub runs even on exception.
- **Why**: minimize the residence time of raw card data.
- **Verification**: code inspection of the `capture_card_dtmf` function in `agent/iris_agent.py`.
- **Failure mode**: if a Python exception escapes the `finally`, the worker subprocess restarts on the next call — locals are discarded with the process.

### C4. No logging of raw card data
- **What**: `pan`, `cvc`, `card[number]`, `card[cvc]`, and the buffers are never passed to `log.info` / `log.warning` / `log.exception` / `print`. The Stripe error-response object is logged only by `code` and `message`, never the full `body` (Stripe sometimes echoes back a `param` field referencing which card field was bad — never the value).
- **Why**: log files persist longer than the in-memory PAN.
- **Verification**: grep for `log.*pan|log.*cvc|log.*card\[number\]|log.*cvc_local|log.*pan_local` should return no matches in iris_agent.py. The Stripe error logging path is single-line and constrained to `code` + `msg`.

### C5. No raw card data in backend or Cloudbeds messages
- **What**: the agent → backend HTTP call (`/tools/save_card_via_token`) sends only `reservation_id`, `token_id`, and `token_card` (Stripe's response metadata: brand, last4, exp month/year, AVS checks, etc., but NOT the PAN). The backend's interaction with `hotels.cloudbeds.com/hotel/save_credit_card` likewise sends only the token, never raw card material.
- **Why**: the backend and Cloudbeds dashboard are out of our PAN-handling scope.
- **Verification**: code inspection of `_handle_save_card_via_token` in `backend/app/routes/vapi_tools.py` and `dashboard_save_credit_card` in `backend/app/tools/cloudbeds_dashboard.py`.

### C6. Network-encrypted in-transit
- **What**: the Stripe POST is to `https://api.stripe.com/v1/tokens` (TLS). The agent → backend call is over the local network on the droplet (or localhost). The backend → Cloudbeds call is to `https://hotels.cloudbeds.com/...` (TLS).
- **Verification**: hardcoded HTTPS schemes; `httpx.AsyncClient` defaults to verifying TLS.

### C7. Restricted invocation
- **What**: `capture_card_dtmf` only runs when the LLM invokes it via the function-tool dispatch. The system prompt restricts that to Stage 10.5 — only after explicit caller consent at Stage 8 to the in-call card-capture path — and limits to one retry on `declined`, zero retries on other failure modes.
- **Verification**: `[Booking Flow Rules]` Stage 8 and Stage 10.5 of the system prompt. The function-tool docstring also explicitly forbids loop-on-error.

### C8. Cloudbeds tokenization key is public
- **What**: the `_CLOUDBEDS_PLATFORM_PK` constant in `iris_agent.py` is a Stripe publishable key (prefix `pk_live_`). Publishable keys are designed to be embedded in client-side code; they let callers create tokens but cannot move money, retrieve customer data, or sign API requests. The corresponding Stripe SECRET key is held by Cloudbeds (the platform) and never touches our infrastructure.
- **Verification**: key prefix is `pk_live_`. Stripe's API docs.

## What to do if you suspect a compromise

1. **Rotate immediately**:
   - Cloudbeds API key (backend `.env` `CLOUDBEDS_API_KEY`)
   - Cloudbeds session cookies (force a re-login via the Playwright bootstrap)
   - Anthropic API key (if you think it leaked from another vector)
   - LiveKit API key + secret (`LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET`)
2. **Tail the call recordings** in `/opt/iris-backend/recordings/`: run `backend/scripts/detect_dtmf_in_ogg.py` against every OGG from suspected calls to confirm no DTMF tones leaked.
3. **Inspect agent + backend logs**: `journalctl -u iris-agent` and `journalctl -u iris-backend` for the time window. Look for any logged token IDs, any `error` lines referencing tokenize or save_card, and any unusual outbound HTTP destinations.
4. **Notify Stripe and Cloudbeds**. The publishable key on our side cannot move money, but a compromise of the host could imply broader exposure that they'll want to know about.
5. **File a postmortem** in this directory.

## Change log

- 2026-05-28: initial document; written alongside the task #12 build.
