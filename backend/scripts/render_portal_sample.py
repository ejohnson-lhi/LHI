"""Render static HTML snapshots of the Guest Portal in several states so we
can review the visual design without running the live backend.

Run from backend/: `.venv/Scripts/python.exe scripts/render_portal_sample.py`

Output: a handful of `portal_sample_*.html` files in backend/ — open them in
a browser. Stripe Elements may complain in the console (file:// URL, no live
Stripe.js init) but the visual layout renders normally.
"""
import asyncio
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# Set env BEFORE importing settings so the hotel header populates correctly.
os.environ.setdefault("HOTEL_NAME", "Florence Lighthouse Inn")
os.environ.setdefault("HOTEL_ADDRESS", "155 Hwy 101, Florence OR 97439")
os.environ.setdefault("HOTEL_PHONE_DISPLAY", "541-997-3221")
os.environ.setdefault("HOTEL_PHONE_TEL", "+15419973221")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("PORTAL_PUBLIC_BASE_URL", "https://iris.lighthouseinn-florence.com")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.db.database import Base  # noqa: E402

# Eagerly import every model whose table _render_portal_for_reservation
# touches (directly or via the helpers it calls). These imports register
# the tables on Base.metadata so create_all builds them. Without these,
# the lazy imports inside the helper functions fire too late and the
# tables don't exist when the query runs.
from app.models import portal_token  # noqa: F401, E402
from app.models import signature_agreement  # noqa: F401, E402
from app.models import pet_declaration  # noqa: F401, E402
from app.models import sms_consent  # noqa: F401, E402
from app.models import contact_acknowledgement  # noqa: F401, E402
from app.models import guest_preference  # noqa: F401, E402
from app.models import guest_qa  # noqa: F401, E402

from app.routes.portal import _render_portal_for_reservation  # noqa: E402


def _mock_reservation(
    *,
    phase: str,
    days_offset: int,
    nights: int,
    cards: list[dict] | None = None,
    is_direct_booking: bool = True,
    door_code: str = "",
    room_name: str = "Room 14 (Bay View)",
) -> dict:
    today = date.today()
    check_in = today + timedelta(days=days_offset)
    check_out = check_in + timedelta(days=nights)
    return {
        "reservation_id": "7952184200254",
        "guest_name": "Jane Mariner",
        "guest_first_name": "Jane",
        "guest_last_name": "Mariner",
        "check_in": check_in.isoformat(),
        "check_out": check_out.isoformat(),
        "start_iso": check_in.isoformat(),
        "end_iso": check_out.isoformat(),
        "stay_phase": phase,
        "status": "confirmed",
        "room_name": room_name,
        "door_code": door_code,
        "cards_on_file": cards or [],
        "is_direct_booking": is_direct_booking,
        "guest_address": "812 SW Coastal Way",
        "guest_address2": "",
        "guest_city": "Portland",
        "guest_state": "OR",
        "guest_zip": "97205",
        "guest_country": "US",
        "guest_phone": "5035551234",
        "guest_cell_phone": "5035551234",
        "guest_email": "jane.mariner@example.com",
    }


async def _render_to_file(
    res: dict,
    filename: str,
    *,
    seed_contact_ack: bool = False,
    seed_signature: bool = False,
    seed_pet: bool = False,
    seed_prefs: list[str] | None = None,
) -> None:
    """Render the portal page with optional pre-seeded DB rows so we can
    show how the layout reorders when sections are completed. Each
    `seed_*` flag inserts a corresponding row into the in-memory DB
    before render."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine) as db:
        if seed_contact_ack:
            from app.models.contact_acknowledgement import ContactAcknowledgement
            db.add(ContactAcknowledgement(
                reservation_id=res["reservation_id"],
                acknowledged_at=datetime.utcnow(),
            ))
        if seed_signature:
            from app.models.signature_agreement import SignatureAgreement
            db.add(SignatureAgreement(
                reservation_id=res["reservation_id"],
                guest_name=res.get("guest_name") or "",
                typed_name=res.get("guest_name") or "",
                agreement_text="(sample-rendered placeholder)",
                agreement_version="2026-05-21.v2",
                signature_png_base64="",
                cloudbeds_attached=True,
                signed_at=datetime.utcnow(),
            ))
        if seed_pet:
            from app.models.pet_declaration import PetDeclaration
            db.add(PetDeclaration(
                reservation_id=res["reservation_id"],
                dog_count=1,
                sold_product_id="SAMPLE-FEE-1",
            ))
        if seed_prefs is not None:
            import json as _json
            from app.models.guest_preference import GuestPreference
            db.add(GuestPreference(
                reservation_id=res["reservation_id"],
                prioritized_json=_json.dumps(seed_prefs),
                updated_at=datetime.utcnow(),
            ))
        if seed_contact_ack or seed_signature or seed_pet or seed_prefs is not None:
            await db.commit()

        response = await _render_portal_for_reservation(
            res,
            contact_action_url="/g/SAMPLETOKEN/contact",
            db=db,
            first_name_fallback="Jane",
        )
    html = response.body.decode("utf-8")
    out = Path(__file__).resolve().parent.parent / filename
    out.write_text(html, encoding="utf-8")
    print(f"Wrote {out}  ({len(html):,} bytes)")


async def main() -> None:
    # State 1: future stay, nothing yet completed -- the "first arrival" view.
    # Address auto-opens (not yet acknowledged), Sign + Card auto-open
    # (incomplete), all four sit at the top in Group A.
    await _render_to_file(
        _mock_reservation(phase="future", days_offset=3, nights=3),
        "portal_sample_future.html",
    )

    # State 2: arriving today, signature + card both still missing -- shows
    # the "please finish these to unlock door code" gating in the welcome box.
    await _render_to_file(
        _mock_reservation(phase="arriving_today", days_offset=0, nights=2),
        "portal_sample_arriving_today.html",
    )

    # State 3: in-house mid-stay, EVERYTHING done. Address acknowledged,
    # signature signed, card on file, pet declared, prefs saved. Watch
    # how the to-dos drop below FAQ into the "done" pile and Room
    # preferences shows the read-only "locked" view.
    await _render_to_file(
        _mock_reservation(
            phase="in_house",
            days_offset=-1,
            nights=3,
            cards=[{"cardType": "visa", "cardNumber": "4242"}],
            door_code="582914",
        ),
        "portal_sample_in_house.html",
        seed_contact_ack=True,
        seed_signature=True,
        seed_pet=True,
        seed_prefs=["top_floor", "back_side", "no_stairs"],
    )

    # State 4: OTA booking with a virtual card on file -- shows the
    # "incidentals card required" copy on the OTA-virtual card variant.
    await _render_to_file(
        _mock_reservation(
            phase="future",
            days_offset=10,
            nights=2,
            cards=[{"cardType": "mastercard", "cardNumber": "9521"}],
            is_direct_booking=False,
        ),
        "portal_sample_ota_virtual_card.html",
    )

    # State 5: departing today, all to-dos done -- Check out button
    # renders inline in the status section. Room prefs show read-only
    # (locked since the guest is checked in).
    await _render_to_file(
        _mock_reservation(
            phase="departing_today",
            days_offset=-2,
            nights=2,
            cards=[{"cardType": "visa", "cardNumber": "4242"}],
            door_code="582914",
        ),
        "portal_sample_departing_today.html",
        seed_contact_ack=True,
        seed_signature=True,
        seed_pet=True,
        seed_prefs=["bath_tub", "no_carpet"],
    )

    print("\nOpen any of the above in a browser to review the layout.")


if __name__ == "__main__":
    asyncio.run(main())
