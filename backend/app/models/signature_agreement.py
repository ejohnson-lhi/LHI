"""Guest signature-agreement audit log.

One row per signed agreement. Per policy, a guest signs ONCE per
reservation -- the next view of the section shows a read-only summary
("Signed by X on Y") instead of the form. To re-enable signing, delete
the row (or implement a staff override).

We store the raw signature PNG (base64) alongside the frozen agreement
text + version so the record is self-contained even if the PDF
attachment in Cloudbeds is lost or revoked. The PDF is the canonical
deliverable; this row is the local audit trail.
"""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text

from app.db.database import Base


class SignatureAgreement(Base):
    __tablename__ = "signature_agreement"

    id = Column(Integer, primary_key=True, autoincrement=True)
    reservation_id = Column(String, nullable=False, index=True, unique=True)
    guest_name = Column(String, nullable=True)
    typed_name = Column(String, nullable=True)  # the typed-name field next to the canvas

    # Frozen copy of what they agreed to. If we change the text later,
    # bump agreement_version; old rows still prove what was actually shown.
    agreement_text = Column(Text, nullable=False)
    agreement_version = Column(String, nullable=False)

    # Base64-encoded PNG of the canvas drawing. Useful for re-rendering or
    # forensic comparison. ~5-20 KB typical.
    signature_png_base64 = Column(Text, nullable=False)

    # Cloudbeds postReservationDocument outcome. attached=True means the
    # PDF made it onto the reservation folio. cloudbeds_doc_id is the
    # returned identifier when available.
    cloudbeds_attached = Column(Boolean, nullable=False, default=False)
    cloudbeds_doc_id = Column(String, nullable=True)
    cloudbeds_attached_at = Column(DateTime, nullable=True)

    signed_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    client_ip = Column(String, nullable=True)
    user_agent = Column(String, nullable=True)
