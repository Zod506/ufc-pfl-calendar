"""UFC event provider.

This module fetches upcoming UFC events from the official UFC website and
returns a list of `FightEvent` instances. It prefers structured JSON-LD
on event pages but falls back to basic HTML heuristics when required.
"""
from __future__ import annotations

from typing import List, Optional
from urllib.parse import urljoin
import logging
import re
from datetime import date

from dateutil import parser as date_parser

from models import FightEvent
from fetcher import fetch_html, extract_json_ld, extract_embedded_json
from timezone import to_riyadh
from ufc_parsers import parse_jsonld_schedule, parse_embedded_json_schedule, parse_html_schedule, is_generic_title

logger = logging.getLogger(__name__)


BASE = "https://www.ufc.com"


def _parse_jsonld_event(obj: dict) -> Optional[FightEvent]:
	"""Parse a JSON-LD object into a FightEvent when possible."""
	typ = obj.get("@type") or obj.get("type")
	if not typ:
		return None

	# Accept SportsEvent or Event
	if isinstance(typ, list):
		ok = any(t in ("SportsEvent", "Event") for t in typ)
	else:
		ok = typ in ("SportsEvent", "Event")

	if not ok:
		return None

	name = obj.get("name")
	url = obj.get("url")
	start = obj.get("startDate")

	# location
	location = None
	city = None
	country = None
	venue_name = None
	loc = obj.get("location") or {}
	if isinstance(loc, dict):
		venue_name = loc.get("name")
		addr = loc.get("address") or {}
		if isinstance(addr, dict):
			city = addr.get("addressLocality")
			country = addr.get("addressCountry")

	# subEvent may include prelims/early prelims
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

	# If top-level start exists and maincard missing, use it
	if maincard is None and start:
		try:
			maincard = date_parser.parse(start)
		except Exception:
			maincard = None

	# Use dedicated parser to extract only explicitly provided fields
	parsed = parse_jsonld_schedule(obj)
	# Avoid generic titles as main_event
	if parsed.get("main_event") and is_generic_title(parsed.get("main_event")):
		parsed["main_event"] = None

	parsed_date = None
	if start:
		try:
			dt = date_parser.parse(start)
			parsed_date = dt.date()
		except Exception:
			parsed_date = None

	fe = FightEvent(
		organization="UFC",
		event_name=name or "UFC Event",
		slug=(url or "").rstrip("/"),
		main_event=parsed.get("main_event"),
		location=venue_name,
		event_date=parsed_date,
		early_prelims=to_riyadh(parsed.get("early_prelims")) if parsed.get("early_prelims") else None,
		prelims=to_riyadh(parsed.get("prelims")) if parsed.get("prelims") else None,
		main_card=to_riyadh(parsed.get("main_card")) if parsed.get("main_card") else None,
		source_url=urljoin(BASE, url) if url else None,
	)

	return fe


def _parse_date_text(text: str) -> Optional[date]:
	if not text:
		return None
	text = re.sub(r"\s+", " ", text).strip()
	if not text:
		return None

	# Prefer explicit date formats with year and month names.
	patterns = [
		r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},\s*\d{4}\b",
		r"\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}\b",
		r"\b\d{4}-\d{2}-\d{2}\b",
	]
	for pattern in patterns:
		match = re.search(pattern, text, flags=re.IGNORECASE)
		if match:
			try:
				dt = date_parser.parse(match.group(0))
				return dt.date()
			except Exception:
				continue

	if not re.search(r"\b20\d{2}\b", text):
		return None

	try:
		dt = date_parser.parse(text, fuzzy=True)
		return dt.date()
	except Exception:
		return None


def _extract_event_date_from_source(soup, source_obj: Optional[dict] = None) -> Optional[date]:
	# Prefer explicit structured fields from the embedded JSON source.
	if isinstance(source_obj, dict):
		for key in ("eventDate", "startDate", "date", "event_date", "start_date"):
			value = source_obj.get(key)
			if isinstance(value, str):
				dt = _parse_date_text(value)
				if dt:
					return dt
			if isinstance(value, dict):
				for nested in ("startDate", "date", "eventDate"):
					nested_value = value.get(nested)
					if isinstance(nested_value, str):
						dt = _parse_date_text(nested_value)
						if dt:
							return dt

	# Look for explicit <time datetime="..."> tags in the page
	for time_tag in soup.find_all("time"):
		if time_tag.has_attr("datetime"):
			try:
				dt = date_parser.parse(time_tag["datetime"])
				return dt.date()
			except Exception:
				continue

	# Look for date text in meta description tags.
	meta_attrs = [
		{"property": "og:description"},
		{"property": "og:title"},
		{"name": "description"},
		{"name": "twitter:description"},
		{"name": "twitter:title"},
	]
	for attrs in meta_attrs:
		meta = soup.find("meta", attrs=attrs)
		if meta and meta.get("content"):
			dt = _parse_date_text(meta["content"])
			if dt:
				return dt

	# Fallback: search page text for a recognizable date string.
	text = soup.get_text(" ", strip=True)
	return _parse_date_text(text)


