"""PFL event provider.

Fetches upcoming events from the official PFL website and converts them
into `FightEvent` instances. Uses JSON-LD when present and falls back to
simple HTML heuristics.
"""
from __future__ import annotations

from typing import List, Optional
from urllib.parse import urljoin
import logging

from dateutil import parser as date_parser

from models import FightEvent
from fetcher import fetch_html, extract_json_ld
from timezone import to_riyadh

logger = logging.getLogger(__name__)


BASE = "https://www.pflmma.com"


def _parse_jsonld_event(obj: dict) -> Optional[FightEvent]:
	typ = obj.get("@type") or obj.get("type")
	if not typ:
		return None

	if isinstance(typ, list):
		ok = any(t in ("SportsEvent", "Event") for t in typ)
	else:
		ok = typ in ("SportsEvent", "Event")

	if not ok:
		return None

	name = obj.get("name")
	url = obj.get("url")
	start = obj.get("startDate")

	venue_name = None
	loc = obj.get("location") or {}
	if isinstance(loc, dict):
		venue_name = loc.get("name")

	early = None
	prelims = None
	maincard = None

	for sub in obj.get("subEvent") or []:
		try:
			sname = sub.get("name", "").lower()
			sstart = sub.get("startDate")
			if sstart:
				dt = date_parser.parse(sstart)
			else:
				dt = None

			if "early" in sname or "early prelim" in sname:
				early = dt
			elif "prelim" in sname and "main" not in sname:
				prelims = dt
			elif "main" in sname or "main card" in sname:
				maincard = dt
		except Exception:
			continue

	if maincard is None and start:
		try:
			maincard = date_parser.parse(start)
		except Exception:
			maincard = None

	fe = FightEvent(
		organization="PFL",
		event_name=name or "PFL Event",
		slug=(url or "").rstrip("/"),
		main_event=None,
		location=venue_name,
		early_prelims=to_riyadh(early) if early else None,
		prelims=to_riyadh(prelims) if prelims else None,
		main_card=to_riyadh(maincard) if maincard else None,
		source_url=urljoin(BASE, url) if url else None,
	)

	return fe


def get_pfl_events() -> List[FightEvent]:
	listing = fetch_html(f"{BASE}/events")
	if listing is None:
		logger.warning("Could not fetch PFL events listing")
		return []

	links = set()
	for a in listing.find_all("a", href=True):
		href = a["href"]
		if "/events/" in href or href.startswith("/events/"):
			links.add(urljoin(BASE, href))

	events: List[FightEvent] = []
	for url in sorted(links):
		soup = fetch_html(url)
		if soup is None:
			continue

		jsonlds = extract_json_ld(soup)
		parsed = None
		for obj in jsonlds:
			ev = _parse_jsonld_event(obj)
			if ev:
				parsed = ev
				break

		if parsed is None:
			title_tag = soup.find("h1")
			title = title_tag.get_text(strip=True) if title_tag else "PFL Event"
			parsed = FightEvent(
				organization="PFL",
				event_name=title,
				slug=url.rstrip("/"),
				main_event=None,
				location=None,
				early_prelims=None,
				prelims=None,
				main_card=None,
				source_url=url,
			)

		events.append(parsed)

	return events

