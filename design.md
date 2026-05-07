# AI Reservation Agent — Design Doc

A voice AI receptionist for a single hotel property, replacing the Hey Sadie service. Design goals are driven by the three pain points the user reported with Sadie: speech-to-text accuracy, no caller-ID authentication of returning guests, and inability to edit the conversation script directly (script changes require a Sadie support ticket).

---

## Goals

- Replace Hey Sadie at lower or comparable cost
- **Better speech-to-text** — Hey Sadie's main weakness; guests have to spell things repeatedly
- **Caller-ID authentication** — recognize returning guests against Cloudbeds and personalize the call
- **Direct script control** — system prompt is a file the user/Claude can edit, no support tickets
- **Outbound calling** with the hotel number as caller-ID (e.g. "your room is ready")
- **Block junk callers** with admin and AI-driven moderation
- **Admin-by-phone** — owner can call the hotel number from inside the hotel and issue commands

## Non-goals (v1)

- Multi-property support
- SMS / written-message handling
- Integrations beyond Cloudbeds

---

## Architecture

### Components

| Component | Role |
|---|---|
| **Twilio** | Carrier-of-record for the hotel phone number. Owns the number after porting. Provides PSTN connectivity and SIP endpoints. BYOT (Bring Your Own Twilio) — user owns the Twilio account directly so numbers stay portable if Vapi is ever replaced. |
| **Vapi** | AI voice orchestration. Handles STT, LLM, TTS, tool calls, transfers. Built on top of Twilio. |
| **Webhook backend** | Locally-hosted server (~$5/mo VPS, e.g. DigitalOcean or Hetzner). Exposes Cloudbeds tool endpoints to Vapi, performs caller-ID authentication, manages the junk-caller block list, processes admin commands. |
| **Cloudbeds API** | Source of truth for reservations. User has API credentials. Prior work in `CloudbedsAPI-2026` etc. may be reusable. |
| **ATA (Grandstream HT802)** | ~$60–70 one-time. 2 FXS ports (only 1 in use today; second available for future analog line). Connects the existing Panasonic KX-TGD864W cordless phones to Twilio via SIP, replacing Hyak's phone service. Existing handsets and user experience are unchanged. |

### Final phone routing (post-port, post-ATA)

```
Inbound caller
      ↓ (PSTN)
   Twilio  ────  hotel number lives here
      ↓ (SIP)
    Vapi  ────  AI agent answers
      ↓
  ┌───┴────────────────────────────┐
  ↓ (tool call)                    ↓ (transfer)
Webhook backend (VPS)        SIP endpoint: ATA
      ↓                            ↓ (RJ-11)
  Cloudbeds API              Panasonic KX-TGD864W
                                   ↓
                             Cordless handsets ring


Outbound from Vapi (e.g. "your room is ready"):
   Webhook  ──→  Vapi API  ──→  Twilio  ──→  guest's phone
                                              (caller-ID = hotel number)

Outbound from front desk:
   Panasonic  ──→  ATA  ──→  Twilio  ──→  guest's phone
                                          (caller-ID = hotel number)
```

### Test phase routing (pre-port, no disruption to live service)

```
Hotel number (still on Hyak — unchanged)
      ↓ (forwarded, like today's Hey Sadie forwarding)
Temporary Twilio test number ($1.15/mo)
      ↓
   Vapi
      ↓ (transfer destination during testing)
Staff cell phone
      (avoids the loop that would happen if Vapi tried
       to transfer back to the still-forwarded hotel number)
```

---

## Features

### Inbound — core

- AI agent answers calls
- Verbal Q&A about the hotel: hours, amenities, location, FAQs
- Transfer to front desk on request or detected need. Transfer goes to the ATA's SIP endpoint — no separate phone number required. Caller-ID of the original caller passes through to the front-desk handsets.

### Reservations

- Create new reservations via Cloudbeds API
- **Caller-ID lookup**: when an inbound call arrives, the backend queries Cloudbeds for any reservation whose phone number matches. If found, the agent greets the guest by name and can answer questions about their stay (dates, room type, balance, etc.) without needing to verify identity verbally.

### Payment capture

The hotel uses **Cloudbeds Payments (Stripe under the hood)** today. The AI captures payment in a way that keeps card data out of our infrastructure (zero PCI scope) while supporting any caller, including those on flip phones or in transit.

**Design constraint**: avoid requiring a second device. A caller on a flip phone, or driving, or otherwise unable to act on a smartphone link must still be able to complete payment in the same call.

**Primary path: Twilio Pay with DTMF keypad entry**

- AI invokes Twilio's `<Pay>` TwiML capability mid-conversation
- Caller types card number, expiration, CVV on the phone keypad
- Card data goes through Twilio's PCI-compliant pipe directly to Stripe — never touches our webhook, never appears in audio recording, never enters transcripts
- Twilio Pay Connector configured for **Stripe**, pointed at the **same Stripe merchant account Cloudbeds Payments already uses** — phone-captured payments appear in the same Stripe dashboard as Cloudbeds-initiated ones, no parallel-processor reconciliation needed
- Cost: $0.10 per successful transaction, $0 for failures, plus standard Stripe processor fees (the same fees the hotel already pays)

**Two reservation workflows mirror existing hotel policy:**

The hotel's policy distinguishes by arrival date:
- **Same-day arrival** → charge the card immediately
- **Future arrival** → store card on file, hotel staff charges it the morning of arrival from the Cloudbeds UI

Both are supported via Cloudbeds' `postReservation` endpoint, which accepts a `cardToken` parameter (must be a **Stripe Customer ID** with an attached Payment Method) and an optional `paymentAuthorizationCode` (Stripe Charge ID).

*Same-day flow*:
1. Twilio Pay captures card via DTMF → Stripe returns PaymentMethod ID
2. Backend creates Stripe Customer, attaches PaymentMethod, creates Charge for the deposit/full amount
3. Backend calls Cloudbeds `postReservation(cardToken=customer_id, paymentAuthorizationCode=charge_id, ...)` — reservation created with payment recorded
4. AI confirms to caller: "Reservation confirmed and card charged $X."