def get_ufc_events() -> List[FightEvent]:
	"""Return a list of upcoming UFC events discovered on the official site.

	The function attempts to discover event pages from the main events
	listing and parse each event's JSON-LD. Events without parseable
	schedule data are still returned, but timestamps may be None when not
	available.
	"""
	listing = fetch_html(f"{BASE}/events")
	if listing is None:
		logger.warning("Could not fetch UFC events listing")
		return []

	links = set()
	for a in listing.find_all("a", href=True):
		href = a["href"]
		if "/event/" in href and href.startswith("/"):
			links.add(urljoin(BASE, href))

	# Deduplicate by canonical event URL (strip fragments and query)
	from urllib.parse import urlsplit, urlunsplit

	def _canonical(u: str) -> str:
		sp = urlsplit(u)
		return urlunsplit((sp.scheme, sp.netloc, sp.path.rstrip('/'), '', ''))

	normalized_links = sorted({_canonical(u) for u in links})

	events: List[FightEvent] = []
	for url in normalized_links:
		soup = fetch_html(url)
		if soup is None:
			continue
		# Precompute a sensible page title for fallbacks
		title_tag = soup.find("h1")
		title = title_tag.get_text(strip=True) if title_tag else "UFC Event"

		jsonlds = extract_json_ld(soup)
		parsed = None
		for obj in jsonlds:
			ev = _parse_jsonld_event(obj)
			if ev:
				parsed = ev
				break

		# Fallback 2: embedded JSON (common on React sites)
		if parsed is None:
			embedded = extract_embedded_json(soup)
			for obj in embedded:
				try:
					mapping = parse_embedded_json_schedule(obj)
					if any(mapping.get(k) for k in ("main_card", "prelims", "early_prelims", "main_event")):
						# Build FightEvent from embedded mapping
						parsed = FightEvent(
							organization="UFC",
							event_name=title,
							slug=url.rstrip("/"),
							main_event=mapping.get("main_event"),
							location=None,
							event_date=_extract_event_date_from_source(soup, obj),
							early_prelims=to_riyadh(mapping.get("early_prelims")) if mapping.get("early_prelims") else None,
							prelims=to_riyadh(mapping.get("prelims")) if mapping.get("prelims") else None,
							main_card=to_riyadh(mapping.get("main_card")) if mapping.get("main_card") else None,
							source_url=url,
						)
						break
				except Exception:
					continue

		# Fallback 3: official HTML elements (only accept explicit <time> or labeled text)
		if parsed is None:
			html_map = parse_html_schedule(soup)
			if any(html_map.get(k) for k in ("main_card", "prelims", "early_prelims", "main_event")):
				parsed = FightEvent(
					organization="UFC",
					event_name=title,
					slug=url.rstrip("/"),
					main_event=html_map.get("main_event"),
					location=None,
					event_date=_extract_event_date_from_source(soup),
					early_prelims=to_riyadh(html_map.get("early_prelims")) if html_map.get("early_prelims") else None,
					prelims=to_riyadh(html_map.get("prelims")) if html_map.get("prelims") else None,
					main_card=to_riyadh(html_map.get("main_card")) if html_map.get("main_card") else None,
					source_url=url,
				)

		# Do NOT invent schedule information. We intentionally avoid
		# falling back to third-party sources to populate missing times.
		# If you want secondary sources, enable them explicitly.

		if parsed is None:
			# Last-resort: attempt to find a sensible title and top-level date
			title_tag = soup.find("h1")
			title = title_tag.get_text(strip=True) if title_tag else "UFC Event"
			parsed = FightEvent(
				organization="UFC",
				event_name=title,
				slug=url.rstrip("/"),
				main_event=None,
				location=None,
				early_prelims=None,
				prelims=None,
				main_card=None,
				source_url=url,
			)

		# Skip generic schedule pages that don't contain explicit schedule or headliner
		# to avoid picking up index pages like "MMA Schedule - 2026".
		if parsed:
			title_is_generic = is_generic_title(parsed.event_name or "")
			if title_is_generic and not (parsed.main_card or parsed.prelims or parsed.early_prelims or parsed.main_event):
				logger.debug("Skipping generic schedule page: %s", url)
				continue

			# Deduplicate by source_url
			if parsed.source_url:
				parsed.source_url = _canonical(parsed.source_url)

			events.append(parsed)

	return events


def _fetch_from_espn(event_name: str) -> Optional[dict]:
	"""Best-effort fallback: try to find event schedule on ESPN schedule page.

	This is a documented secondary public source used only when the
	official UFC event page lacks explicit schedule information.
	Returns a dict with possible keys: `main_card`, `prelims`,
	`early_prelims`, `main_event` where values are datetimes.
	"""
	try:
		url = "https://www.espn.com/mma/schedule"
		soup = fetch_html(url)
		if not soup:
			return None

		text_norm = event_name.lower()
		# Search for a block that contains the event name
		for block in soup.find_all(lambda tag: tag.name in ("section", "article", "div") and tag.get_text(strip=True)):
			blk_text = block.get_text(" ", strip=True).lower()
			if text_norm in blk_text or text_norm.split(" ")[0] in blk_text:
				# Look for <time> tags inside block
				times = []
				for t in block.find_all("time"):
					if t.has_attr("datetime"):
						try:
							times.append(date_parser.parse(t["datetime"]))
						except Exception:
							continue
				out = {}
				if times:
					# assign first time as main_card as a best-effort
					out["main_card"] = times[0]
				# attempt to extract a main event string
				he = block.find(lambda tag: tag.name in ("h1", "h2", "h3", "h4") and tag.get_text(strip=True))
				if he:
					out["main_event"] = he.get_text(strip=True)
				return out
	except Exception:
		return None

	return None

