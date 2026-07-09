"""UFC event provider.

Uses SportsDataIO MMA endpoints to fetch upcoming UFC events. The API key
must be provided via the SPORTSDATA_API_KEY environment variable.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
import os
import logging
import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import requests
from dateutil import parser as date_parser

from models import FightEvent
from timezone import RIYADH, to_riyadh

logger = logging.getLogger(__name__)

API_BASE = "https://api.sportsdata.io/v3/mma/scores/json"
SCHEDULE_ENDPOINT = f"{API_BASE}/Schedule/UFC"
EVENT_ENDPOINT = f"{API_BASE}/Event"
API_KEY_ENV = "SPORTSDATA_API_KEY"
EASTERN = ZoneInfo("America/New_York")
UTC = timezone.utc


def _get_api_key() -> Optional[str]:
    api_key = os.environ.get(API_KEY_ENV)
    if not api_key:
        logger.warning("Missing %s environment variable; UFC provider disabled", API_KEY_ENV)
    return api_key


def _build_headers(api_key: str) -> dict[str, str]:
    return {
        "Ocp-Apim-Subscription-Key": api_key,
        "Accept": "application/json",
    }


def _fetch_json(url: str, headers: dict[str, str], params: Optional[dict[str, Any]] = None) -> Optional[Any]:
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        logger.warning("SportsDataIO request failed for %s: %s", url, exc)
        return None


def _normalize_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    cleaned = re.sub(r"\s+", " ", text).strip()
    return cleaned or None


def _is_date_only(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()))


def _parse_datetime(value: Any, assume_tz: timezone | ZoneInfo = None) -> Optional[datetime]:
    """Parse a value into a datetime.  If the result is naive and ``assume_tz``
    is provided, that timezone is applied; otherwise falls back to Eastern."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = date_parser.parse(value)
        except Exception:
            return None
    else:
        return None

    if dt.tzinfo is None:
        tz = assume_tz if assume_tz is not None else EASTERN
        dt = dt.replace(tzinfo=tz)
    return dt


def _parse_event_start(event: Dict[str, Any]) -> Optional[datetime]:
    # DateTimeUTC is explicitly UTC — must not be treated as Eastern.
    utc_val = event.get("DateTimeUTC")
    if utc_val and not _is_date_only(utc_val):
        dt = _parse_datetime(utc_val, assume_tz=UTC)
        if dt is not None:
            return dt

    # DateTime is the local venue time — SportsDataIO uses Eastern for UFC.
    for key in ("DateTime", "StartDateTime", "StartDate", "EventDate"):
        value = event.get(key)
        if value is None:
            continue
        if _is_date_only(value):
            continue
        dt = _parse_datetime(value, assume_tz=EASTERN)
        if dt is not None:
            return dt
    return None


def _parse_event_date(event: Dict[str, Any]) -> Optional[date]:
    dt = _parse_event_start(event)
    if dt is not None:
        return to_riyadh(dt).date()

    for key in ("Date", "EventDate", "StartDate", "DateTime", "DateTimeUTC"):
        value = event.get(key)
        if not isinstance(value, str):
            continue
        if _is_date_only(value):
            try:
                return date_parser.parse(value).date()
            except Exception:
                continue
        try:
            dt = date_parser.parse(value)
            return dt.date()
        except Exception:
            continue
    return None


def _extract_event_name(event: Dict[str, Any]) -> str:
    for key in ("Name", "Title", "EventName", "ShortName", "Headline"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            cleaned = _normalize_text(value)
            if cleaned:
                return cleaned
    return "UFC Event"


def _extract_event_id(event: Dict[str, Any]) -> Optional[str]:
    for key in ("EventID", "EventId", "Id", "ID"):
        value = event.get(key)
        if value is not None:
            return str(value)
    return None


def _extract_location(event: Dict[str, Any]) -> Optional[str]:
    venue = event.get("Venue") or event.get("Arena") or event.get("Location")
    if isinstance(venue, str) and venue.strip():
        return _normalize_text(venue)
    if isinstance(venue, dict):
        candidates: list[str] = []
        for key in ("Name", "Venue", "Location", "City", "State", "Country"):
            value = venue.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())
        if candidates:
            return _normalize_text(", ".join(dict.fromkeys(candidates)))
    city = event.get("City")
    state = event.get("State")
    country = event.get("Country")
    parts = [part for part in (city, state, country) if isinstance(part, str) and part.strip()]
    if parts:
        return _normalize_text(", ".join(parts))
    return None


