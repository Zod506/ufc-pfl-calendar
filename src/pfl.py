"""PFL event provider.

Fetches upcoming events from the official PFL website and converts them
into ``FightEvent`` instances.

Strategy
--------
1. Fetch ``pflmma.com/events`` listing.
2. Find all ``event-card-info`` containers -- each one corresponds to a single
   event card in the listing grid.
3. For every card, extract the preliminary date and card-timing lines
   (e.g. "5pm ET Early Card | 8pm ET Main Card") directly from the listing.
4. Skip cards that show "VIEW RESULTS" (past events).
5. Visit only the individual event pages that are candidates for being future
   events, collect the clean official title and confirm the date via H2.
6. Skip past events, deduplicate, return sorted results.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import logging
import re

from dateutil import parser as date_parser

from models import FightEvent
from fetcher import fetch_html, extract_json_ld
from timezone import to_riyadh, RIYADH

logger = logging.getLogger(__name__)

BASE = "https://pflmma.com"

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

# Timezone abbreviation to IANA name mapping for card-time parsing.
_TZ_MAP: Dict[str, str] = {
    "ET": "America/New_York",
    "EDT": "America/New_York",
    "EST": "America/New_York",
    "CT": "America/Chicago",
    "CDT": "America/Chicago",
    "CST": "America/Chicago",
    "MT": "America/Denver",
    "MDT": "America/Denver",
    "MST": "America/Denver",
    "PT": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "PST": "America/Los_Angeles",
    "SAST": "Africa/Johannesburg",
    "GMT": "UTC",
    "UTC": "UTC",
}

# Tokens that indicate navigation / marketing -- never use as event titles.
_JUNK_TOKENS = frozenset(
    w.lower()
    for w in (
        "buy tickets", "buy now", "tickets", "matchups", "bout sheet",
        "event info", "view results", "register", "watch live", "watch now",
        "live", "previous", "next", "follow live", "vip experiences",
        "how to watch", "register your interest", "performance solutions",
    )
)


# ---- helpers ----------------------------------------------------------------

def _canonical_url(url: str) -> str:
    sp = urlsplit(url)
    return urlunsplit((sp.scheme, sp.netloc, sp.path.rstrip("/"), "", ""))


def _is_pflmma_event_link(href: str) -> bool:
    """Return True if href is a pflmma.com single-event page link."""
    if not href:
        return False
    if href.startswith("/event/"):
        return True
    return "pflmma.com/event/" in href


def _normalize(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    cleaned = re.sub(r"\s+", " ", text).strip()
    return cleaned or None


def _strip_org_suffix(title: str) -> str:
    """Remove trailing org name variants, e.g. '| Professional Fighters League'."""
    # Use explicit pipe or dash characters, not a character range
    title = re.sub(
        r"\s*(?:[|]|[-]|[--])\s*Professional\s+Fighters\s+League\s*$",
        "",
        title,
        flags=re.IGNORECASE,
    ).strip()
    # Also strip trailing "| PFL" or "- PFL"
    title = re.sub(
        r"\s*[|]\s*PFL\s*$",
        "",
        title,
        flags=re.IGNORECASE,
    ).strip()
    return title


def _is_junk_title(text: Optional[str]) -> bool:
    if not text:
        return True
    low = text.strip().lower()
    if not low:
        return True
    return low in _JUNK_TOKENS


def _clean_title(raw: Optional[str]) -> Optional[str]:
    """Strip org suffixes and junk tokens; return None if nothing usable remains."""
    if not raw:
        return None
    text = _strip_org_suffix(_normalize(raw) or "")
    if not text:
        return None
    # Remove junk tokens from the end / middle
    for token in sorted(_JUNK_TOKENS, key=len, reverse=True):
        text = re.sub(r"\b" + re.escape(token) + r"\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" |-")
    if not text or _is_junk_title(text):
        return None
    return text


# ---- date parsing -----------------------------------------------------------

def _parse_yearless_date(month_str: str, day: int) -> Optional[date]:
    """Infer the most-likely calendar year for a month+day combination.

    Uses the current Riyadh date as reference; picks the nearest upcoming
    occurrence (current year preferred; next year used only if the candidate
    falls more than 7 days in the past).
    """
    today = datetime.now(RIYADH).date()
    for year in (today.year, today.year + 1):
        try:
            d = date_parser.parse(f"{month_str} {day} {year}").date()
            if d >= today - timedelta(days=7):
                return d
        except Exception:
            continue
    return None


def _parse_listing_date(text: str) -> Optional[date]:
    """Parse a date in the form 'Fri, Jul 10' from listing card text."""
    m = re.search(
        r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),?\s+"
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})\b",
        text,
        re.IGNORECASE,
    )
    if not m:
        return None
    return _parse_yearless_date(m.group(1), int(m.group(2)))


def _parse_h2_date(h2_text: str) -> Optional[date]:
    """Parse a date from an H2 like 'PFL AUSTIN 2026 | SAT JUL 18'."""
    # Try pipe-separated format first
    m = re.search(
        r"[|]\s*(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+"
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})\b",
        h2_text,
        re.IGNORECASE,
    )
    if not m:
        # Standalone "FRI JUL 10" without pipe
        m = re.search(
            r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+"
            r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})\b",
            h2_text,
            re.IGNORECASE,
        )
        if not m:
            return None
    return _parse_yearless_date(m.group(1), int(m.group(2)))


def _parse_full_date(text: str) -> Optional[date]:
    """Parse any date with an explicit 4-digit year in text."""
    patterns = [
        r"\b\d{4}-\d{2}-\d{2}\b",
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},\s*\d{4}\b",
        r"\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}\b",
    ]
    for pattern in patterns:
        m_obj = re.search(pattern, text, flags=re.IGNORECASE)
        if m_obj:
            try:
                return date_parser.parse(m_obj.group(0)).date()
            except Exception:
                continue
    return None


# ---- card-time parsing ------------------------------------------------------

_CARD_TIME_RE = re.compile(
    r"(\d{1,2}(?::\d{2})?\s*(?:am|pm))\s+([A-Z]{2,4})\s+"
    r"(Early\s+Card|Early\s+Prelims?|Prelims?|Main\s+Card)",
    re.IGNORECASE,
)


def _parse_card_times(
    text: str, event_date: Optional[date]
) -> Dict[str, Optional[datetime]]:
    """Extract early-prelims / prelims / main-card datetimes from listing text.

    Parses patterns like "5pm ET Early Card | 8pm ET Main Card".
    All results are converted to Asia/Riyadh.
    """
    result: Dict[str, Optional[datetime]] = {
        "early_prelims": None,
        "prelims": None,
        "main_card": None,
    }
    if event_date is None:
        return result

    for m in _CARD_TIME_RE.finditer(text):
        time_str = m.group(1).strip()
        tz_abbrev = m.group(2).upper()
        label = m.group(3).lower().replace(" ", "_")

        tz_name = _TZ_MAP.get(tz_abbrev)
        if not tz_name:
            logger.debug("Unknown TZ abbreviation %r in PFL listing", tz_abbrev)
            continue

        tz = ZoneInfo(tz_name)
        try:
            t = date_parser.parse(time_str).time()
            local_dt = datetime.combine(event_date, t, tzinfo=tz)
            riyadh_dt = local_dt.astimezone(RIYADH)
        except Exception as exc:
            logger.debug("Could not parse card time %r: %s", time_str, exc)
            continue

        if "early" in label:
            result["early_prelims"] = riyadh_dt
        elif "prelim" in label and "early" not in label:
            result["prelims"] = riyadh_dt
        elif "main" in label:
            result["main_card"] = riyadh_dt

    return result


# ---- per-event page scraping ------------------------------------------------

# Acronyms / short identifiers that must stay fully uppercase.
_KEEP_UPPER = frozenset([
    "PFL", "MENA", "USA", "UK", "NYC", "DC", "UAE", "KSA",
    "UFC", "MMA", "NFL", "NBA", "II", "III", "IV",
])


def _smart_title_case(name: str) -> str:
    """Convert an ALL-CAPS event name to Title Case while keeping known acronyms.

    "PFL AUSTIN 2026"  -> "PFL Austin 2026"
    "PFL MENA 10"      -> "PFL MENA 10"  (MENA is an acronym)
    "PFL NEW YORK"     -> "PFL New York"
    "PFL Charlotte"    -> "PFL Charlotte" (already mixed case, unchanged)
    """
    if not name:
        return name
    # Only transform names that are predominantly uppercase.
    letters = re.sub(r"[^a-zA-Z]", "", name)
    if not letters:
        return name
    if sum(1 for c in letters if c.isupper()) / len(letters) < 0.7:
        return name  # already mixed-case — leave untouched
    words = name.split()
    result = []
    for word in words:
        alpha = re.sub(r"[^a-zA-Z]", "", word)
        if not alpha or alpha.upper() in _KEEP_UPPER or len(alpha) <= 2:
            result.append(word)
        else:
            result.append(word.capitalize())
    return " ".join(result)

def _extract_event_title(soup) -> Optional[str]:
    """Best-effort title extraction from an individual event page."""
    # 1. OG title (most reliable -- already includes event name)
    meta = soup.find("meta", attrs={"property": "og:title"})
    if meta and meta.get("content"):
        t = _clean_title(meta["content"])
        if t:
            return t

    # 2. JSON-LD name
    for obj in extract_json_ld(soup):
        name = obj.get("name")
        if isinstance(name, str):
            t = _clean_title(name)
            if t:
                return t

    # 3. First H2 that starts with "PFL" and has no pipe (not a date-annotated heading)
    for h in soup.find_all("h2"):
        text = h.get_text(" ", strip=True)
        if "|" in text:
            continue
        t = _clean_title(text)
        if t and re.match(r"^pfl\b", t, re.IGNORECASE):
            return t

    return None


def _extract_event_date_from_page(soup) -> Optional[date]:
    """Extract the event date from an individual event page."""
    # <time datetime="..."> tags
    for tag in soup.find_all("time"):
        raw = tag.get("datetime", "")
        if raw:
            try:
                return date_parser.parse(raw).date()
            except Exception:
                pass

    # JSON-LD startDate
    for obj in extract_json_ld(soup):
        sd = obj.get("startDate")
        if isinstance(sd, str):
            try:
                return date_parser.parse(sd).date()
            except Exception:
                pass

    # H2 with "| DAY MON DD" format
    for h in soup.find_all("h2"):
        text = h.get_text(" ", strip=True)
        d = _parse_h2_date(text)
        if d:
            return d

    # Full date with 4-digit year anywhere on page
    text = soup.get_text(" ", strip=True)
    return _parse_full_date(text)


def _extract_location_from_page(soup) -> Optional[str]:
    """Extract venue/location from an event page."""
    for obj in extract_json_ld(soup):
        loc = obj.get("location")
        if isinstance(loc, dict):
            name = loc.get("name")
            addr = loc.get("address") or {}
            locality = addr.get("addressLocality") if isinstance(addr, dict) else None
            parts = [p for p in (name, locality) if isinstance(p, str) and p.strip()]
            if parts:
                return ", ".join(dict.fromkeys(parts))
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


def _iter_jsonld_objects(soup) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for obj in extract_json_ld(soup):
        if not isinstance(obj, dict):
            continue
        graph = obj.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                if isinstance(item, dict):
                    out.append(item)
        else:
            out.append(obj)
    return out


def _format_location_from_place(place: Dict[str, Any]) -> Optional[str]:
    if not isinstance(place, dict):
        return None
    place_name = _normalize(place.get("name"))
    addr = place.get("address") if isinstance(place.get("address"), dict) else {}
    country_raw = _normalize(addr.get("addressCountry"))
    country = "USA" if country_raw and country_raw.lower() in ("united states", "us", "usa") else country_raw
    region_raw = _normalize(addr.get("addressRegion"))
    if country == "USA" and region_raw and region_raw.upper() in _US_STATE_ABBR:
        region = _US_STATE_ABBR[region_raw.upper()]
    else:
        region = region_raw
    locality_parts = [
        _normalize(addr.get("addressLocality")),
        region,
        country,
    ]
    locality = ", ".join([p for p in locality_parts if p])

    if place_name and locality and locality.lower() not in place_name.lower():
        return f"{place_name}\n{locality}"
    if place_name:
        if "," in place_name:
            left, right = place_name.split(",", 1)
            left = left.strip()
            right = right.strip()
            if left and right:
                return f"{left}\n{right}"
        return place_name
    if locality:
        return locality
    return None


def _extract_division_from_description(text: Optional[str]) -> Optional[str]:
    cleaned = _normalize(text)
    if not cleaned or " - " not in cleaned:
        return None
    return cleaned.rsplit(" - ", 1)[-1].strip() or None


def _is_explicit_championship(text: Optional[str]) -> bool:
    ll = (text or "").lower()
    return any(k in ll for k in ("title", "championship", "interim belt", "interim world champion"))


def _pfl_championship_name(text: Optional[str]) -> Optional[str]:
    ll = (text or "").lower()
    if "world" in ll and "title" in ll:
        if "interim" in ll:
            return "Interim PFL World Championship"
        return "PFL World Championship"
    if "championship" in ll or "title" in ll:
        if "interim" in ll:
            return "Interim PFL Championship"
        return "PFL Championship"
    return None


def _extract_official_metadata_from_page(soup, main_event: Optional[str]) -> Dict[str, Any]:
    """Extract official metadata from PFL JSON-LD only."""
    out: Dict[str, Any] = {
        "location": None,
        "co_main_event": None,
        "main_event_division": None,
        "main_event_is_championship": False,
        "main_event_championship_name": None,
        "main_event_official": None,
        "fight_list": None,
    }

    objs = _iter_jsonld_objects(soup)
    if not objs:
        return out

    event_obj = None
    for obj in objs:
        typ = obj.get("@type") or obj.get("type")
        if typ == "SportsEvent" and str(obj.get("@id", "")).endswith("#event"):
            event_obj = obj
            break
    if event_obj is None:
        for obj in objs:
            typ = obj.get("@type") or obj.get("type")
            if typ == "SportsEvent":
                event_obj = obj
                break
    if event_obj is None:
        return out

    out["location"] = _format_location_from_place(
        event_obj.get("location") if isinstance(event_obj.get("location"), dict) else {}
    )

    sub_events = [s for s in (event_obj.get("subEvent") or []) if isinstance(s, dict)]
    fight_names: List[str] = []
    target_key = _matchup_key(main_event)
    for sub in sub_events:
        name = _normalize(sub.get("name"))
        desc = _normalize(sub.get("description"))
        if name and " vs " in name.lower():
            fight_names.append(name)

        marker_blob = " ".join([p for p in (name, desc) if p]).lower()
        if name and "co-main" in marker_blob and not out["co_main_event"]:
            out["co_main_event"] = name

        if target_key and name and _matchup_key(name) == target_key:
            out["main_event_division"] = _extract_division_from_description(desc)
            out["main_event_is_championship"] = _is_explicit_championship(marker_blob)
            out["main_event_championship_name"] = _pfl_championship_name(marker_blob)

    if fight_names:
        out["fight_list"] = "\n".join(fight_names)
        out["main_event_official"] = fight_names[0]

    return out


_VS_RE = re.compile(
    r"([A-Z][A-Za-z'.\- ]{2,40}?)\s+vs\.?\s+([A-Z][A-Za-z'.\- ]{2,40})",
    re.IGNORECASE,
)
_JUNK_SUFFIX_RE = re.compile(
    r"\s*(?:official|promo|video|highlights|watch|tickets|buy|matchups)\b.*",
    re.IGNORECASE,
)


def _extract_main_event_from_page(soup) -> Optional[str]:
    """Find the headliner 'Fighter A vs Fighter B' matchup on an event page."""

    def _try_match(text: str) -> Optional[str]:
        text = _JUNK_SUFFIX_RE.sub("", text).strip()
        m = _VS_RE.search(text)
        if not m:
            return None
        left = m.group(1).strip()
        right = m.group(2).strip()
        if (
            left and right
            and not _is_junk_title(left)
            and not _is_junk_title(right)
            and len(left) > 2
            and len(right) > 2
        ):
            return f"{left} vs {right}"
        return None

    # Strategy 1: H2/H3/H4/strong/b tags without pipes (not event title headings)
    for tag in soup.find_all(["h2", "h3", "h4", "strong", "b"]):
        raw = tag.get_text(" ", strip=True)
        if "|" in raw:
            continue
        if " vs " not in raw.lower():
            continue
        result = _try_match(raw)
        if result:
            return result

    # Strategy 2: p/a tags -- PFL uses "EVENT NAME - Fighter A vs Fighter B | Event Name"
    # First strip " | Event Name" suffix, then strip "EVENT NAME - " prefix, then extract.
    for tag in soup.find_all(["p", "a"]):
        raw = tag.get_text(" ", strip=True)
        if " vs " not in raw.lower():
            continue
        # Take text before the first "|" (removes event name suffix)
        text = raw.split("|")[0].strip()
        # If the text contains " - ", take everything after the last " - " to remove
        # event-name prefixes like "PFL AUSTIN - "
        if " - " in text:
            text = text.rsplit(" - ", 1)[-1].strip()
        result = _try_match(text)
        if result:
            return result

    return None


# ---- listing-page parsing ---------------------------------------------------

def _parse_listing_cards(soup) -> List[Dict]:
    """Parse ``event-card-info`` containers from the listing page.

    Only returns UPCOMING events (cards with MATCHUPS / EVENT DETAILS buttons,
    NOT cards with VIEW RESULTS which indicate past events).

    Returns a list of dicts with keys: url, date, card_text.
    """
    cards: List[Dict] = []
    seen_urls: set = set()

    for card_div in soup.find_all("div", class_=lambda c: c and "event-card-info" in c):
        card_text = card_div.get_text(" ", strip=True)

        # Skip past events -- they show a "VIEW RESULTS" button.
        if re.search(r"\bview\s+results\b", card_text, re.IGNORECASE):
            continue

        # Find the primary /event/ link in this card
        event_url = None
        for a in card_div.find_all("a", href=True):
            href = a["href"]
            if _is_pflmma_event_link(href):
                full = urljoin(BASE, href) if href.startswith("/") else href
                event_url = _canonical_url(full)
                break

        if not event_url or event_url in seen_urls:
            continue
        seen_urls.add(event_url)

        event_date = _parse_listing_date(card_text)
        cards.append({"url": event_url, "date": event_date, "card_text": card_text})

    return cards


# ---- future-event filter ----------------------------------------------------

def _is_future_event(event: FightEvent) -> bool:
    today = datetime.now(RIYADH).date()
    if event.event_date is not None:
        return event.event_date >= today
    for dt in (event.main_card, event.prelims, event.early_prelims):
        if dt is not None and to_riyadh(dt).date() >= today:
            return True
    return False


# ---- main entry point -------------------------------------------------------

def get_pfl_events() -> List[FightEvent]:
    """Fetch upcoming PFL events and return a list of ``FightEvent`` objects."""
    listing_soup = fetch_html(f"{BASE}/events")
    if listing_soup is None:
        logger.warning("Could not fetch PFL events listing")
        return []

    cards = _parse_listing_cards(listing_soup)
    logger.info(
        "Found %d upcoming event-card entries on PFL listing page", len(cards)
    )

    today = datetime.now(RIYADH).date()
    events: List[FightEvent] = []
    seen_uids: set = set()

    for card in cards:
        url = card["url"]
        prelim_date: Optional[date] = card["date"]
        card_text: str = card["card_text"]

        # Skip clearly past events to avoid unnecessary HTTP requests.
        if prelim_date is not None and prelim_date < today - timedelta(days=1):
            logger.debug(
                "Skipping past PFL event (listing date %s): %s", prelim_date, url
            )
            continue

        # Fetch the individual event page.
        soup = fetch_html(url)
        if soup is None:
            logger.warning("Could not fetch PFL event page: %s", url)
            continue

        # Title
        title = _extract_event_title(soup)
        if not title:
            logger.debug("No usable title for PFL event %s; skipping", url)
            continue
        title = _smart_title_case(title)

        # Date -- prefer confirmed page date over listing date.
        event_date = _extract_event_date_from_page(soup) or prelim_date

        # Card times (extracted from listing card text).
        times = _parse_card_times(card_text, event_date or prelim_date)

        # Location
        location = _extract_location_from_page(soup)

        # Main event
        main_event = _extract_main_event_from_page(soup)

        # Additional official metadata from PFL JSON-LD.
        official_meta = _extract_official_metadata_from_page(soup, main_event)
        location = location or official_meta.get("location")
        co_main_event = official_meta.get("co_main_event")
        main_event_division = official_meta.get("main_event_division")
        main_event_is_championship = bool(official_meta.get("main_event_is_championship"))
        main_event_championship_name = official_meta.get("main_event_championship_name")
        fight_list = official_meta.get("fight_list")

        # Prefer official fight-card naming when available.
        official_main = official_meta.get("main_event_official")
        if official_main:
            known_keys = {_matchup_key(x) for x in (fight_list or "").splitlines() if _matchup_key(x)}
            if not main_event or _matchup_key(main_event) not in known_keys:
                main_event = official_main

        uid = _canonical_url(url)
        if uid in seen_uids:
            continue
        seen_uids.add(uid)

        fe = FightEvent(
            organization="PFL",
            event_name=title,
            slug=uid,
            main_event=main_event,
            co_main_event=co_main_event,
            main_event_division=main_event_division,
            main_event_is_championship=main_event_is_championship,
            main_event_championship_name=main_event_championship_name,
            fight_list=fight_list,
            location=location,
            event_date=event_date,
            early_prelims=times["early_prelims"],
            prelims=times["prelims"],
            main_card=times["main_card"],
            source_url=url,
        )

        if not _is_future_event(fe):
            logger.debug(
                "Skipping past PFL event (page date %s): %s", event_date, url
            )
            continue

        events.append(fe)
        logger.info(
            "PFL event: %s | date=%s | main_event=%s", title, event_date, main_event
        )

    return events
