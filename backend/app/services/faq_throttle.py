"""LLM-call rate limiting for the portal FAQ / Ask Iris feature.

Policy (per reservation per UTC day):
  - 0-10 calls   -> Ask Iris button appears immediately
  - 11-15 calls  -> Button hidden for 5 seconds after a no-match query
  - 16-20 calls  -> Button hidden for 15 seconds
  - 21+ calls    -> Button never shown; "you've used today's allowance"
                    message instead.

The delay is a soft brake that gently steers the guest toward the FAQ.
The hard cap is the cost ceiling.

Counts are computed on the fly from the guest_qa table (no separate
counter table). For 150-entry KBs and short stays this is plenty fast;
if it ever becomes hot, add a per-reservation daily counter table.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ThrottleState:
    """Snapshot the frontend renders against."""
    used_today: int            # LLM calls already used (UTC day)
    daily_limit: int            # Hard cap before the button vanishes
    blocked: bool               # True iff the next call would exceed limit
    delay_seconds: int          # Frontend defers showing the button by this
    blocked_message: str        # Empty unless blocked


DAILY_LIMIT = 20
_DELAY_BANDS = (
    # (used_threshold_inclusive, delay_seconds_when_above)
    (10, 0),    # 0-10 used: no delay
    (15, 5),    # 11-15 used: 5s delay
    (20, 15),   # 16-20 used: 15s delay
)


def _delay_for(used: int) -> int:
    """Return the delay (seconds) to apply after `used` LLM calls today."""
    for threshold, delay in _DELAY_BANDS:
        if used <= threshold:
            return delay
    return 0  # past the last band the caller sees `blocked=True` anyway


def _utc_day_bounds(now: datetime) -> tuple[datetime, datetime]:
    """[start, end) of the UTC day containing `now`. UTC because the
    droplet runs in UTC and the daily reset semantics shouldn't drift
    with the hotel's wall-clock timezone."""
    start = datetime.combine(now.date(), time.min)
    return start, start + timedelta(days=1)


async def get_throttle_state(
    db: AsyncSession, reservation_id: str
) -> ThrottleState:
    """Look up how many LLM calls this reservation has used today and
    return a ThrottleState the UI can render against."""
    from app.models.guest_qa import GuestQa  # noqa: PLC0415
    now = datetime.utcnow()
    start, end = _utc_day_bounds(now)
    stmt = (
        select(func.count())
        .select_from(GuestQa)
        .where(and_(
            GuestQa.reservation_id == reservation_id,
            GuestQa.llm_used.is_(True),
            GuestQa.asked_at >= start,
            GuestQa.asked_at < end,
        ))
    )
    used = (await db.execute(stmt)).scalar_one() or 0
    blocked = used >= DAILY_LIMIT
    if blocked:
        msg = (
            "You've reached today's question allowance "
            f"({DAILY_LIMIT} questions). You can still use the FAQ list above, "
            "or call the front desk for anything more involved. Resets at UTC midnight."
        )
    else:
        msg = ""
    return ThrottleState(
        used_today=used,
        daily_limit=DAILY_LIMIT,
        blocked=blocked,
        delay_seconds=_delay_for(used),
        blocked_message=msg,
    )