def _extract_fighter_name(fight: Dict[str, Any], prefix: str) -> Optional[str]:
    if fight is None or not isinstance(fight, dict):
        return None
    exact_keys = [f"{prefix}Name", f"{prefix}FullName", prefix, f"{prefix}ShortName"]
    for key in exact_keys:
        value = fight.get(key)
        if isinstance(value, str) and value.strip():
            return _normalize_text(value)
        if isinstance(value, dict):
            for nested in ("Name", "FullName", "DisplayName"):
                nested_value = value.get(nested)
                if isinstance(nested_value, str) and nested_value.strip():
                    return _normalize_text(nested_value)
    return None


def _extract_fight_text(fight: Dict[str, Any]) -> Optional[str]:
    left = _extract_fighter_name(fight, "Fighter1")
    right = _extract_fighter_name(fight, "Fighter2")
    if left and right:
        return f"{left} vs {right}"
    return None


def _extract_fights(event_detail: Dict[str, Any]) -> list[str]:
    if not isinstance(event_detail, dict):
        return []
    fights: list[str] = []
    for fight in event_detail.get("Fights") or []:
        fight_text = _extract_fight_text(fight)
        if fight_text:
            fights.append(fight_text)
    if fights:
        return fights

    for key in ("FightCard", "Card", "FightsList"):
        for fight in event_detail.get(key) or []:
            fight_text = _extract_fight_text(fight)
            if fight_text:
                fights.append(fight_text)
    return fights


def _build_source_url(event: Dict[str, Any], event_id: Optional[str]) -> Optional[str]:
    source_url = event.get("Url") or event.get("url") or event.get("EventUrl") or event.get("OfficialUrl")
    if isinstance(source_url, str) and source_url.strip():
        return _normalize_text(source_url)
    if event_id:
        return f"{EVENT_ENDPOINT}/{event_id}"
    return None


def _build_fight_list(event_detail: Dict[str, Any]) -> Optional[str]:
    fights = _extract_fights(event_detail)
    if not fights:
        return None
    return "\n".join(fights)


def _build_main_event(event_detail: Dict[str, Any]) -> Optional[str]:
    fights = _extract_fights(event_detail)
    if fights:
        return fights[0]
    return None


def _fetch_event_detail(api_key: str, event_id: str) -> Optional[Dict[str, Any]]:
    headers = _build_headers(api_key)
    url = f"{EVENT_ENDPOINT}/{event_id}"
    result = _fetch_json(url, headers)
    if isinstance(result, dict):
        return result
    return None


def _build_event(schedule_event: Dict[str, Any], api_key: str) -> Optional[FightEvent]:
    event_id = _extract_event_id(schedule_event)
    if not event_id:
        return None

    event_detail = _fetch_event_detail(api_key, event_id) or {}

    start = _parse_event_start(schedule_event) or _parse_event_start(event_detail)
    main_card = to_riyadh(start) if start else None
    event_date = to_riyadh(start).date() if start else _parse_event_date(schedule_event)
    if event_date is None:
        return None

    event_name = _extract_event_name(schedule_event)
    location = _extract_location(schedule_event) or _extract_location(event_detail)
    fight_list = _build_fight_list(event_detail)
    main_event = _build_main_event(event_detail) or (fight_list.splitlines()[0] if fight_list else None)
    source_url = _build_source_url(event_detail, event_id)

    return FightEvent(
        organization="UFC",
        event_name=event_name,
        slug=event_id,
        main_event=main_event,
        fight_list=fight_list,
        location=location,
        event_date=event_date,
        early_prelims=None,
        prelims=None,
        main_card=main_card,
        source_url=source_url,
    )


def _is_future_event(event: FightEvent) -> bool:
    today = datetime.now(RIYADH).date()
    if event.main_card is not None:
        return to_riyadh(event.main_card).date() >= today
    if event.event_date is not None:
        return event.event_date >= today
    return False


def get_ufc_events() -> List[FightEvent]:
    api_key = _get_api_key()
    if not api_key:
        return []

    headers = _build_headers(api_key)
    now = datetime.now(RIYADH)
    current_year = now.year
    next_year = current_year + 1
    event_ids: set[str] = set()
    events: List[FightEvent] = []

    for season in (current_year, next_year):
        url = f"{SCHEDULE_ENDPOINT}/{season}"
        schedule = _fetch_json(url, headers)
        if not isinstance(schedule, list):
            continue

        for schedule_event in schedule:
            if not isinstance(schedule_event, dict):
                continue
            event_id = _extract_event_id(schedule_event)
            if not event_id or event_id in event_ids:
                continue

            event = _build_event(schedule_event, api_key)
            if event is None:
                continue
            if not _is_future_event(event):
                continue

            event_ids.add(event_id)
            events.append(event)

    return sorted(events, key=lambda ev: ev.main_card or datetime.combine(ev.event_date, datetime.min.time(), tzinfo=RIYADH))
