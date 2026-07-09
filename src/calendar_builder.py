"""Build an iCalendar file from `FightEvent` objects.

This module creates a single `output/calendar.ics` file containing one
calendar event per fight card. Each calendar event's start time is the
`main_card` kickoff (or the earliest available time if main card is
missing). The description contains Main Event, venue and official URL.
All times are converted to Asia/Riyadh.  Events with no known time are
added as all-day events.  No synthetic times are ever invented.
"""
from __future__ import annotations

from typing import Iterable
from pathlib import Path
from datetime import datetime, date, time
import logging

from ics import Calendar, Event

from models import FightEvent
from timezone import RIYADH
import uuid

logger = logging.getLogger(__name__)


# Always write to <project_root>/output/calendar.ics regardless of cwd.
OUTPUT = Path(__file__).resolve().parent.parent / "output" / "calendar.ics"


def _stable_uid(ev: FightEvent) -> str:
	"""Generate a deterministic UID for an event.

	Prefer using the canonical source_url when available, falling back to
	organization + slug + event_name to keep UIDs stable across runs.
	"""
	seed = ev.source_url or f"{ev.organization}/{ev.slug or ev.event_name}"
	return str(uuid.uuid5(uuid.NAMESPACE_URL, seed)) + "@ufc-pfl-calendar"


def _choose_start(event: FightEvent):
	# Use the *earliest* known card time so the calendar entry begins when doors
	# open (early prelims > prelims > main card).  If no times are known, return
	# None -- the caller will create an all-day event.  Never synthesize times.
	for t in (event.early_prelims, event.prelims, event.main_card):
		if t is not None:
			return t
	return None


def build_calendar(events: Iterable[FightEvent]) -> Path:
	"""Create and write an ICS file containing all provided events.

	Returns the path to the written file.
	"""
	cal = Calendar()
	seen_uids: set = set()

	# Sort chronologically: timed events first, then all-day by date.
	def _sort_key(ev: FightEvent):
		start = _choose_start(ev)
		if start is not None:
			return start.astimezone(RIYADH)
		if ev.event_date is not None:
			return datetime.combine(ev.event_date, time(0, 0), tzinfo=RIYADH)
		return datetime.max.replace(tzinfo=RIYADH)

	sorted_events = sorted(events, key=_sort_key)

	for ev in sorted_events:
		uid = _stable_uid(ev)
		if uid in seen_uids:
			logger.debug("Skipping duplicate event UID: %s", uid)
			continue
		seen_uids.add(uid)

		start = _choose_start(ev)
		e = Event()
		e.name = f"{ev.organization}: {ev.event_name}"
		e.uid = uid
		e.description = _render_description(ev)

		if start is not None:
			e.begin = start.astimezone(RIYADH)
			e.duration = {"hours": 4}
		elif ev.event_date is not None:
			# All-day event -- no synthetic time.
			e.begin = ev.event_date.isoformat()
			e.make_all_day()
		else:
			logger.warning(
				"Event %s has no date or time; skipping", ev.event_name
			)
			continue

		cal.events.add(e)

	OUTPUT.parent.mkdir(parents=True, exist_ok=True)
	OUTPUT.write_text(cal.serialize(), encoding="utf-8")
	logger.info("Wrote calendar to %s", OUTPUT)
	return OUTPUT


def _render_description(ev: FightEvent) -> str:
	parts = ["━━━━━━━━━━━━━━", "", "🥊 Main Event", ""]
	parts.append(ev.main_event or "Not announced yet")

	if ev.co_main_event:
		parts.extend(["", "🥈 Co-Main Event", "", ev.co_main_event])

	if ev.main_event_division:
		parts.extend(["", "🥋 Division", "", ev.main_event_division])

	if ev.main_event_is_championship:
		parts.extend(["", "🏆 Championship Fight"])

	if ev.fight_list:
		parts.extend(["", "📋 Fight Card", "", ev.fight_list])

	if ev.location:
		parts.extend(["", "📍 Venue", "", ev.location])

	if ev.source_url:
		parts.extend(["", "🌐 Official", "", ev.source_url])

	parts.extend(["", "━━━━━━━━━━━━━━", ""])

	def fmt(dt) -> str:
		if dt is None:
			return "Not announced yet"
		riyadh = dt.astimezone(RIYADH)
		hour = riyadh.hour % 12 or 12
		ampm = "AM" if riyadh.hour < 12 else "PM"
		return f"{hour}:{riyadh.minute:02d} {ampm}"

	parts.extend(["🟢 Early Prelims", "", fmt(ev.early_prelims), ""])
	parts.extend(["🟡 Prelims", "", fmt(ev.prelims), ""])
	parts.extend(["🔴 Main Card", "", fmt(ev.main_card)])

	return "\n".join(parts)