*Future-arrival flow*:
1. Twilio Pay captures card via DTMF → Stripe returns PaymentMethod ID
2. Backend creates Stripe Customer, attaches PaymentMethod (no charge)
3. Backend calls Cloudbeds `postReservation(cardToken=customer_id, ...)` — reservation created with card on file
4. Hotel staff sees card on reservation in Cloudbeds UI exactly as if entered manually
5. Staff charges from Cloudbeds UI on arrival morning, existing workflow unchanged

The AI determines which flow to use based on arrival date.

**Important Cloudbeds caveat**: deposit rules configured in Cloudbeds' UI (Manage > Payment Options > Processing Methods) **are NOT applied to reservations created via the API**. The webhook backend must know the deposit/charging policy explicitly and execute it at step 2 of the same-day flow — this is our responsibility, not a Cloudbeds setting we can lean on. Source: [Cloudbeds: Pass Stripe tokens to Cloudbeds](https://developers.cloudbeds.com/docs/pass-stripe-tokens-to-cloudbeds).

**Token-on-existing-reservation limitation**: per Cloudbeds docs, only `postReservation` accepts the `cardToken` parameter — there's no documented way to attach a card to an existing Cloudbeds reservation later. This means the card capture happens in the same conversation as the reservation creation. If a guest needs to update their card later, the most likely path is the Cloudbeds payment-link fallback (sent to the guest, they update card via web).

**Fallback path: SMS payment link via Cloudbeds**

Triggered when DTMF fails repeatedly (~2–3 retries) or if the caller proactively asks ("can you just text me a link?"):

- Webhook calls Cloudbeds API to generate a payment link for the reservation
- Webhook sends the link via Twilio SMS to the caller's number
- AI confirms the link was sent and offers to wait or end the call
- Caller completes payment on Cloudbeds-hosted page when convenient

**Why both paths**: DTMF is the smooth default (single device, single call). SMS link is a graceful escape for callers whose keypad entry isn't working or who'd rather complete payment later. Keeping both available means we don't lose any reservation due to payment friction.

**Caller experience for DTMF**:
> *AI: "I can take the card on file now. You'll enter it on your phone's keypad — your card numbers are never recorded or heard by me. If you're driving, please pull over or have a passenger help. Ready when you are: please enter the 16-digit card number followed by the pound key."*
> *(caller types digits)*
> *AI: "Got it. Now the expiration date as MMYY followed by pound."*
> *(caller types digits)*
> *AI: "And the security code from the back of the card."*
> *(caller types digits)*
> *AI: "One moment... Approved. Your reservation is confirmed."*

**Cost impact at our volume**: ~5–10 phone-captured payments/month → **~$2–3/month additional** (Twilio Pay fees + small voice-minute extension during entry). Stripe processor fees are unchanged from current operations.

**Two things to verify when wiring this up**:
1. Whether Vapi supports inserting `<Pay>` TwiML inline as a tool call, or whether the call needs a brief handoff to a TwiML script for the payment portion and back to Vapi after.
2. Exact Cloudbeds API endpoint for posting an external payment to a reservation (so the Stripe charge captured via Twilio Pay appears on the reservation correctly).

### Outbound calling

- Triggered from the webhook backend (or a small admin UI) via Vapi's REST API
- Use cases: "your room is ready," pre-arrival confirmation, post-stay follow-up, payment-failure follow-up, no-show confirmation
- Caller-ID = hotel number (because Twilio is carrier-of-record for it)
- Voicemail detection with an alternate recorded message ("Hi, this is the [Hotel Name] — your room is ready whenever you'd like to check in.")
- Dynamic context (guest name, room number, etc.) injected into the prompt at call time

### Dynamic call routing

Time-bound call routing is something the user reports is also a Hey Sadie feature; this section documents how our implementation works. Triggered by admin-by-phone commands (see next section), but the underlying state machine and pre-call decision live here.

**State model** — kept in the webhook backend's database:

| Field | Values | Purpose |
|---|---|---|
| `mode` | `ai_handle` (default) \| `forward` \| `voicemail` | What to do with inbound calls right now |
| `destination` | phone number or SIP URI | Used only when `mode = forward` |
| `expires_at` | timestamp (or `null` for indefinite) | Auto-reverts to `ai_handle` after this — see below |
| `fallback_on_no_answer` | `ai_handle` (default) \| `voicemail` \| `another_number` | Where to send the call if the forward destination doesn't pick up within the timeout |
| `set_by` | who/which command set the current state | Audit trail |

**Pre-call routing check** — every inbound call hits the webhook *before* Vapi:

```
inbound call arrives at Twilio
    ↓
webhook /incoming-call endpoint
    ↓
1. Is caller on block list?       → return TwiML <Hangup> (silent)
2. Has expires_at passed?         → revert mode to ai_handle (then proceed)
3. mode = ai_handle?              → return TwiML bridging to Vapi (default path)
4. mode = forward?                → return TwiML <Dial timeout="20"> destination </Dial>
                                       on no-answer: route per fallback_on_no_answer
5. mode = voicemail?              → return TwiML playing greeting + <Record>
```

In `forward` mode, **Vapi is never invoked** for that call. No AI cost or latency burned; the call routes in tens of milliseconds. This matters for cost too: forwarding to the front desk for an hour costs ~$0 in Vapi/STT/LLM/TTS for that hour.

**Admin tools the agent can call:**

