"""Final official start-time resolution before falling back to all-day events.

Priority order:
1) SportsDataIO
2) Official PFL website
3) Official UFC website

Never estimates times. Returns timezone-aware datetimes converted to Asia/Riyadh.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, Optional
import re
from zoneinfo import ZoneInfo

from dateutil import parser as date_parser

from fetcher import fetch_html
from models import FightEvent
from timezone import RIYADH, to_riyadh
import ufc


def _parse_datetime_strict(value: str) -> Optional[datetime]:
    """Parse only explicit datetime strings (must include date+time)."""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    # Require an explicit time marker to avoid date-only inference.
    if "T" not in raw and not re.search(r"\b\d{1,2}:\d{2}\b", raw):
        return None
    try:
        dt = date_parser.parse(raw)
    except Exception:
        return None
    if dt.tzinfo is None:
        # No timezone in source -> cannot safely convert without inference.
        return None
    return dt


def _iter_jsonld_objects(soup) -> Iterable[Dict[str, Any]]:
    from fetcher import extract_json_ld

    for obj in extract_json_ld(soup):
        if not isinstance(obj, dict):
            continue
        graph = obj.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                if isinstance(item, dict):
                    yield item
        else:
            yield obj


def _resolve_from_sportsdata(event: FightEvent) -> Optional[datetime]:
    if (event.organization or "").upper() != "UFC":
        # This project's SportsDataIO integration is UFC-scoped.
        return None

    api_key = ufc._get_api_key()
    if not api_key:
        return None

    # UFC slug is the SportsData event id in this project.
    event_id = (event.slug or "").strip()
    if not event_id:
        return None

    detail = ufc._fetch_event_detail(api_key, event_id)
    if not isinstance(detail, dict):
        return None

    start = ufc._parse_event_start(detail)
    if start is None:
        return None
    return to_riyadh(start)


def _resolve_from_pfl_website(event: FightEvent) -> Optional[datetime]:
    url = (event.source_url or "").strip()
    if "pflmma.com" not in url:
        return None

    soup = fetch_html(url, timeout=10)
    if soup is None:
        return None

    # Prefer explicit displayed event time from the official page.
    # Example: "5:00 PM AST"
    tz_map = {
        "AST": "Asia/Riyadh",
        "KSA": "Asia/Riyadh",
        "ET": "America/New_York",
        "EDT": "America/New_York",
        "EST": "America/New_York",
        "CT": "America/Chicago",
        "CDT": "America/Chicago",
        "CST": "America/Chicago",
        "PT": "America/Los_Angeles",
        "PDT": "America/Los_Angeles",
        "PST": "America/Los_Angeles",
        "UTC": "UTC",
        "GMT": "UTC",
    }

    event_day = event.event_date
    if event_day is None:
        # Derive date from official JSON-LD when needed.
        for obj in _iter_jsonld_objects(soup):
            sd = obj.get("startDate")
            if isinstance(sd, str):
                try:
                    event_day = date_parser.parse(sd).date()
                    break
                except Exception:
                    continue

    if event_day is not None:
        time_nodes = soup.select(".event-info-time")
        for node in time_nodes:
            text = " ".join(node.get_text(" ", strip=True).split())
            m = re.search(r"(\d{1,2}(?::\d{2})?\s*(?:AM|PM))\s+([A-Z]{2,5})", text, flags=re.IGNORECASE)
            if not m:
                continue
            time_part = m.group(1).upper().replace(" ", "")
            tz_abbr = m.group(2).upper()
            tz_name = tz_map.get(tz_abbr)
            if not tz_name:
                continue
            try:
                parsed_t = date_parser.parse(time_part).time()
                local_dt = datetime(
                    event_day.year,
                    event_day.month,
                    event_day.day,
                    parsed_t.hour,
                    parsed_t.minute,
                    tzinfo=ZoneInfo(tz_name),
                )
                return local_dt.astimezone(RIYADH)
            except Exception:
                continue

    # Highest-confidence source: JSON-LD startDate with timezone.
    for obj in _iter_jsonld_objects(soup):
        sd = obj.get("startDate")
        dt = _parse_datetime_strict(sd) if isinstance(sd, str) else None
        if dt is not None:
            return dt.astimezone(RIYADH)

    # Fallback: explicit datetime attr in <time datetime="...">.
    for tag in soup.find_all("time"):
        raw = tag.get("datetime", "")
        dt = _parse_datetime_strict(raw)
        if dt is not None:
            return dt.astimezone(RIYADH)

    return None


def _resolve_from_ufc_website(event: FightEvent) -> Optional[datetime]:
    # 1) Direct event page timestamps (works for numbered events).
    url = (event.source_url or "").strip()
    if "ufc.com" in url:
        page_times = ufc._fetch_ufc_numbered_event_timestamps(url)
        if page_times.get("main_card") is not None:
            return page_times["main_card"]

    # 2) UFC official events listing overlay times.
    listing_index = ufc._fetch_ufc_events_listing_index()
    if listing_index:
        title_mm = ufc._extract_main_event_from_title(event.event_name)
        key = ufc._matchup_key(event.main_event) or ufc._matchup_key(title_mm or "")
        last_key = ufc._matchup_lastname_key(event.main_event) or ufc._matchup_lastname_key(title_mm or "")

        listing_entry = None
        if key and key in listing_index:
            listing_entry = listing_index[key]
        elif last_key and last_key in listing_index:
            listing_entry = listing_index[last_key]

        if listing_entry:
            times = listing_entry.get("times") or {}
            mc = times.get("main_card")
            if mc is not None:
                return mc

    # 3) UFC watch schedule (official source) for near-term cards.
    if event.event_date is not None:
        ws = ufc._fetch_ufc_watch_schedule()
        if ws:
            # event_date here is Riyadh date; still useful as final fallback lookup.
            ws_key = ufc._watch_schedule_key(event.event_name, event.event_date)
            if ws_key and ws_key in ws and ws[ws_key].get("main_card") is not None:
                return ws[ws_key]["main_card"]

    return None


def resolve_official_start_time(event: FightEvent) -> Optional[datetime]:
    """Resolve an official start time using source priority order.

    Priority:
    1) SportsDataIO
    2) Official PFL website
    3) Official UFC website
    """
    for resolver in (
        _resolve_from_sportsdata,
        _resolve_from_pfl_website,
        _resolve_from_ufc_website,
    ):
        try:
            dt = resolver(event)
        except Exception:
            dt = None
        if dt is not None:
            return to_riyadh(dt)
    return None
