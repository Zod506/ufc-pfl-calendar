"""UFC event provider.

Uses SportsDataIO MMA endpoints to fetch upcoming UFC events. The API key
must be provided via the SPORTSDATA_API_KEY environment variable.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import os
import logging
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from dateutil import parser as date_parser

from fetcher import fetch_html
from models import FightEvent
from timezone import RIYADH, to_riyadh

logger = logging.getLogger(__name__)

API_BASE = "https://api.sportsdata.io/v3/mma/scores/json"
SCHEDULE_ENDPOINT = f"{API_BASE}/Schedule/UFC"
EVENT_ENDPOINT = f"{API_BASE}/Event"
API_KEY_ENV = "SPORTSDATA_API_KEY"
EASTERN = ZoneInfo("America/New_York")
UTC = timezone.utc

# Fallback URL shown to users when no specific event page can be determined.
UFC_EVENTS_URL = "https://www.ufc.com/events"

# Source for card-segment timing (Early Prelims / Prelims / Main Card).
UFC_WATCH_SCHEDULE_URL = "https://www.ufc.com/watch/schedule"

# Timezone abbreviations used on UFC.com pages.
_UFC_TZ_MAP: Dict[str, str] = {
    "EDT": "America/New_York",
    "EST": "America/New_York",
    "CDT": "America/Chicago",
    "CST": "America/Chicago",
    "MDT": "America/Denver",
    "MST": "America/Denver",
    "PDT": "America/Los_Angeles",
    "PST": "America/Los_Angeles",
    "BST": "Europe/London",
    "GMT": "UTC",
    "UTC": "UTC",
    "CEST": "Europe/Paris",
    "CET": "Europe/Paris",
    "EEST": "Europe/Helsinki",
    "EET": "Europe/Helsinki",
    "GST": "Asia/Dubai",
    "AST": "Asia/Riyadh",
    "AEST": "Australia/Sydney",
    "AEDT": "Australia/Sydney",
    "JST": "Asia/Tokyo",
}

_US_STATE_ABBR: Dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}

# Matches one entry on the watch/schedule page, e.g.:
# "Jul 11 Sat UFC 329 Early Prelims Sat Jul 11 17  EDT"
# "Jul 18 Sat UFC Fight Night Prelims Sat Jul 18 17  EDT"
_WATCH_ENTRY_RE = re.compile(
    r"(?P<month>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(?P<day>\d{1,2})\s+"
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+"
    r"(?P<event>UFC\s+\d+|UFC\s+Fight\s+Night(?:\s*[:\-]\s*.+?)?)\s+"
    r"(?P<card>Early\s+Prelims?|Prelims?|Main\s+Card)\s+"
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\s+"
    r"(?P<hour>\d{1,2})"
    r"(?::(?P<minute>\d{2}))?\s*"
    r"(?P<ampm>AM|PM)?\s+"
    r"(?P<tz>[A-Z]{2,5})",
    re.IGNORECASE,
)


def _normalize_watch_event_short(raw_event: str) -> str:
    normalized = re.sub(r"\s+", " ", (raw_event or "")).strip().upper()
    m = re.match(r"^(UFC\s+\d+)\b", normalized)
    if m:
        return m.group(1)
    if normalized.startswith("UFC FIGHT NIGHT"):
        return "UFC FIGHT NIGHT"
    return normalized


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


def _normalize_country(country: Optional[str]) -> Optional[str]:
    c = _normalize_text(country)
    if not c:
        return None
    lc = c.lower()
    if lc in ("united states", "us", "usa", "u.s.a."):
        return "USA"
    if lc in ("united arab emirates", "uae"):
        return "UAE"
    return c


def _normalize_region(state: Optional[str], country: Optional[str]) -> Optional[str]:
    s = _normalize_text(state)
    if not s:
        return None
    if _normalize_country(country) == "USA":
        up = s.upper()
        return _US_STATE_ABBR.get(up, s)
    return s


def _format_venue_lines(
    venue_name: Optional[str], city: Optional[str], state: Optional[str], country: Optional[str]
) -> Optional[str]:
    first = _normalize_text(venue_name)
    region = _normalize_region(state, country)
    country_name = _normalize_country(country)
    locality_parts = [
        _normalize_text(city),
        region,
        country_name,
    ]
    locality = ", ".join([p for p in locality_parts if p])
    if first and locality:
        return f"{first}\n{locality}"
    if first:
        return first
    if locality:
        return locality
    return None


def _split_single_line_venue(text: Optional[str]) -> Optional[str]:
    """Convert one-line venue text into a two-line layout when possible."""
    cleaned = _normalize_text(text)
    if not cleaned:
        return None
    if "\n" in cleaned:
        return cleaned
    if "," in cleaned:
        left, right = cleaned.split(",", 1)
        left = left.strip()
        right = right.strip()
        if left and right:
            return f"{left}\n{right}"
    return cleaned


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
        city = event.get("City") if isinstance(event.get("City"), str) else None
        state = event.get("State") if isinstance(event.get("State"), str) else None
        country = event.get("Country") if isinstance(event.get("Country"), str) else None
        return _format_venue_lines(venue, city, state, country)
    if isinstance(venue, dict):
        venue_name = venue.get("Name") or venue.get("Venue") or venue.get("Location")
        city = venue.get("City") or event.get("City")
        state = venue.get("State") or event.get("State")
        country = venue.get("Country") or event.get("Country")
        formatted = _format_venue_lines(
            venue_name if isinstance(venue_name, str) else None,
            city if isinstance(city, str) else None,
            state if isinstance(state, str) else None,
            country if isinstance(country, str) else None,
        )
        if formatted:
            return formatted
        candidates: list[str] = []
        for key in ("Name", "Venue", "Location", "City", "State", "Country"):
            value = venue.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())
        if candidates:
            return _split_single_line_venue(_normalize_text(", ".join(dict.fromkeys(candidates))))
    city = event.get("City")
    state = event.get("State")
    country = event.get("Country")
    parts = [part for part in (city, state, country) if isinstance(part, str) and part.strip()]
    if parts:
        return _format_venue_lines(None, city if isinstance(city, str) else None, state if isinstance(state, str) else None, country if isinstance(country, str) else None)
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
    if not (left and right):
        fighters = fight.get("Fighters") if isinstance(fight, dict) else None
        if isinstance(fighters, list) and len(fighters) >= 2:
            names = []
            for f in fighters[:2]:
                if not isinstance(f, dict):
                    continue
                first = _normalize_text(f.get("FirstName"))
                last = _normalize_text(f.get("LastName"))
                name = _normalize_text(" ".join([p for p in (first, last) if p]))
                if name:
                    names.append(name)
            if len(names) >= 2:
                left, right = names[0], names[1]
    if left and right:
        return f"{left} vs {right}"
    return None


def _matchup_key(matchup: Optional[str]) -> Optional[tuple[str, str]]:
    if not matchup or " vs " not in matchup.lower():
        return None
    parts = re.split(r"\s+v(?:s)?\.?\s+", matchup, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) != 2:
        return None

    def norm(name: str) -> str:
        return re.sub(r"[^a-z0-9]", "", name.lower())

    a = norm(parts[0])
    b = norm(parts[1])
    if not a or not b:
        return None
    return tuple(sorted((a, b)))


def _matchup_lastname_key(matchup: Optional[str]) -> Optional[tuple[str, str]]:
    if not matchup or " vs " not in matchup.lower():
        return None
    parts = re.split(r"\s+v(?:s)?\.?\s+", matchup, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) != 2:
        return None

    def norm_last(name: str) -> str:
        tokens = [t for t in re.split(r"\s+", name.strip()) if t]
        if not tokens:
            return ""
        last = tokens[-1]
        return re.sub(r"[^a-z0-9]", "", last.lower())

    a = norm_last(parts[0])
    b = norm_last(parts[1])
    if not a or not b:
        return None
    return tuple(sorted((a, b)))


def _extract_matchup_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"([A-Za-z0-9'.\- ]{2,60}?)\s+v(?:s)?\.?\s+([A-Za-z0-9'.\- ]{2,60})", text, flags=re.IGNORECASE)
    if not m:
        return None
    left = _normalize_text(m.group(1))
    right = _normalize_text(m.group(2))
    if left and right:
        return f"{left} vs {right}"
    return None


def _parse_division_from_label(label: Optional[str]) -> Optional[str]:
    cleaned = _normalize_text(label)
    if not cleaned:
        return None
    if "scrambled" in cleaned.lower():
        return None
    cleaned = re.sub(r"#\d+", "", cleaned)
    cleaned = re.sub(r"\b(?:title\s+bout|championship\s+bout|bout)\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
    return cleaned or None


def _is_official_championship_label(label: Optional[str]) -> bool:
    if not label:
        return False
    ll = label.lower()
    return any(k in ll for k in ("title bout", "championship", "interim title", "interim"))


def _championship_name_from_label(label: Optional[str], division: Optional[str]) -> Optional[str]:
    if not _is_official_championship_label(label):
        return None
    prefix = "Interim UFC" if label and "interim" in label.lower() else "UFC"
    if division:
        return f"{prefix} {division} Championship"
    return f"{prefix} Championship"


def _extract_ufc_metadata_from_api(
    event_detail: Dict[str, Any],
    main_event: Optional[str],
) -> tuple[Optional[str], Optional[str], bool, Optional[str]]:
    """Return (co_main_event, main_division, is_championship, championship_name)."""
    co_main_event: Optional[str] = None
    main_division: Optional[str] = None
    main_is_championship = False
    championship_name: Optional[str] = None
    target_key = _matchup_key(main_event)

    fights = event_detail.get("Fights") or []
    for fight in fights:
        if not isinstance(fight, dict):
            continue
        matchup = _extract_fight_text(fight)
        if not matchup:
            continue
        segment = _normalize_text(fight.get("CardSegment"))
        weight_class = _normalize_text(fight.get("WeightClass"))

        if segment and "co-main" in segment.lower() and not co_main_event:
            co_main_event = matchup

        if target_key and _matchup_key(matchup) == target_key:
            main_division = _parse_division_from_label(weight_class or segment)
            main_is_championship = _is_official_championship_label(weight_class) or _is_official_championship_label(segment)
            championship_name = _championship_name_from_label(weight_class or segment, main_division)

    return co_main_event, main_division, main_is_championship, championship_name


def _fetch_ufc_event_page_metadata(event_url: str, main_event: Optional[str]) -> Dict[str, Any]:
    """Extract venue/division/championship/co-main from official UFC event page."""
    out: Dict[str, Any] = {
        "location": None,
        "co_main_event": None,
        "main_event_division": None,
        "main_event_is_championship": False,
        "main_event_championship_name": None,
    }
    if not event_url:
        return out
    try:
        soup = fetch_html(event_url, timeout=10)
        if soup is None:
            return out
        title_tag = soup.find("title")
        if title_tag and title_tag.string and "search" in title_tag.string.lower():
            return out

        venue_node = soup.find("div", class_=re.compile(r"field--name-venue|c-hero__text|hero-fixed-bar__place"))
        if venue_node:
            out["location"] = _split_single_line_venue(venue_node.get_text(" ", strip=True))

        target_key = _matchup_key(main_event)
        for li in soup.select("li.l-listing__item"):
            fighters = [
                _normalize_text(n.get_text(" ", strip=True))
                for n in li.select(".c-listing-fight__corner-name")
            ]
            fighters = [n for n in fighters if n]
            if len(fighters) < 2:
                continue
            matchup = f"{fighters[0]} vs {fighters[1]}"

            class_node = li.select_one(".c-listing-fight__class-text")
            class_text = class_node.get_text(" ", strip=True) if class_node else ""
            li_text = li.get_text(" ", strip=True).lower()
            is_explicit_main = "main event" in li_text
            is_explicit_co_main = "co-main" in li_text

            if is_explicit_co_main and not out["co_main_event"]:
                out["co_main_event"] = matchup

            if target_key and _matchup_key(matchup) == target_key:
                division = _parse_division_from_label(class_text)
                out["main_event_division"] = division
                out["main_event_is_championship"] = _is_official_championship_label(class_text) or _is_official_championship_label(li_text)
                out["main_event_championship_name"] = _championship_name_from_label(class_text or li_text, division)
                if is_explicit_main:
                    # Keep explicit marker if available for future resilience.
                    out["main_event_division"] = out["main_event_division"]
    except Exception as exc:
        logger.debug("Could not parse UFC event metadata from %s: %s", event_url, exc)
    return out


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


def _parse_listing_month_day(text: Optional[str]) -> Optional[date]:
    raw = _normalize_text(text)
    if not raw:
        return None
    m = re.search(r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),?\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})\b", raw, flags=re.IGNORECASE)
    if not m:
        return None
    month_str = m.group(1)
    day = int(m.group(2))
    today_eastern = datetime.now(EASTERN).date()
    year = today_eastern.year
    try:
        candidate = date_parser.parse(f"{month_str} {day} {year}").date()
        if candidate < today_eastern - timedelta(days=7):
            year += 1
        return date_parser.parse(f"{month_str} {day} {year}").date()
    except Exception:
        return None


def _parse_listing_time_value(text: str, event_date: Optional[date]) -> Optional[datetime]:
    if not text or event_date is None:
        return None
    m = re.search(r"(\d{1,2}:\d{2}\s*(?:AM|PM))\s+([A-Z]{2,5})", text, flags=re.IGNORECASE)
    if not m:
        return None
    time_str = m.group(1).upper().replace(" ", "")
    tz_abbr = m.group(2).upper()
    tz_name = _UFC_TZ_MAP.get(tz_abbr, "America/New_York")
    try:
        tm = date_parser.parse(time_str).time()
        dt = datetime(event_date.year, event_date.month, event_date.day, tm.hour, tm.minute, tzinfo=ZoneInfo(tz_name))
        return dt.astimezone(RIYADH)
    except Exception:
        return None


def _extract_listing_venue(article) -> Optional[str]:
    if article is None:
        return None
    venue_node = article.select_one(".field--name-venue")
    loc_node = article.select_one(".field--name-location")
    venue_text = _normalize_text(venue_node.get_text(" ", strip=True)) if venue_node else None
    loc_text = _normalize_text(loc_node.get_text(" ", strip=True)) if loc_node else None
    if not venue_text and not loc_text:
        return None

    city = None
    state = None
    country = None
    if loc_text:
        loc_clean = re.sub(r"\s*,\s*", ", ", loc_text)
        m = re.search(r"(?P<city>[A-Za-z .'-]+),\s*(?P<state>[A-Za-z]{2}|[A-Za-z .'-]+)\s+(?P<country>United\s+States|United\s+Arab\s+Emirates|[A-Za-z .'-]+)$", loc_clean, flags=re.IGNORECASE)
        if m:
            city = m.group("city").strip()
            state = m.group("state").strip()
            country = m.group("country").strip()
        else:
            parts = [p.strip() for p in re.split(r",", loc_clean) if p.strip()]
            if parts:
                city = parts[0]
            if len(parts) > 1:
                state = parts[1]

    return _format_venue_lines(venue_text, city, state, country)


def _fetch_ufc_events_listing_index() -> Dict[tuple[str, str], Dict[str, Any]]:
    """Return matchup-keyed index from official UFC events listing."""
    out: Dict[tuple[str, str], Dict[str, Any]] = {}
    soup = fetch_html(UFC_EVENTS_URL, timeout=12)
    if soup is None:
        return out

    for article in soup.select("article.c-card-event--result"):
        link = article.find("a", href=True)
        if not link:
            continue
        href = _normalize_text(link.get("href"))
        if not href or "/event/" not in href:
            continue
        url = href if href.startswith("http") else f"https://www.ufc.com{href}"

        headline_node = article.select_one(".c-card-event--result__headline")
        headline = _normalize_text(headline_node.get_text(" ", strip=True)) if headline_node else None
        mm = _extract_matchup_from_text(headline or "")
        if not mm:
            continue
        key = _matchup_key(mm)
        if not key:
            continue

        listing_item = article.find_parent("div", class_="l-listing__item")
        event_date = None
        date_node = article.select_one(".c-card-event--result__date")
        if date_node:
            event_date = _parse_listing_month_day(date_node.get_text(" ", strip=True))

        times: Dict[str, Optional[datetime]] = {
            "early_prelims": None,
            "prelims": None,
            "main_card": None,
        }
        if listing_item is not None:
            for li in listing_item.select(".c-how-to-watch--event-main-card-list li"):
                label_node = li.select_one(".c-listing-viewing-option__fight-card")
                time_node = li.select_one(".c-listing-viewing-option__time")
                if not label_node or not time_node:
                    continue
                label = _normalize_text(label_node.get_text(" ", strip=True) or "")
                val = None
                ts = time_node.get("data-timestamp")
                if ts:
                    try:
                        val = datetime.fromtimestamp(int(ts), tz=UTC).astimezone(RIYADH)
                    except Exception:
                        val = None
                if val is None:
                    val = _parse_listing_time_value(time_node.get_text(" ", strip=True), event_date)

                if not val:
                    continue
                ll = label.lower()
                if "early" in ll:
                    times["early_prelims"] = val
                elif "prelim" in ll:
                    times["prelims"] = val
                elif "main" in ll:
                    times["main_card"] = val

        entry = {
            "event_name": _normalize_text(headline) or _normalize_text(link.get_text(" ", strip=True)) or "UFC Event",
            "main_event": mm,
            "url": url,
            "event_date": event_date,
            "venue": _extract_listing_venue(article),
            "times": times,
        }
        out[key] = entry

        # Also index by surname-only key so full-name API matchups can map to
        # official listing cards that use surname headlines.
        last_key = _matchup_lastname_key(mm)
        if last_key:
            out[last_key] = entry

    return out


# ── Card-timing helpers ────────────────────────────────────────────────────────

def _watch_schedule_key(event_name: str, eastern_date: date) -> Optional[Tuple[str, date]]:
    """Build a lookup key for the watch-schedule dict.

    Normalises: "UFC 329: McGregor vs. Holloway 2" → ("UFC 329", date)
                "UFC Fight Night: Ankalaev vs. ..."  → ("UFC FIGHT NIGHT", date)
    """
    name = (event_name or "").upper().strip()
    m = re.match(r"^(UFC\s+\d+)", name)
    if m:
        return (re.sub(r"\s+", " ", m.group(1)).strip(), eastern_date)
    if "UFC FIGHT NIGHT" in name:
        return ("UFC FIGHT NIGHT", eastern_date)
    return None


def _fetch_ufc_watch_schedule() -> Dict[Tuple[str, date], Dict[str, Optional[datetime]]]:
    """Fetch https://www.ufc.com/watch/schedule and extract card-segment times.

    The page contains entries like:
      "Jul 11 Sat UFC 329 Early Prelims Sat Jul 11 17  EDT"
      "Jul 18 Sat UFC Fight Night Prelims Sat Jul 18 17  EDT"

    Returns {(event_short, eastern_date): {early_prelims, prelims, main_card}}
    with all datetimes converted to Asia/Riyadh.
    """
    result: Dict[Tuple[str, date], Dict[str, Optional[datetime]]] = {}
    try:
        soup = fetch_html(UFC_WATCH_SCHEDULE_URL, timeout=12)
        if soup is None:
            logger.warning("Could not fetch UFC watch schedule")
            return result

        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        today_eastern = datetime.now(ZoneInfo("America/New_York")).date()

        for m in _WATCH_ENTRY_RE.finditer(text):
            month_str = m.group("month")
            day = int(m.group("day"))
            event_short = _normalize_watch_event_short(m.group("event"))
            card_label = m.group("card").strip().lower()
            hour = int(m.group("hour"))
            minute = int(m.group("minute") or 0)
            ampm = (m.group("ampm") or "").upper()
            tz_abbr = m.group("tz").upper()

            # Watch schedule sometimes emits 12-hour values with AM/PM.
            if ampm:
                if hour == 12:
                    hour = 0
                if ampm == "PM":
                    hour += 12

            tz_name = _UFC_TZ_MAP.get(tz_abbr, "America/New_York")
            tz = ZoneInfo(tz_name)

            # Infer calendar year from today (all entries are "Upcoming").
            year = today_eastern.year
            try:
                candidate = date_parser.parse(f"{month_str} {day} {year}").date()
                if candidate < today_eastern - timedelta(days=7):
                    year += 1
                eastern_date = date_parser.parse(f"{month_str} {day} {year}").date()
                event_dt = datetime(eastern_date.year, eastern_date.month,
                                    eastern_date.day, hour, minute, tzinfo=tz)
                riyadh_dt = event_dt.astimezone(RIYADH)
            except Exception:
                continue

            key: Tuple[str, date] = (event_short, eastern_date)
            if key not in result:
                result[key] = {"early_prelims": None, "prelims": None, "main_card": None}

            lc = card_label.replace(" ", "_")
            if "early" in lc:
                result[key]["early_prelims"] = riyadh_dt
            elif "prelim" in lc:
                result[key]["prelims"] = riyadh_dt
            elif "main" in lc:
                result[key]["main_card"] = riyadh_dt

        logger.info("UFC watch schedule: %d event entries parsed", len(result))
    except Exception as exc:
        logger.warning("Error parsing UFC watch schedule: %s", exc)
    return result


def _fetch_ufc_numbered_event_timestamps(event_url: str) -> Dict[str, Optional[datetime]]:
    """Scrape card-segment times from a numbered UFC event page (e.g. /event/ufc-329).

    Uses the Unix ``data-timestamp`` attribute on
    ``c-listing-viewing-option__time`` elements for timezone-safe conversion.

    Only works for numbered PPV events where the URL pattern is reliable.
    Returns {early_prelims, prelims, main_card} (values may be None).
    """
    result: Dict[str, Optional[datetime]] = {
        "early_prelims": None,
        "prelims": None,
        "main_card": None,
    }
    if not event_url or "fight-night" in event_url.lower():
        # Fight Night slugs are unreliable — skip to avoid landing on search page.
        return result
    try:
        soup = fetch_html(event_url, timeout=10)
        if soup is None:
            return result

        # Detect if we landed on a search/404 page.
        title_tag = soup.find("title")
        if title_tag and title_tag.string and "search" in title_tag.string.lower():
            logger.debug("UFC event page %s returned search page — skipping", event_url)
            return result

        how_to_div = soup.find("div", class_="c-how-to-watch--event-main-card-list")
        if how_to_div is None:
            return result

        for li in how_to_div.find_all("li"):
            label_div = li.find(class_="c-listing-viewing-option__fight-card")
            time_div = li.find(class_="c-listing-viewing-option__time")
            if not label_div or not time_div:
                continue

            label = label_div.get_text(" ", strip=True).lower().replace(" ", "_")
            ts_str = time_div.get("data-timestamp")
            if not ts_str:
                continue
            try:
                ts = int(ts_str)
                riyadh_dt = datetime.fromtimestamp(ts, tz=UTC).astimezone(RIYADH)
            except (ValueError, OSError, OverflowError):
                continue

            if "early" in label:
                result["early_prelims"] = riyadh_dt
            elif "prelim" in label:
                result["prelims"] = riyadh_dt
            elif "main" in label:
                result["main_card"] = riyadh_dt

        filled = sum(1 for v in result.values() if v is not None)
        logger.debug("UFC event page %s: %d segment times found", event_url, filled)

    except Exception as exc:
        logger.debug("Error fetching UFC event page %s: %s", event_url, exc)
    return result


def _extract_main_event_from_title(event_name: str) -> Optional[str]:
    """Extract 'Fighter A vs Fighter B' from titles like 'UFC 329: A vs. B'.

    Returns None when the title does not contain a clearly formatted matchup.
    Does not overwrite a value already obtained from the API fight list.
    """
    if not event_name:
        return None
    # Look for everything after the first ": " that contains "vs"
    m = re.search(r":\s*(.+\bvs\.?\s+.+)$", event_name, re.IGNORECASE)
    if not m:
        return None
    matchup = m.group(1).strip()
    # Normalize "vs." to "vs"
    matchup = re.sub(r"\bvs\.\s*", "vs ", matchup)
    matchup = re.sub(r"\s+", " ", matchup).strip().rstrip(".")
    return matchup or None


def _is_api_url(url: Optional[str]) -> bool:
    """Return True when *url* is an internal API endpoint users should not see."""
    return bool(url and "sportsdata.io" in url)


def _build_ufc_event_url(event_name: str) -> Optional[str]:
    """Construct an official ufc.com event page URL from the event name.

    "UFC 329: …"          → https://www.ufc.com/event/ufc-329
    "UFC Fight Night: A vs. B" → https://www.ufc.com/event/ufc-fight-night-a-vs-b
    Returns None when a clean slug cannot be derived.
    """
    name = _normalize_text(event_name) or ""
    # Numbered UFC event: "UFC 329" or "UFC 329: …"
    m = re.match(r"^UFC\s+(\d+)\b", name, re.IGNORECASE)
    if m:
        return f"https://www.ufc.com/event/ufc-{m.group(1)}"
    # UFC Fight Night: "UFC Fight Night: Fighter A vs. Fighter B"
    m = re.match(r"^UFC\s+Fight\s+Night\s*[:\-]?\s*(.+)$", name, re.IGNORECASE)
    if m:
        after = m.group(1).strip()
        slug = after.lower()
        slug = re.sub(r"\bvs\.\s*", "vs-", slug)
        slug = re.sub(r"[^a-z0-9\s\-]", "", slug)
        slug = re.sub(r"[\s]+", "-", slug.strip())
        slug = re.sub(r"-{2,}", "-", slug).strip("-")
        if slug:
            return f"https://www.ufc.com/event/ufc-fight-night-{slug}"
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


def _build_event(
    schedule_event: Dict[str, Any],
    api_key: str,
    watch_schedule: Optional[Dict] = None,
    listing_index: Optional[Dict[tuple[str, str], Dict[str, Any]]] = None,
) -> Optional[FightEvent]:
    event_id = _extract_event_id(schedule_event)
    if not event_id:
        return None

    event_detail = _fetch_event_detail(api_key, event_id) or {}

    start = _parse_event_start(schedule_event) or _parse_event_start(event_detail)
    main_card_dt = to_riyadh(start) if start else None
    event_date = to_riyadh(start).date() if start else _parse_event_date(schedule_event)
    if event_date is None:
        return None

    # Eastern date of the event (US calendar date, used for watch-schedule lookup).
    eastern_start = start.astimezone(EASTERN) if start else None
    eastern_date: date = eastern_start.date() if eastern_start else event_date

    event_name = _extract_event_name(schedule_event)
    location = _extract_location(schedule_event) or _extract_location(event_detail)
    fight_list = _build_fight_list(event_detail)

    # Main event: prefer API fight data, fall back to event title.
    main_event = (
        _build_main_event(event_detail)
        or (fight_list.splitlines()[0] if fight_list else None)
        or _extract_main_event_from_title(event_name)
    )

    co_main_event, main_event_division, main_event_is_championship, main_event_championship_name = _extract_ufc_metadata_from_api(
        event_detail, main_event
    )

    listing_entry = None
    mm_key = _matchup_key(main_event) or _matchup_key(_extract_main_event_from_title(event_name) or "")
    mm_last_key = _matchup_lastname_key(main_event) or _matchup_lastname_key(_extract_main_event_from_title(event_name) or "")
    if listing_index:
        if mm_key and mm_key in listing_index:
            listing_entry = listing_index[mm_key]
        elif mm_last_key and mm_last_key in listing_index:
            listing_entry = listing_index[mm_last_key]

    # Build a user-facing URL — never expose internal API endpoints.
    raw_url = _build_source_url(event_detail, event_id)
    if _is_api_url(raw_url):
        raw_url = None
    ufc_page_url = (
        raw_url if raw_url and "ufc.com" in raw_url
        else _build_ufc_event_url(event_name)
    )
    if listing_entry and listing_entry.get("url"):
        # Events listing has canonical fight-night URLs when slug generation is unreliable.
        ufc_page_url = listing_entry["url"]
    source_url = raw_url or ufc_page_url or UFC_EVENTS_URL

    if listing_entry and listing_entry.get("venue"):
        # Prefer official UFC listing venue over API venue formatting.
        location = listing_entry["venue"]

    # Fill richer metadata from the official UFC event page when API fields are
    # missing, while preserving SportsDataIO precedence.
    if ufc_page_url and (
        not location
        or not main_event_division
        or not main_event_is_championship
        or not co_main_event
    ):
        page_meta = _fetch_ufc_event_page_metadata(ufc_page_url, main_event)
        if page_meta.get("location"):
            # Event page venue is the highest-confidence official formatting.
            location = page_meta.get("location")
        main_event_division = main_event_division or page_meta.get("main_event_division")
        main_event_is_championship = main_event_is_championship or bool(page_meta.get("main_event_is_championship"))
        main_event_championship_name = main_event_championship_name or page_meta.get("main_event_championship_name")
        co_main_event = co_main_event or page_meta.get("co_main_event")

    # ── Card-segment timing (priority order) ─────────────────────────────────
    early_prelims: Optional[datetime] = None
    prelims: Optional[datetime] = None

    # 1. UFC watch/schedule page — single HTTP request, covers near-term events.
    #    Always prefer this source; override the SportsDataIO main_card time with
    #    the more precise broadcast schedule when available.
    if watch_schedule:
        ws_key = _watch_schedule_key(event_name, eastern_date)
        if ws_key and ws_key in watch_schedule:
            ws = watch_schedule[ws_key]
            early_prelims = ws.get("early_prelims")
            prelims = ws.get("prelims")
            if ws.get("main_card"):
                main_card_dt = ws["main_card"]  # watch schedule is authoritative
            logger.debug(
                "Watch schedule match: %s → EP=%s, P=%s, MC=%s",
                event_name, early_prelims, prelims, main_card_dt,
            )

    # 2. Individual event page — reliable only for numbered PPV events; provides
    #    exact Unix timestamps with no timezone ambiguity.
    if early_prelims is None and prelims is None and ufc_page_url:
        page = _fetch_ufc_numbered_event_timestamps(ufc_page_url)
        if any(page.values()):
            early_prelims = page.get("early_prelims")
            prelims = page.get("prelims")
            if page.get("main_card"):
                main_card_dt = page["main_card"]  # timestamp is more precise

    # 3. Official UFC events listing (start times overlay) for remaining gaps.
    if listing_entry:
        lt = listing_entry.get("times") or {}
        if early_prelims is None and lt.get("early_prelims") is not None:
            early_prelims = lt.get("early_prelims")
        if prelims is None and lt.get("prelims") is not None:
            prelims = lt.get("prelims")
        if main_card_dt is None and lt.get("main_card") is not None:
            main_card_dt = lt.get("main_card")

    # Sanity: prelims must come before main card; discard if order is inverted.
    if prelims and main_card_dt and prelims > main_card_dt:
        logger.warning(
            "Prelims (%s) after main card (%s) for %s — discarding prelims",
            prelims, main_card_dt, event_name,
        )
        prelims = None
    if prelims and main_card_dt and prelims == main_card_dt:
        logger.warning(
            "Prelims equals main card (%s) for %s — discarding prelims",
            main_card_dt, event_name,
        )
        prelims = None

    if main_event_is_championship and not main_event_championship_name:
        main_event_championship_name = _championship_name_from_label("championship", main_event_division)

    return FightEvent(
        organization="UFC",
        event_name=event_name,
        slug=event_id,
        main_event=main_event,
        co_main_event=co_main_event,
        main_event_division=main_event_division,
        main_event_is_championship=main_event_is_championship,
        main_event_championship_name=main_event_championship_name,
        fight_list=fight_list,
        location=location,
        event_date=event_date,
        early_prelims=early_prelims,
        prelims=prelims,
        main_card=main_card_dt,
        source_url=source_url,
    )


def _is_future_event(event: FightEvent) -> bool:
    today = datetime.now(RIYADH).date()
    if event.main_card is not None:
        return to_riyadh(event.main_card).date() >= today
    if event.event_date is not None:
        return event.event_date >= today
    return False


def _build_public_ufc_events(
    listing_index: Dict[tuple[str, str], Dict[str, Any]],
    watch_schedule: Optional[Dict[Tuple[str, date], Dict[str, Optional[datetime]]]] = None,
) -> List[FightEvent]:
    """Build UFC events from the public UFC events listing when API access is unavailable."""
    events: List[FightEvent] = []
    today = datetime.now(RIYADH).date()

    for entry in listing_index.values():
        event_name = _normalize_text(entry.get("event_name")) or "UFC Event"
        main_event = _normalize_text(entry.get("main_event"))
        event_date = entry.get("event_date")
        if event_date is None:
            continue
        if event_date < today:
            continue

        times = entry.get("times") or {}
        early_prelims = times.get("early_prelims")
        prelims = times.get("prelims")
        main_card = times.get("main_card")

        if watch_schedule:
            ws_key = _watch_schedule_key(event_name, event_date)
            if ws_key and ws_key in watch_schedule:
                ws = watch_schedule[ws_key]
                if ws.get("early_prelims") is not None:
                    early_prelims = ws.get("early_prelims")
                if ws.get("prelims") is not None:
                    prelims = ws.get("prelims")
                if ws.get("main_card") is not None:
                    main_card = ws.get("main_card")

        if prelims and main_card and prelims > main_card:
            prelims = None
        if prelims and main_card and prelims == main_card:
            prelims = None

        events.append(
            FightEvent(
                organization="UFC",
                event_name=event_name,
                slug=event_name.lower().replace(" ", "-") if event_name else "ufc-event",
                main_event=main_event,
                fight_list=None,
                location=entry.get("venue"),
                event_date=event_date,
                early_prelims=early_prelims,
                prelims=prelims,
                main_card=main_card,
                source_url=entry.get("url"),
            )
        )

    return sorted(events, key=lambda ev: ev.main_card or datetime.combine(ev.event_date, datetime.min.time(), tzinfo=RIYADH))


def get_ufc_events() -> List[FightEvent]:
    api_key = _get_api_key()
    watch_schedule = _fetch_ufc_watch_schedule()
    listing_index = _fetch_ufc_events_listing_index()

    if not api_key:
        return _build_public_ufc_events(listing_index, watch_schedule)

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

            event = _build_event(schedule_event, api_key, watch_schedule, listing_index)
            if event is None:
                continue
            if not _is_future_event(event):
                continue

            event_ids.add(event_id)
            events.append(event)

    if not events:
        logger.info("SportsDataIO returned no UFC events; falling back to public UFC listing")
        return _build_public_ufc_events(listing_index, watch_schedule)

    return sorted(events, key=lambda ev: ev.main_card or datetime.combine(ev.event_date, datetime.min.time(), tzinfo=RIYADH))
