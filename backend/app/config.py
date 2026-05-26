"""Application settings loaded from environment variables."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- App ---
    app_env: str = "development"
    log_level: str = "INFO"

    # --- Twilio ---
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_hotel_number: str = ""
    twilio_tollfree_number: str = ""
    # Optional Messaging Service SID (starts with "MG..."). If set, send_sms
    # uses MessagingServiceSid instead of From — required pattern for A2P
    # 10DLC-registered traffic. Leave blank to fall back to From=number.
    twilio_messaging_service_sid: str = ""

    # --- Cloudbeds ---
    cloudbeds_api_key: str = ""
    cloudbeds_property_id: str = ""
    # User ID of the "Iris Agent" Cloudbeds user — used as note attribution
    # so audit trails show notes coming from Iris instead of the API key
    # owner. Look up via GET /admin/cloudbeds_users.
    cloudbeds_iris_user_id: str = ""
    # Reservation sourceID for tagging bookings created by Iris (e.g.,
    # "s-1183685" for the "Voice AI" source). Look up via GET /admin/cloudbeds_sources.
    cloudbeds_reservation_source_id: str = ""
    # Cloudbeds itemID for the dog/pet fee (configured per property). Default
    # matches the Lighthouse Inn's existing item used by the GX-26 wrapper
    # since 2024 -- $20/stay per dog, multiplied by itemQuantity.
    cloudbeds_dog_fee_item_id: str = "401630"

    # --- Cloudbeds dashboard automation (Playwright) ---
    # Used to generate Pay-by-Link URLs for guests by driving the Cloudbeds
    # admin UI (the API path is gated behind Marketplace App approval that we
    # don't have). All sensitive; never commit to source. Set in .env only.
    cloudbeds_login_url: str = "https://signin.cloudbeds.com"
    cloudbeds_admin_email: str = ""
    cloudbeds_admin_password: str = ""
    # Base32-encoded TOTP secret for the bot's 2FA. Get this by adding
    # "Google Authenticator" as a factor in your Okta account: when the QR
    # code shows, click "Can't scan?" / "Manual entry" to reveal the
    # text-form secret. Stash it here. NEVER commit. Treat like a password.
    cloudbeds_totp_secret: str = ""
    # Anti-bot pacing. Real humans don't fill 6-digit codes in 0ms. Okta
    # (and Cloudbeds' Stripe layer below) will quietly reject too-fast
    # input as scripted. Defaults match an average human's keystroke
    # cadence; bump higher if you still see rejections.
    cloudbeds_typing_delay_ms: int = 110   # delay between each typed char
    cloudbeds_action_pause_ms: int = 800   # pause between major actions
    cloudbeds_browser_slow_mo_ms: int = 60  # Playwright per-action slowdown
    # Set False during selector-discovery / debugging so we can SEE the browser.
    # Should be True in production.
    cloudbeds_browser_headless: bool = True
    # Amount (USD cents) to charge on the Pay-by-Link. v1 default: $1 auth-only
    # (the smallest "feels real" hold). Cloudbeds may force a specific minimum.
    cloudbeds_paylink_amount_cents: int = 100
    cloudbeds_paylink_description: str = "Card on file for incidentals (Lighthouse Inn)"
    # Where to SMS-alert when the automation fails (Eric by default).
    cloudbeds_automation_alert_phone: str = ""  # falls back to eric_cell_number

    # --- Anthropic ---
    anthropic_api_key: str = ""

    # --- Stripe ---
    # Secret key (sk_test_... or sk_live_...). Never exposed to clients.
    stripe_api_key: str = ""
    # Publishable key (pk_test_... or pk_live_...). Safe to embed in HTML for
    # Stripe.js / Stripe Elements. Required for the guest-portal add-card flow.
    stripe_publishable_key: str = ""

    # --- Vapi ---
    vapi_api_key: str = ""
    vapi_assistant_id: str = ""

    # --- Database ---
    database_url: str = "sqlite+aiosqlite:///./data/lighthouse.db"

    # --- Admin auth ---
    admin_sip_header_secret: str = ""

    # --- Eric's contact ---
    eric_cell_number: str = ""

    # --- Guest portal ---
    # Shared secret DCS uses to call portal admin endpoints (X-Portal-Auth header).
    # Generate with: python -c "import secrets; print(secrets.token_urlsafe(32))"
    portal_shared_secret: str = ""
    # Public base URL the SMS links point at. e.g. "https://hotel.example.com".
    # Tokens get appended as /c/{token}.
    portal_public_base_url: str = "http://localhost:8000"
    # When true, every checkout SMS is redirected to ERIC_CELL_NUMBER regardless
    # of which guest the reservation is for. The original phone is logged but
    # never texted. Belt-and-suspenders test safety: even if DCS sends real
    # reservations, no guest is bothered. Set PORTAL_TEST_MODE=true in .env.
    portal_test_mode: bool = False

    # --- DCS relay (hotel-side admin UI reachable through this droplet) ---
    # When set, /dcs/{path} HTTP-proxies requests to this URL over the
    # WireGuard tunnel — preferred mode going forward. When BLANK, the
    # relay falls back to 302-redirecting users to the ngrok URL most
    # recently published via /portal/dcs-tunnel (legacy mode, kept as a
    # months-long rollback path while we shake out WireGuard).
    # Typical value once WG is up: "http://10.42.0.2:8090"
    dcs_wg_target_url: str = ""

    # --- SMS signup webhook ---
    # Shared secret the WordPress /sms-signup/ Fluent Forms webhook sends in
    # the X-Signup-Secret header. Generate with:
    #   python -c "import secrets; print(secrets.token_urlsafe(32))"
    # Leave blank to disable the endpoint (returns 503 for all requests --
    # useful while developing or if the WP form is taken down).
    sms_signup_shared_secret: str = ""
    # Hostname the WP signup form lives on (informational; logged at startup
    # and exposed via /sms-signup/health for sanity-checking deploys).
    sms_signup_origin_host: str = "lighthouseinn-florence.com"
    # Public base URL Twilio POSTs to for inbound SMS (STOP/HELP/START
    # keywords). Used to reconstruct the canonical URL for Twilio's
    # signature verification (HMAC-SHA1 of URL + sorted form params).
    # Must match what's configured in the Twilio Messaging Service's
    # "Incoming Messages" webhook URL (scheme + host only -- the path is
    # appended by the route handler).
    twilio_inbound_base_url: str = "https://iris.lighthouseinn-florence.com"

    # --- Hotel identity (pinned to the top of every guest portal page) ---
    hotel_name: str = "Lighthouse Inn"
    # Single-line address: e.g. "155 Hwy 101, Florence, OR 97439". Empty -> not shown.
    hotel_address: str = ""
    # Display format guests see (e.g. "(541) 997-3221").
    hotel_phone_display: str = "(541) 997-3221"
    # E.164 form for tel: links (e.g. "+15419973221").
    hotel_phone_tel: str = "+15419973221"


settings = Settings()
