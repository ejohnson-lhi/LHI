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

    # --- Hotel identity (pinned to the top of every guest portal page) ---
    hotel_name: str = "Lighthouse Inn"
    # Single-line address: e.g. "155 Hwy 101, Florence, OR 97439". Empty -> not shown.
    hotel_address: str = ""
    # Display format guests see (e.g. "(541) 997-3221").
    hotel_phone_display: str = "(541) 997-3221"
    # E.164 form for tel: links (e.g. "+15419973221").
    hotel_phone_tel: str = "+15419973221"


settings = Settings()
