"""Build an iCalendar file from `FightEvent` objects.

This module creates a single `output/calendar.ics` file containing one
calendar event per fight card. Each calendar event's start time is the
`main_card` kickoff (or the earliest available time if main card is
missing). The description contains Main Event, venue and official URL.
All times are converted to Asia/Riyadh.
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


OUTPUT = Path("output") / "calendar.ics"


def _stable_uid(ev: FightEvent) -> str:
	"""Generate a deterministic UID for an event.

	Prefer using the canonical source_url when available, falling back to
	organization + slug + event_name to keep UIDs stable across runs.
	"""
	seed = ev.source_url or f"{ev.organization}/{ev.slug or ev.event_name}"
	return str(uuid.uuid5(uuid.NAMESPACE_URL, seed)) + "@ufc-pfl-calendar"


def _choose_start(event: FightEvent):
	# Use main_card when available. If the event has no confirmed main_card
	# but the date is known, use noon Asia/Riyadh. Otherwise skip the event.
	if event.main_card is not None:
		return event.main_card
	if event.event_date is not None:
		return datetime.combine(event.event_date, time(12, 0), tzinfo=RIYADH)
	return None


def build_calendar(events: Iterable[FightEvent]) -> Path:
	"""Create and write an ICS file containing all provided events.

	Returns the path to the written file.
	"""
	cal = Calendar()
	for ev in events:
		start = _choose_start(ev)
		if start is None:
			logger.warning("Event %s has no start times; skipping event", ev.event_name)
			continue

		e = Event()
		e.name = f"{ev.organization}: {ev.event_name}"
		# Ensure datetime uses Riyadh tzinfo
		e.begin = start.astimezone(RIYADH)
		e.duration = {"hours": 4}
		e.description = _render_description(ev)
		# stable UID so updates replace existing entries instead of duplicating
		e.uid = _stable_uid(ev)
		cal.events.add(e)

	OUTPUT.parent.mkdir(parents=True, exist_ok=True)
	OUTPUT.write_text(cal.serialize(), encoding="utf-8")
	logger.info("Wrote calendar to %s", OUTPUT)
	return OUTPUT


def _render_description(ev: FightEvent) -> str:
	parts = []
	# Main event text
	if ev.main_event:
		parts.append(f"Main Event: {ev.main_event}")
	else:
		parts.append("Main Event: Not announced yet")
	if ev.location:
		parts.append(f"Venue: {ev.location}")
	if ev.source_url:
		parts.append(f"Official URL: {ev.source_url}")

	# Add timing lines
	def fmt(label, dt):
		return f"{label}: {dt.astimezone(RIYADH).isoformat()}" if dt else f"{label}: Not announced yet"

	parts.append(fmt("🟢 Early Prelims", ev.early_prelims))
	parts.append(fmt("🟡 Prelims", ev.prelims))
	parts.append(fmt("🔴 Main Card", ev.main_card))

	return "\n".join(parts)


