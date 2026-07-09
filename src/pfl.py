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
from datetime import date, datetime

from dateutil import parser as date_parser

from models import FightEvent
from fetcher import fetch_html, extract_json_ld
from timezone import to_riyadh, RIYADH

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


def _is_future_event(event: FightEvent) -> bool:
	today = datetime.now(RIYADH).date()
	if event.event_date is not None:
		return event.event_date >= today
	for dt in (event.main_card, event.prelims, event.early_prelims):
		if dt is not None and to_riyadh(dt).date() >= today:
			return True
	return False


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
	text = re.sub(r"\b(?:early card|main card|matchups|tickets|buy tickets|view results|learn more|register|learn more)\b.*", "", text, flags=re.IGNORECASE).strip()
	return text or "PFL Event"


def _normalize_pfl_text(text: Optional[str]) -> Optional[str]:
	if not text:
		return None
	cleaned = re.sub(r"\s+", " ", text).strip()
	return cleaned if cleaned else None


def _is_invalid_pfl_title(text: Optional[str]) -> bool:
	if not text:
		return True
	cleaned = _normalize_pfl_text(text)
	if not cleaned:
		return True
	if re.fullmatch(r"(?i)(buy tickets|tickets|pfl event|register|watch live|live|previous|next)", cleaned):
		return True
	if re.search(r"\b(?:buy tickets|buy now|register|watch live|view tickets|watch now|live|previous|next)\b", cleaned, flags=re.IGNORECASE):
		return True
	return False


def _clean_pfl_title(text: Optional[str]) -> Optional[str]:
	cleaned = _normalize_pfl_text(text)
	if not cleaned:
		return None
	# Split on common separators and evaluate each candidate separately.
	parts = [re.sub(r"[\|•\-]", " ", part).strip() for part in re.split(r"[\|•\-]", cleaned)]
	candidates = []
	for part in parts:
		if not part:
			continue
		part_clean = re.sub(r"\b(?:buy tickets|buy now|register|watch live|view tickets|watch now|live|previous|next)\b", "", part, flags=re.IGNORECASE)
		part_clean = re.sub(r"\s+", " ", part_clean).strip()
		if not part_clean:
			continue
		if _is_invalid_pfl_title(part_clean):
			continue
		candidates.append(part_clean)
	if candidates:
		for candidate in candidates:
			if re.match(r"(?i)^pfl\b", candidate):
				return candidate
		return max(candidates, key=lambda c: len(c))

	# If the full cleaned string is valid, use it.
	if not _is_invalid_pfl_title(cleaned):
		return cleaned
	return None


def _extract_pfl_title(soup) -> Optional[str]:
	# Prefer JSON-LD name
	jsonlds = extract_json_ld(soup)
	for obj in jsonlds:
		name = obj.get("name") if isinstance(obj.get("name"), str) else None
		if name:
			validated = _clean_pfl_title(name)
			if validated:
				return validated

	# Prefer OG title
	meta = soup.find("meta", attrs={"property": "og:title"})
	if meta and meta.get("content"):
		validated = _clean_pfl_title(meta["content"])
		if validated:
			return validated

	# Prefer Twitter title
	meta = soup.find("meta", attrs={"name": "twitter:title"})
	if meta and meta.get("content"):
		validated = _clean_pfl_title(meta["content"])
		if validated:
			return validated

	# If page is on Webook, prefer heading over button text
	if "webook.com" in (soup.find("meta", attrs={"property": "og:url"}) or {}).get("content", "").lower():
		title_tag = soup.find("h1")
		if title_tag:
			title = title_tag.get_text(" ", strip=True)
			validated = _clean_pfl_title(title)
			if validated:
				return validated

	# Prefer H1
	title_tag = soup.find("h1")
	if title_tag:
		title = title_tag.get_text(" ", strip=True)
		validated = _clean_pfl_title(title)
		if validated:
			return validated

	# Prefer title tag
	title_tag = soup.find("title")
	if title_tag and title_tag.string:
		title = title_tag.string.strip()
		validated = _clean_pfl_title(title)
		if validated:
			return validated

	return None


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
		listing_title = _extract_listing_title(text)
		anchor_title = _normalize_pfl_text(a.get_text(" ", strip=True))
		if listing_title and not _is_invalid_pfl_title(listing_title):
			title = listing_title
		elif anchor_title and not _is_invalid_pfl_title(anchor_title):
			title = anchor_title
		else:
			title = listing_title or anchor_title

		fallbacks[url] = {
			"title": title,
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
			title = _extract_pfl_title(soup)
			fallback = listing_fallbacks.get(url)
			fallback_title = fallback["title"] if fallback else None
			if title is None:
				title = _clean_pfl_title(fallback_title)
			if title is None and fallback_title and not _is_invalid_pfl_title(fallback_title):
				title = fallback_title
			if title is not None and title.strip().upper() == "PFL EVENT":
				title = None
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

		if not _is_future_event(parsed):
			logger.debug("Skipping past or undated PFL event: %s", url)
			continue

		events.append(parsed)

	return events

