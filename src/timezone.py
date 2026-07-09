from datetime import datetime, timezone
from zoneinfo import ZoneInfo

RIYADH = ZoneInfo("Asia/Riyadh")


def to_riyadh(dt: datetime | None) -> datetime | None:
    """Convert a datetime to Asia/Riyadh.

    If ``dt`` is naive (no tzinfo) it is assumed to be UTC. Returns None
    when ``dt`` is None.
    """
    if dt is None:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(RIYADH)