"""Canonical list of room preferences shown in the guest portal.

To change the list, edit AVAILABLE_PREFS below. The `key` field is the
stable identifier we save in the DB; renaming a `label` is safe (saved
data still resolves), but renaming a `key` orphans existing saved data
under that key. DELETING a key is also safe -- existing rows that
reference it get silently filtered out at render time.

Order in this list = the default order shown to a guest who hasn't
saved any preferences yet (all items start in the "no preference"
zone). The guest reorders them by dragging into the "matters to me"
zone in their personal priority order.

Some pairs are conceptually mutually exclusive (carpeted vs no_carpet,
front_side vs back_side). The portal lets the guest put both in
"matters" anyway; front desk reads the higher-priority one as the
stronger preference. No need to enforce exclusivity in the UI.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class RoomPreference:
    key: str           # stable identifier (lowercase snake_case)
    label: str         # display text shown to the guest
    tip: str = ""      # optional short tooltip / hint (under the label)


AVAILABLE_PREFS: list[RoomPreference] = [
    RoomPreference("outside_door", "Outside door"),
    RoomPreference("carpeted",     "Carpeted"),
    RoomPreference("no_carpet",    "No carpet",   "Hard floors — for allergies"),
    RoomPreference("bath_tub",     "Bath tub"),
    RoomPreference("no_stairs",    "No stairs",   "Step-free access"),
    RoomPreference("front_side",   "Front side"),
    RoomPreference("back_side",    "Back side"),
    RoomPreference("top_floor",    "Top floor"),
]


_BY_KEY: dict[str, RoomPreference] = {p.key: p for p in AVAILABLE_PREFS}


def get_by_key(key: str) -> RoomPreference | None:
    """Look up a preference by its stable key. Returns None if the key
    no longer exists in the canonical list (e.g. saved data references
    a preference we've since removed)."""
    return _BY_KEY.get(key)


def valid_keys() -> set[str]:
    """Set of currently-valid preference keys. Used to filter saved data
    against the canonical list when rendering or returning to staff."""
    return set(_BY_KEY.keys())