| Tool | What it does |
|---|---|
| `set_call_routing(mode, destination?, duration?, fallback?)` | Updates the state. `duration` is required when setting `forward` or `voicemail` modes (we deliberately don't allow indefinite forwards by default — see auto-revert below). |
| `get_call_routing()` | Reads back the current state for confirmation ("Right now I'm forwarding to your cell until 3:42 PM, with fallback to voicemail.") |
| `clear_call_routing()` | Reverts to `ai_handle` immediately. |

**Auto-revert** is a deliberate safety feature. The most common failure mode of forwarding is: someone sets it for "a while," walks away, and forgets. By default every forward command requires a duration, the agent reads back the auto-revert time, and the system reverts on its own. Indefinite forwards are possible but require explicit confirmation from the admin (`"Yes, leave it forwarding indefinitely until I say otherwise"`).

**Fallback on no-answer**: when forwarding to a cell phone and you miss the call, the system rings the destination for 20 seconds (configurable) and then takes the fallback path. Default fallback is `ai_handle` — the AI catches the call and handles it normally. This means a missed forward never drops the caller; they just get the AI as a backup.

**Recurring schedules** (later, not v1): the same state model supports scheduled entries — *"every night between 11 PM and 7 AM, mode = voicemail"* — by storing them as repeating rules and applying the matching one at call time. Defer to v2.

### Admin-by-phone

The owner/manager calls the hotel's own number from inside the hotel and issues admin commands.

**Authentication**: the ATA is configured to attach a custom SIP header (e.g. `X-Hotel-Auth: <shared-secret>`) on every outbound INVITE. Vapi forwards SIP headers to our webhook (verified — Vapi exposes `From:`, `P-Asserted-Identity:`, and arbitrary `X-*` headers in the assistant message payload). The webhook verifies the shared secret before honoring admin commands. **This is unspoofable**: the secret never leaves the hotel network, and caller-ID-spoofed calls from outside don't traverse the ATA so they don't carry the header.

Note: an earlier draft of this design relied on STIR/SHAKEN attestation level as a secondary check. Research found Vapi does not document the attestation level in its webhook payload (only caller-ID and SIP headers). Twilio does expose it as `StirVerstat` at the TwiML layer if we ever route through TwiML before handing to Vapi, but for the admin-auth use case the custom-header check is simpler and stronger.

Layered defenses:
1. **Custom SIP header check** (primary, free, unspoofable) — webhook verifies the shared secret
2. **Spoken PIN** (optional, paranoid) — "say your admin code" before commands take effect

For a single property at low call volume, layer #1 alone is sufficient.

**Commands:**

- "Forward all calls to [cell number]" — vacation, off-hours, AI maintenance
- "Send everything to voicemail"
- "Resume normal answering"
- "What's the occupancy today?" / "Read me today's check-ins"
- "How many of today's reservations haven't checked-in?"
- "Don't transfer to the front desk for the next two hours" (busy / event mode)
- "Block this number" (after a problem caller)
- "Unblock [number]"
- "Show me / read me the block list"

### Junk caller blocking

- **Block list** stored in the webhook backend's database. Schema: `number, reason, added_at, added_by, recording_url` (recording URL only present for AI-flagged auto-blocks, so a human can review).
- **Pre-call screening**: when an inbound call arrives, the webhook checks the block list *before* routing to Vapi. Saves AI cost and avoids engaging the agent with junk.
- **Block action**: **silent hangup**. Scammers don't learn the number is screened, so they don't iterate.
- **How numbers get added:**
  1. **Admin command** from the hotel phone ("Block this number" right after a bad call, or "Block 555-...")
  2. **Automatic flag** by the AI mid-call, when scam patterns are detected (cold sales pitch, robocall, inappropriate payment requests, etc.). The full call recording is saved alongside the block-list entry so a human can review and unblock if it was a false positive.
- **Rate limiting is NOT used.** Guests legitimately call back several times while building a reservation; auto-blocking them would be unacceptable.
- **Optional**: Twilio Lookup API spam-score check on first-time callers (small per-lookup fee), as an extra signal feeding into the auto-flag heuristic.

### Power and connectivity resilience

Two failure scales to plan for:

**Scale 1 — brief power blips and short outages (most common)**: handled by UPS coverage of every node in the SIP traffic path. With ~900VA UPS protection, the system stays fully operational through ~30–90 minute outages. See One-time hardware costs section for UPS placement.

**Scale 2 — extended outages (rare, every couple of years)**: 4-hour power outages exceed reasonable UPS runtime. For these, the system uses **automatic failover to a staff cell phone** via the existing dynamic call routing mechanism:

1. When the ATA SIP endpoint stops responding (Twilio's registration lapses or call routing fails), the webhook detects this either via Twilio API health checks (proactive) or via call-attempt failures (reactive).
2. The webhook automatically sets `call_routing.mode = "forward", destination = staff_cell_number`. This uses the existing routing state machine; no special-case logic needed beyond the trigger.
3. Twilio still has its number and is reachable — calls land at Twilio, which reads the routing state from the webhook and forwards to the staff cell.
4. Staff handle calls from their cells until the ATA comes back online (power returns, ATA re-registers).
5. Webhook detects the ATA is back, auto-reverts to `mode = ai_handle`.
6. Text the owner when failover triggers and again when it clears, so they know the state.

This design means a 4-hour power outage at the hotel is *not* 4 hours of dropped calls — it's 4 hours of staff-cell-handled calls, with automatic recovery when power returns.

**One key requirement**: the webhook backend itself must stay up during the hotel's outage. It does — the VPS is at DigitalOcean SFO3, completely independent of the hotel's local power and internet. The webhook keeps running, makes routing decisions, and Twilio keeps acting on them.

**What still doesn't work during a hotel outage**: outbound calls from the front-desk Panasonic (it has no power), and AI-handled calls (the ATA is down). Both come back automatically once power returns.

### Call data archival (vendor-independent local copy)

A scheduled job on the VPS pulls every Vapi call's full data into our own storage on a regular cadence (default: nightly). Purpose: vendor independence, long-term retention beyond Vapi's defaults, and full local control of data for credit card dispute resolution and prompt iteration.

**What gets archived per call:**
- Full metadata (timestamps, caller-ID, dialed number, duration, status, cost, etc.)
- Conversation transcript
- Vapi auto-generated call summary
- Full audio recording (MP3 or WAV)
- Tool-call log (which tools the AI invoked, parameters, results)
- Cross-references from our webhook DB: routing decisions, Cloudbeds reservation IDs created, junk-flag status, etc.

**Storage layout:**
- **Audio files** on filesystem, organized by date (`/var/recordings/YYYY/MM/DD/call_<id>.mp3`)
- **Database table** (in the webhook backend's PostgreSQL/SQLite) with all metadata, transcript, summary, tools, and Cloudbeds reservation linkage. Audio file path is stored as a column referencing the filesystem.

**Storage scale:** ~3 MB/call audio × 12 calls/day = ~1.1 GB/month, ~13 GB/year. Comfortably fits on the DigitalOcean Basic Droplet's 25 GB SSD for ~2 years. After that: add a DO Volume ($1/mo per 10 GB), or push older recordings to Backblaze B2 (~$0.005/GB/mo) and keep only recent ones local.

**Retention policy:**
- Default: 18 months (covers credit card chargeback dispute windows)
- AI-flagged junk-block calls: indefinite (human review may happen weeks/months later)
- Marked "keep forever": indefinite (manual override for unusual cases)
- Calls that captured credit card numbers (defensive — we won't be intentionally collecting card numbers verbally): shorter retention with secure deletion

**Critical join key for dispute resolution**: when the AI creates a Cloudbeds reservation, the resulting `cloudbeds_reservation_id` is stored on the call record. To respond to a dispute on reservation #12345: one query returns the call ID, audio file, transcript, timestamp, and caller — everything needed for the credit card agency.

**Critical for prompt iteration**: a small web UI on the backend lists recent calls with filters (date range, "calls that ended in transfer," "calls flagged with issues," etc.), and clicking expands to transcript + audio playback + tools-invoked log. This is the primary tool for finding and fixing prompt weaknesses based on real call behavior.

**PCI note**: avoid capturing credit card numbers by voice — collect via secure follow-up link (text/email) instead. If card numbers ever leak into a recording, that recording is now PCI-DSS-scoped data and the system inherits compliance obligations. Best avoided.

---

## Choices made

| Decision | Choice | Why |
|---|---|---|
| Voice platform | **Vapi** | Better tool-calling docs and webhook story than Retell. |
| Carrier | **Twilio (BYOT)** | User owns the numbers directly; portable if Vapi is ever replaced. |
| STT provider | **Deepgram Nova-3 monolingual** ($0.0048/min) | Telephony-tuned; addresses Hey Sadie's biggest weakness. Multilingual upgrade ($0.0058/min) deferred — only ~3% Spanish-accented English callers, no actual non-English calls expected. |
| LLM | **Claude Sonnet 4.6** as default ($3/MTok in, $15/MTok out via Anthropic API). Opus 4.7 ($5/$25) as fallback if guests find the default struggles on edge cases. | Sonnet 4.6 is the typical voice-agent choice — good latency, strong reasoning. Opus 4.7's recent price drop (from $15/$75 → $5/$25) makes it economically viable as a quality upgrade. |
| LLM billing | **Separate from Claude.ai Pro/Max subscription.** Anthropic API is billed independently at console.anthropic.com. Pro/Max does not include API credits. | Verified directly with Anthropic support docs. |
| TTS provider | **ElevenLabs** with voice chosen from Voice Library or generated via Voice Design. Voice character: warm, professional, caring, compassionate, informative. Slightly slower pace, mid pitch, high stability. | Largest voice selection; Voice Design avoids needing to clone any real person, which sidesteps consent and continuity issues. |
| Backend language | **Python + FastAPI** | Cleaner code for a Python newcomer; FastAPI's auto-generated JSON schemas make Vapi tool wiring less error-prone; more Cloudbeds examples online. |
| VPS | **DigitalOcean SFO3, $6/mo Basic Droplet** + $1.20 backups. Cloudflare Tunnel + local PC during dev. | West Coast region near AWS us-west-2 (where Vapi routes our traffic). DigitalOcean over Hetzner Hillsboro despite Hetzner's slightly lower latency, because Hetzner has documented account-suspension issues that would be unacceptable for a hotel's live phone webhook. |
| Phone strategy | Port hotel number to Twilio after testing; replace Hyak phone service with Twilio + ATA. Keep Hyak's fiber for internet only. | One number for everything. |
| ATA architecture | Register the ATA to a **TwiML SIP Domain**, not an Elastic SIP Trunk. | Trunks don't accept SIP REGISTER from ATAs; SIP Domains do. Slightly higher per-minute cost ($0.0085 vs $0.0034) is irrelevant at our volume. |
| ATA hardware | **Grandstream HT802** (ordered, ~$60–70 one-time). 2 FXS ports — first port to the Panasonic, second port reserved for future analog line. Cisco SPA112 noted as fallback if HT802 hits Twilio compatibility issues. | Twilio doesn't officially endorse the HT802 but it meets generic SIP requirements; widely used in the field. Some config trial-and-error expected. HT802 chosen over HT801 for the second FXS port (modest cost increase, future-proofing for an additional analog line). HT812 (PoE) was rejected — PoE convenience didn't justify the further cost increase given everything is in one server closet. |
| Transfer destination (production) | ATA SIP endpoint (no second phone number required) | One number for everything. |
| Transfer destination (test phase) | Staff cell phone | Avoids forwarding-loop with the live hotel number. |
| AI transparency | **Agent introduces itself as an AI** in greeting. Example: *"Hi, you've reached [Hotel Name] — I'm [Name], the inn's AI assistant."* | User's explicit ethical preference: callers should know they're talking to an AI to set correct expectations. Also legally cleaner under emerging AI-disclosure laws. |
| Caller authentication | **Caller-ID lookup against Cloudbeds (primary) + verbal verification of one detail (fallback)** for callers from unfamiliar numbers. **No voice biometrics.** Soft-signal: log voice characteristics per call for flagging mismatches, not for blocking. | Voice biometrics is overkill at our scale, has enrollment chicken-and-egg, and creates BIPA/CCPA compliance overhead. Verbal verification of one detail (date of arrival, spelled last name) is more reliable anyway. |
| Spelling clarification policy | Model asks for spelling **only when genuinely uncertain** about a word important to the reservation. Common confident names: don't ask. Always read back the final reservation for natural error-catching. | Hey Sadie's habit of asking for every spelling is a major friction point; fixing it is one of the rebuild's wins. |
| Block action | Silent hangup | Scammers don't iterate. |

## Resolved (moved out of open questions)

- **Hyak phone service**: month-to-month, no contract barrier to porting. Bundled with internet, not expensive on its own. Internet stays with Hyak after the port; phone portion gets cancelled when Twilio takes over.

## Integration with GX-26 (existing hotel automation system)

**Location**: `D:\2-Work\ComputerSoftwareDevelopment\Cloudbeds-GX26`

**What GX-26 already does** (B4J/Java application that runs on the same Windows machine, port 8080):

- **Cloudbeds API integration** — full wrapper (`CloudbedsAPI.bas`) with auth/request scaffolding we should lift from rather than re-implement
- **Door code management** (`DoorCodeMgr.bas`) — generation, assignment, distribution
- **Z-Wave network management** — controls the smart locks on the rooms
- **SMS to guests** — currently the system that sends arrival info, etc. (May migrate to our Twilio setup once it's live; deferred decision.)
- **Room assignment in Cloudbeds** — automatically assigns guests to rooms
- **Housekeeper management** — tracking, scheduling
- **Check-in readiness** (`CheckinReadiness.bas`) — knows when rooms are ready
- Lots of API result examples (`API-resultFrom-*.txt`) from Cloudbeds endpoints — useful as schema reference for our Python wrappers
- **Cloudbeds API documentation** files captured locally — `CloudbedsAPI-v1.3-documentation.odt`, `CloudbedsWebhookDocumentation.odt`

**Implications for Iris's backend**:

- The Iris webhook backend (Python/FastAPI on port 8000) and GX-26 (B4J on port 8080) coexist on the same machine without conflict.
- **Cloudbeds operations: Iris calls Cloudbeds directly** (decided 2026-05-03). Latency matters during a live phone conversation — Iris waiting for tool responses is the caller waiting in silence, so removing an intermediate hop (Iris → GX-26 → Cloudbeds → back) is worth the modest duplication. GX-26's `CloudbedsAPI.bas` is reference material for our Python implementation, not an intermediary.
- **GX-26-only operations** (door codes, Z-Wave, room assignment beyond what Cloudbeds tracks, housekeeper status): Iris calls GX-26 over a local HTTP API. These are GX-26's domain — duplicating them in Iris would be wasteful.
- **SMS pipeline**: GX-26 currently sends guest-facing SMS. Once our Twilio SMS is wired up, decide whether to consolidate (one of them takes over) or coexist (each handles its own messages). Open decision; not blocking initial Iris build.

**Reference materials in the GX-26 folder we can lean on**:

- `CloudbedsAPI.bas` — auth flow, request structure, error handling patterns
- `API-resultFrom-getReservation.txt` etc. — actual Cloudbeds response shapes (helpful for Pydantic models in `app/models/`)
- `CloudbedsAPI-v1.3-documentation.odt` — verify current API version and endpoints we plan to use
- `CloudbedsWebhookDocumentation.odt` — for the room-ready webhook integration plan

**Resolved** (the original scope items, now subsumed by the integration discussion above):
- Cloudbeds room-ready trigger via GX-26 ✓
- Reuse of Cloudbeds API auth scaffolding ✓

## Open questions

- **Specific ElevenLabs voice**: pick during build phase by listening to Voice Library samples and/or generating via Voice Design. Locked-in characteristics (warm, professional, caring, compassionate, slightly slower pace, mid pitch, high stability) are documented above.
- **Custom SIP header secret value**: will generate a random secret when configuring the ATA, store in webhook backend's environment variables.
- **System prompt content**: in progress; v1 draft at `D:\2-Work\ComputerSoftwareDevelopment\AI Reservation Agent\AI_Prompts\Lighthouse_AI_system_prompt-2026may02.txt`. Will iterate based on test calls.
- **Branded calling registration** (Hiya / First Orion, ~$5–15/mo): deferred until/unless guests start ignoring outbound calls as spam.
- **Vapi voice biometrics availability**: not currently planned to use, but worth checking when wiring up — could feed the soft-signal flag without external services.

## Implementation TODOs (deferred until webhook backend exists)

- **`send_door_code` tool**: when a caller is arriving after 8 PM, Iris offers to text them the room number and door code so they can self-check-in. Implementation needs:
  - A reliable way to get the door code for a given room (likely a custom field in Cloudbeds, or a separate keymaster system the hotel uses today). Verify how the door codes are managed.
  - Twilio SMS API call to send the message to the caller's number.
  - SMS template: room number + door code + brief directions.

- **Lowest-rate selection in `check_availability`**: when wiring up the Cloudbeds availability tool, ensure the response surfaces ALL applicable rates (standard / 5% direct-call / multi-night), so the agent can pick the lowest. Verify Cloudbeds API exposes the rate plans we need; some PMSes return only the default rate.

- **Same-day room + code handoff at end of call**: for same-day bookings where (a) payment is verified, (b) the room is already assigned, and (c) the door code is retrievable — Iris gives the caller their room number, brief directions, and door code right at the end of the call instead of the generic "you're all set" recap. Streamlines the check-in experience for direct callers, eliminates the need for them to stop at the front desk. Requires both the room-assignment and door-code routines (see above) to be working. Already added to Booking Flow Rules Stage 11 with a conditional gate.

- **Modify / cancel reservation tools** (with booking-source check): Iris can modify or cancel reservations only if they were booked DIRECTLY through the hotel (phone, AI agent, or hotel website). For OTA bookings (Booking.com, Expedia, etc.), she directs the caller to the original booking channel. Implementation needs:
  - `lookup_reservation(phone_or_lastname)` — returns reservation record including the `source` field (Cloudbeds API exposes this), the assigned room number, the current door code, and ALL phone numbers on the reservation (primary + any secondary contacts). The room/door/phones fields are critical for the lockout self-service flow (see TODO below).
  - `modify_reservation(reservation_id, changes)` — updates dates, room, notes, etc. Cloudbeds API supports this.
  - `cancel_reservation(reservation_id, reason)` — cancels per Cloudbeds policy.
  - `add_reservation_note(reservation_id, note)` — appends to special-requests / internal notes.
  - Verify which `source` values Cloudbeds returns for direct bookings so the prompt's branch logic matches reality (likely `phone`, `manual`, `direct`, `mybooking_engine`, `cloudbeds_website`, or similar).
  - Already added to [Handling Existing Booking Inquiries] section of the prompt as the canonical handling flow.

- **Lockout self-service with caller-ID + room-number authentication**: per the [Transfer Scope Rules] section "Lockout self-service" subsection, Iris can give a verified caller their room number and door code without transferring. Implementation needs:
  - `lookup_reservation` returning room + door code + all phones (see above).
  - **Two-factor verification logic** in the webhook backend: (a) caller's incoming phone matches a phone on the reservation, AND (b) caller verbally states the correct room number.
  - `send_door_code(reservation_id, phone_number)` — SMS the room number and door code to the caller. Existing TODO; this extends the use case.
  - **Audit log table**: every self-service door-code retrieval (success or fail) logs caller-ID, reservation ID (or null if no match), claimed room number, verification outcome, and timestamp. Useful if there's ever a security concern. Integrate with the broader `Call data archival` flow.

- **Florence emergency-services caller-ID list**: compile and maintain the dispatch phone numbers for Florence-area police, fire, and EMS so Iris can recognize them on caller-ID and transfer immediately (cold transfer, no briefing). Numbers to look up:
  - Florence Police Department dispatch
  - Western Lane Ambulance / Florence-area EMS
  - Siuslaw Valley Fire & Rescue dispatch
  - Lane County Sheriff (covers unincorporated areas around Florence)
  - Oregon State Police regional office
  - Source: each agency's published non-emergency line + any dispatch numbers shared with local businesses. Verify via the agencies directly.
  - Stored in the webhook backend as a config list; transfer tool checks caller-ID against it on inbound.

- **Full transcripts of human-handled calls** (extension of Full-call recording TODO): the Twilio recordings of human-only portions (transferred segments, forward-mode calls, direct staff calls) are audio-only by default. To get text transcripts of these for prompt iteration, run a batch transcription pass after the call ends:
  - Use Deepgram batch API or OpenAI Whisper (whichever is cheaper / better for our audio).
  - Trigger: when a call ends and a Twilio recording is available, send it for transcription.
  - Cost: Deepgram Nova-3 batch ~$0.0043/min (vs streaming $0.0048/min); Whisper $0.006/min. Both very cheap at our volume.
  - Store transcripts alongside the audio in the call archive.
  - Use case: review actual human handling of edge cases to inform Iris's prompt, especially for situations Iris currently transfers.

- **SMS-Eric fallback when both transfer destinations fail**: rare but worth handling. If both front desk and Eric's cell time out on a transfer attempt, Iris sends Eric an SMS alert (e.g., "Hotel call needs attention: [caller name + brief context]") via the Twilio SMS API. Caller hears something like "Both lines are unavailable right now — I've sent Eric a text alert. Could I take your number for a callback?"

- **`send_sms_to_eric(message)` tool** for routine operational notifications: this is the same SMS pipe as the transfer-fallback above, used for routine ops messages from the [Check-Out Requests] flow. Examples Iris will generate:
  - "Room 27 just checked out — Iris."
  - "Room 2 requested a noon checkout — Iris."
  - "Room 5 requested a 2 PM checkout — Iris."
  - "Room 5 wants to stay another day — Iris."
  - Eric uses these to know what to expect from housekeeping and which rooms have late checkouts. No reply needed; one-way notifications.

- **`mark_reservation_checked_out(reservation_id)` Cloudbeds API call**: the FUTURE workflow for phone check-out. Currently Iris notifies Eric by SMS and Eric updates Cloudbeds manually. Once the Cloudbeds API endpoint for "mark checked out" is wired up, Iris can update the reservation status directly. The SMS notification can either continue (informational for Eric) or be dropped (since the system status will reflect it). Decide at implementation time.

- **Weather lookup tool** (`get_weather(location)`): nice-to-have so Iris can answer "What's the weather like?" with current conditions. Today she defers to "check a weather app." Implementation: small wrapper around a free weather API (OpenWeatherMap, NWS, or similar) that returns current conditions and a brief forecast for Florence, OR. Default location is the hotel; could accept other locations if a guest is asking about somewhere they're driving from. Low priority — a deferred answer is acceptable for v1.

- **Knowledge Base content migration** — DONE for extraction. 160 Q&A entries migrated from Hey Sadie's KB to `AI_Prompts/knowledge_base.md` (~27KB / ~7K tokens). Remaining work:
  - Review each entry for accuracy and Iris's voice (some Hey Sadie answers reference Sadie's persona).
  - Update three entries that reference `(541) 256-2320` (Whistle texting number) once the new Twilio SMS system is set up.
  - Reconcile the 5%-vs-10% discount discrepancy (KB says 10% AARP/Senior on phone bookings; prompt's [Notes] says 5% direct-call discount).

- **Full-call recording at the Twilio/carrier level** (broader than AI-only recording): record EVERY call to the hotel — AI-handled, transferred, forward-mode, paused — capturing the complete conversation including post-transfer human portions. Existing Vapi recording covers only the AI portion of AI-handled calls; this expands the scope.
  - **Why**: the user wants real human-staff conversations as reference material for refining Iris's prompt. Also useful for dispute resolution, quality review, and training. Captures the full picture of every interaction with the hotel.
  - **Implementation**: enable call recording at the Twilio phone-number level (or via TwiML `Dial` with `record="record-from-answer-dual"`). Twilio mediates all call audio after porting, so recording continues seamlessly across transfers and forward-mode routing.
  - **Cost**: ~$0.0025/min recording + storage; trivial at our volume (~$1/month total).
  - **Storage**: integrate with the existing `Call data archival` flow (extend to also pull Twilio recordings, not just Vapi recordings, and merge them per call). Same retention defaults apply (18 months baseline).
  - **Privacy**: Oregon is a one-party consent state, so recording is legal. Iris's greeting mentions "this call may be recorded." For forward-mode and direct-staff calls (where Iris isn't on the call), consider a brief pre-bridge recording disclosure tone or a TwiML `<Say>` line, OR rely on staff to disclose verbally. Decide policy at build time.
  - **Useful side effect**: full recordings inform the prompt-iteration loop. Listen to how Eric handles edge cases the AI struggles with, and bake those patterns into the prompt.

- **Date variable injection in Vapi**: the prompt's [Reference Dates] section uses Vapi template variables (`{{current_datetime_long}}`, `{{current_day}}`, `{{tomorrow}}`, `{{day_plus_2}}` through `{{day_plus_6}}`, and `{{next_sunday}}` through `{{next_saturday}}`). These need to be set up in the Vapi assistant's `assistantOverrides.variableValues` (or equivalent) so they're populated dynamically per call. Likely options:
  - Vapi may support computed/dynamic date variables natively — check their docs at config time.
  - Otherwise the webhook backend computes these values when triggering an outbound call, or sets them via the assistant config API at startup with a fresh-each-day refresh (cron job).
  - Or the conversation-start webhook hook injects them per call.
  - All variables should be in the hotel's local timezone (US/Pacific). Format: `Friday, May 1` for the human-readable parts.

- **Revisit Conversational Excellence pace/energy section after a few weeks of live use**: the "Match the caller's pace and energy" subsection of [Conversational Excellence] in the prompt is somewhat aspirational — LLMs aren't reliably good at perceiving caller mood from voice transcripts. Listen to actual call recordings during the first few weeks of operation; if Iris is meaningfully adapting to caller energy, keep the section. If it's making no perceivable difference, trim it. If it's causing weird behavior (mismatched tone), revise.

- **`build_prompt.py` build/sync script**: a small utility that concatenates the prompt sections + `knowledge_base.md` into a single deployable prompt for Vapi. Should:
  - Read `AI_Prompts/Lighthouse_AI_system_prompt-2026may02.txt` and `AI_Prompts/knowledge_base.md`.
  - Combine them (KB content gets injected at the [Knowledge Base] section's location).
  - Write the assembled prompt to a deployable artifact (e.g., `AI_Prompts/_build/lighthouse_ai_full.txt`) or push directly to Vapi via API.
  - Optionally compute a content hash for cache-busting / change detection.
  - Anthropic prompt caching keeps the KB cheap on every call as long as the prompt is structured with stable content first (system instructions + KB) and dynamic content last (per-call variables like date and caller phone).

---

## Rollout plan

### Phase 1 — Build and test (no disruption to live service)

1. Sign up for Twilio (BYOT) and Vapi accounts (both free to start)
2. Provision a temporary Twilio number through Vapi for testing
3. Set up forwarding from the live hotel number (Hyak) to the test number — same as today's Hey Sadie forwarding
4. Build webhook backend with Cloudbeds tool integration. Start with two tools: `lookup_reservation_by_phone` and `create_reservation`
5. Write the system prompt; iterate via test calls
6. Test transfers to a staff cell phone
7. Test outbound calling
8. Test admin-by-phone using a SIP softphone (simulates the future ATA setup)
9. Test junk-blocking flow end-to-end (admin command + AI auto-flag)

### Phase 2 — Cutover (staggered: 866 toll-free first, then local number)

The hotel has two existing customer-facing numbers, both currently on Hyak:
- One **toll-free 866** number — ports first (lower-risk if anything goes wrong; statistically lower-stakes than the local-area regulars who use the local line)
- One **local** number — ports second, after the system has proven itself in production with real calls

Staff outbound caller-ID will be the local number for both AI outbound and front-desk outbound.

**Pre-port preparation (do once, applies to both ports):**

10. ~~Order~~ ATA (Grandstream HT802) — **ordered**. Configure with Twilio SIP credentials and the custom `X-Hotel-Auth` header on arrival.
11. Order and install the unmanaged switch + UPS in the server closet
12. Submit port requests for both numbers to Twilio. Toll-free porting is a RespOrg change (typically faster than local LRN porting, but lead time should be re-verified at submission). Local porting takes 7–15 days.

**Stagger step A — port the 866 toll-free number:**

13. Configure Hyak: forward the 866 number → the local number. Test by calling the 866 from a cell phone; should ring through to the current local-number destination (Hey Sadie).
14. On 866 cutover day: install ATA in front of the Panasonic, configure Twilio for the now-Twilio 866 number to route through the webhook. Verify by calling the 866 — should reach the AI agent.
15. During the cutover window, any calls that briefly hit Hyak for the 866 get forwarded to the local number → Hey Sadie. No call dropped.
16. After 866 cutover: 866 is on Twilio + AI; local is on Hyak + Hey Sadie. Run in parallel for a validation period (a few days to a few weeks). Iterate the prompt, listen to recordings, fix issues.
17. Clean up the Hyak forwarding rule for 866 (it's moot now since Hyak doesn't have the number, but tidy up).

**Stagger step B — port the local number:**

18. When confident the AI is performing well: configure Hyak: forward the local number → the now-Twilio 866 number.
19. Submit and execute the port for the local number.
20. During the local cutover window: stray calls briefly hitting Hyak for the local number get forwarded to the 866 (now on Twilio) → AI agent. Continuity preserved.
21. After local cutover: both numbers are on Twilio. Configure both numbers' Voice "A call comes in" webhook to the same backend `/incoming-call` endpoint. Both handled identically.
22. Update Vapi assistant transfer destination from staff cell to ATA SIP endpoint.

**Cleanup (after both ports complete):**

23. Cancel Hyak phone service (keep internet).
24. Tidy up any leftover Hyak forwarding rules.
25. Retire Hey Sadie.

**Forwarding capability confirmed**: Hyak supports forwarding to numbers on other carriers (the user already uses cell-phone forwarding). The strategy works without further dependencies.

---

## Cost estimate (unverified, per month)

**Assumptions**: 12 inbound calls/day × 3 min avg + ~5 AI outbound/day × 1.5 min = ~1,305 AI-handled min/month. 30 days/month. Sonnet 4.6 LLM. Inbound calls split roughly 50/50 between the toll-free 866 and the local number (rough estimate; actual split won't materially change totals). All outbound uses the local number for caller-ID. Front-desk outbound minutes via the ATA are excluded — they replace existing Hyak outbound and are roughly a wash.

| Line item | Calculation | Monthly |
|---|---|---|
| Twilio US local number | flat | $1.15 |
| Twilio US toll-free 866 number | flat | $2.15 |
| Twilio inbound — local (~540 min) | 540 × $0.0085 | $4.59 |
| Twilio inbound — toll-free (~540 min) | 540 × $0.022 | $11.88 |
| Twilio outbound — AI only, all from local CID | 225 × $0.014 | $3.15 |
| Twilio Lookup API (optional, ~10/day across both numbers) | 300 × $0.008 | $2.40 |
| Twilio Pay fees (~10 phone-captured payments/month) | 10 × $0.10 | $1.00 |
| Extra Vapi minutes during DTMF payment entry | ~10 min × $0.16 | $1.60 |
| Vapi platform fee | 1,305 × $0.05 | $65.25 |
| Deepgram Nova-3 monolingual STT | 1,305 × $0.0048 | $6.26 |
| Anthropic Claude Sonnet 4.6 | ~$0.024/call × 510 calls | $12.24 |
| ElevenLabs TTS (varies by plan/voice — see scenarios below) | | $25–115 |
| DigitalOcean Basic Droplet + backups | flat | $7.20 |
| Cloudbeds API | already paid | $0 |
| Cloudflare Tunnel (dev only) | free | $0 |

Toll-free pricing is materially higher than local: $2.15/mo vs $1.15/mo for the number, and $0.022/min vs $0.0085/min for inbound. Net effect of having the toll-free in addition to the local: about **+$10/month** vs a single-local-number scenario. Worth it because both numbers have been advertised for years and dropping either would lose calls.

### Total by configuration (with both numbers)

| Configuration | Total/mo |
|---|---|
| **Budget** (OpenAI TTS instead of ElevenLabs) | ~$140 |
| **Recommended** (ElevenLabs Turbo voice) | ~$220 |
| **Premium** (ElevenLabs Multilingual + Opus 4.7) | ~$290 |

The recommended config lands at roughly $220/mo with both numbers. The biggest swing factor is ElevenLabs voice tier; LLM choice (Sonnet 4.6 vs Opus 4.7) changes the total by only ~$8. Earlier $115–180/mo estimate (single-number, optimistic stack pricing) has been corrected.

### One-time hardware and setup costs

**Network layout (confirmed):**
- Server closet: GigaPoint, main GigaSpire BLAST, existing switch, Panasonic, (new) ATA
- Down the hall: one satellite GigaSpire BLAST (Wi-Fi coverage extension; was also feeding back to the server closet switch in the original wiring)

**Topology change adopted (Option B from design discussion):**

Insert a small unmanaged 5-port switch in the server closet, behind the main GigaSpire BLAST's LAN port 2. The cable that currently runs from main GigaSpire to the down-the-hall satellite gets reterminated at the new unmanaged switch instead. The new unmanaged switch then connects to both (a) the down-the-hall satellite (uplink path unchanged from satellite's perspective) and (b) the existing server-closet switch directly.

This routes the existing switch + ATA's traffic through the main GigaSpire directly, bypassing the satellite for SIP traffic. The satellite continues to provide Wi-Fi coverage as before, but its power state no longer affects the phone system. Eliminates the need for a second UPS at the satellite location.

**Verified compatible with Calix mesh** ([Calix mesh multi-hop documentation](https://www.calix.com/content/dam/calix/mycalix-misc/lib/prem/op/exos/spg/110394.htm)). Calix explicitly supports intermediate ethernet switches in the wired-backhaul path with these requirements, all of which our setup satisfies:
- Switch must not block IEEE 1905.1 protocol messages (MAC `01:80:c2:00:00:13`) — basic unmanaged switches forward all multicast by default ✓
- No VLAN segmentation issues — unmanaged switches aren't VLAN-aware ✓
- No ethernet loops — single uplink + single downlinks ✓
- Max 3 links in the path from RG to any satellite — we add 1 link (now 2 total) ✓
- Max 8 satellites per RG, max 3 in a chain — we have 2 satellites, both 1 hop from RG ✓

If a managed switch is ever substituted, IEEE 1905.1 forwarding and VLANs 501–548 (used for secondary SSIDs) must be allowed. Stick with basic unmanaged switches and this isn't a concern.

| Item | Where it goes | Cost |
|---|---|---|
| Grandstream HT802 ATA *(ordered)* | Server closet, plugged into existing switch (PoE port used as regular ethernet) and Panasonic via existing RJ-11 cable. Second FXS port unused, reserved for potential future analog line. | ~$60–70 |
| UPS, ~900VA (CyberPower CP900AVR or APC BR900MS) | Server closet — powers GigaPoint, main GigaSpire, unmanaged switch, existing switch, and ATA | ~$110–130 |
| Unmanaged 5-port gigabit switch (TP-Link TL-SG105 or Netgear GS305) | Server closet, between main GigaSpire LAN port 2 and the wall cable to the satellite | ~$15 |
| Short ethernet cables (2x) | Server closet, for the topology change | ~$10 |
| Twilio porting fee | — | ~$0–10 |
| Cloud account setup (Twilio, Vapi, Anthropic, DigitalOcean, etc.) | — | $0 |
| **Total one-time** | | **~$185–215** |

**Why this approach over alternatives**:

- *Two UPSes (one at server closet, one at satellite)* would cost ~$45 more and add a second battery-replacement location to remember. Rejected.
- *Asking Hyak for an upgraded main GigaSpire with more ports* — they were reluctant on the first install ask. The unmanaged-switch trick achieves the same outcome without depending on Hyak.

**What's still in the SIP path on UPS**: GigaPoint, main GigaSpire, unmanaged switch, existing switch, ATA. All in the server closet, all powered by the single UPS.

**What's no longer in the SIP path**: the satellite GigaSpire down the hall. Its Wi-Fi area goes dark during a power outage to that location, but phones keep working as long as the server closet has power.

### Offsets

- Hyak phone-service portion (cancelled at cutover): the user reports it's bundled and "not expensive," so a modest offset
- Hey Sadie subscription (replaced): unknown amount, but its replacement is a direct offset
- Front-desk outbound minutes through Twilio (~$11/mo at 25 min/day): replace what Hyak charges today, roughly a wash

### Sources for verified figures

- [Twilio voice pricing](https://www.twilio.com/voice/pricing/us)
- [Deepgram pricing](https://deepgram.com/pricing) — Nova-3 monolingual confirmed at $0.0048/min
- [Anthropic API pricing](https://platform.claude.com/docs/en/about-claude/pricing) — Sonnet 4.6 $3/$15 per MTok, Opus 4.7 $5/$25
- [Vapi pricing breakdown](https://pxlpeak.com/blog/ai-tools/vapi-pricing-breakdown) — $0.05/min platform fee
- ElevenLabs: estimated from credit-based tiers; will refine after measuring real character throughput from test calls.
