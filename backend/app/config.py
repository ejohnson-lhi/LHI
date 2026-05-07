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

    # --- Anthropic ---
    anthropic_api_key: str = ""

    # --- Stripe ---
    stripe_api_key: str = ""

    # --- Vapi ---
    vapi_api_key: str = ""
    vapi_assistant_id: str = ""

    # --- Database ---
    database_url: str = "sqlite+aiosqlite:///./data/lighthouse.db"

    # --- Admin auth ---
    admin_sip_header_secret: str = ""

    # --- Eric's contact ---
    eric_cell_number: str = ""


settings = Settings()
