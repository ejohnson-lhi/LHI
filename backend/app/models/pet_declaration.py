"""Guest pet declaration log.

One row per save from the portal's "Pets" section. The latest row by id
is the current state for the reservation:
  - dog_count: 0, 1, or 2 (cats are not allowed -- no field for them)
  - sold_product_id: the Cloudbeds soldProductID we're currently holding
    for this reservation's pet fee (null when dog_count == 0)

We keep the full history rather than UPDATE in place because a guest
adding then removing then re-adding a dog is exactly the kind of
trail-of-changes the front desk wants to see when a card is later
disputed. Cloudbeds is the source of truth for the actual charge --
this table is local audit + reconciliation state.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String

from app.db.database import Base


class PetDeclaration(Base):
    __tablename__ = "pet_declaration"

    id = Column(Integer, primary_key=True, autoincrement=True)
    reservation_id = Column(String, nullable=False, index=True)
    dog_count = Column(Integer, nullable=False)  # 0 | 1 | 2

    # The Cloudbeds soldProductID for the currently-active fee, if any.
    # Null when dog_count == 0 (no fee on the folio).
    sold_product_id = Column(String, nullable=True)

    declared_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    client_ip = Column(String, nullable=True)
    user_agent = Column(String, nullable=True)
