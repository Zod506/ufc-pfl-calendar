"""PFL event provider.

Fetches upcoming events from the official PFL website and converts them
into `FightEvent` instances. Uses JSON-LD when present and falls back to
simple HTML heuristics.
"""
from __future__ import annotations

from typing import List, Optional
from urllib.parse import urljoin
import logging
import re
from datetime import date

from dateutil import parser as date_parser

from models import FightEvent
from fetcher import fetch_html, extract_json_ld
from timezone import to_riyadh

logger = logging.getLogger(__name__)


BASE = "https://www.pflmma.com"


def _canonical_url(url: str) -> str:
	from urllib.parse import urlsplit, urlunsplit
	sp = urlsplit(url)
	return urlunsplit((sp.scheme, sp.netloc, sp.path.rstrip('/'), '', ''))


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

	parsed_date = None
	if start:
		try:
			dt = date_parser.parse(start)
			parsed_date = dt.date()
		except Exception:
			parsed_date = None

	fe = FightEvent(
		organization="PFL",
		event_name=name or "PFL Event",
		slug=(url or "").rstrip("/"),
		main_event=None,
		location=venue_name,
		event_date=parsed_date,
		early_prelims=to_riyadh(early) if early else None,
		prelims=to_riyadh(prelims) if prelims else None,
		main_card=to_riyadh(maincard) if maincard else None,
		source_url=urljoin(BASE, url) if url else None,
	)

	return fe


def _parse_date_text(text: str) -> Optional[date]:
	if not text:
		return None
	text = re.sub(r"\s+", " ", text).strip()
	if not text:
		return None

	patterns = [
		r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},\s*\d{4}\b",
		r"\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}\b",
		r"\b\d{4}-\d{2}-\d{2}\b",
		r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+[A-Za-z]{3,9}\s+\d{1,2}(?:,\s*\d{4})?\b",
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


def _extract_event_date(soup) -> Optional[date]:
	for time_tag in soup.find_all("time"):
		if time_tag.has_attr("datetime"):
			try:
				dt = date_parser.parse(time_tag["datetime"])
				return dt.date()
			except Exception:
				continue

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

	text = soup.get_text(" ", strip=True)
	return _parse_date_text(text)


def _find_event_listing_container(anchor):
	for ancestor in anchor.parents:
		if not hasattr(ancestor, "get_text"):
			continue
		classes = ancestor.get("class") or []
		if any(isinstance(c, str) and c.lower().startswith("event") for c in classes):
			return ancestor
		text = ancestor.get_text(" ", strip=True)
		if len(text) > 80 and re.search(r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+[A-Za-z]{3,9}\s+\d{1,2}\b", text):
			return ancestor
	# fallback to the anchor's parent element
	return anchor.parent


def _extract_listing_title(text: str) -> str:
	if not text:
		return "PFL Event"
	# Remove date prefixes and common action labels
	text = re.sub(r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+[A-Za-z]{3,9}\s+\d{1,2}(?:,\s*\d{4})?\b", "", text, flags=re.IGNORECASE).strip()
	text = re.sub(r"\b(early card|main card|matchups|tickets|buy tickets|view results)\b.*", "", text, flags=re.IGNORECASE).strip()
	return text or "PFL Event"


def _build_listing_fallback(listing):
	fallbacks = {}
	for a in listing.find_all("a", href=True):
		href = a["href"]
		if "/events/" not in href:
			continue
		url = _canonical_url(urljoin(BASE, href))
		if url in fallbacks:
			continue
		container = _find_event_listing_container(a)
		text = container.get_text(" ", strip=True)
		parsed_date = _parse_date_text(text)
		fallbacks[url] = {
			"title": a.get_text(strip=True) or _extract_listing_title(text),
			"event_date": parsed_date,
		}
	return fallbacks


def get_pfl_events() -> List[FightEvent]:
	listing = fetch_html(f"{BASE}/events")
	if listing is None:
		logger.warning("Could not fetch PFL events listing")
		return []

	listing_fallbacks = _build_listing_fallback(listing)

	links = set()
	for a in listing.find_all("a", href=True):
		href = a["href"]
		if "/events/" not in href:
			continue
		url = _canonical_url(urljoin(BASE, href))
		if url == _canonical_url(urljoin(BASE, "/events")):
			continue
		links.add(url)

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
			page_title = title_tag.get_text(strip=True) if title_tag else None
			fallback = listing_fallbacks.get(url)
			title = page_title or (fallback["title"] if fallback else "PFL Event")
			event_date = _extract_event_date(soup) or (fallback["event_date"] if fallback else None)
			parsed = FightEvent(
				organization="PFL",
				event_name=title,
				slug=url,
				main_event=None,
				location=None,
				event_date=event_date,
				early_prelims=None,
				prelims=None,
				main_card=None,
				source_url=url,
			)

		events.append(parsed)

	return events

