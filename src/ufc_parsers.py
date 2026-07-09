"""Parsing helpers for UFC event pages with unit-testable functions.

Helpers return only explicitly verified fields. They never invent
times and avoid using generic page titles as main event names.
"""
from __future__ import annotations

from typing import Any, Dict, Optional
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from datetime import datetime
import re

GENERIC_TITLE_KEYWORDS = ("mma schedule", "schedule", "events")


def is_generic_title(title: str) -> bool:
    if not title:
        return True
    t = title.strip().lower()
    return any(k in t for k in GENERIC_TITLE_KEYWORDS)


def parse_jsonld_schedule(obj: Dict[str, Any]) -> Dict[str, Optional[datetime]]:
    """Extract schedule and headliner from a JSON-LD-like dict.

    Returns dict with keys: main_event, early_prelims, prelims, main_card
    Values are datetimes or None. Only returns values when fields are
    explicitly present in the structure.
    """
    out = {"main_event": None, "early_prelims": None, "prelims": None, "main_card": None}

    # main event: prefer explicit mainEvent field
    main_event = obj.get("mainEvent") or obj.get("headline") or obj.get("main_event")
    if isinstance(main_event, str) and not is_generic_title(main_event):
        out["main_event"] = main_event.strip()

    # performers may compose a headliner
    performers = obj.get("performer") or obj.get("performers")
    if not out["main_event"] and isinstance(performers, list) and len(performers) >= 2:
        try:
            names = [p.get("name") for p in performers if isinstance(p, dict) and p.get("name")]
            if len(names) >= 2:
                candidate = f"{names[0]} vs {names[1]}"
                if not is_generic_title(candidate):
                    out["main_event"] = candidate
        except Exception:
            pass

    # Fallback: try to extract a matchup from description text
    if not out["main_event"]:
        desc = obj.get("description") or obj.get("summary") or ""
        if isinstance(desc, str):
            mm = _extract_matchup_from_text(desc)
            if mm:
                out["main_event"] = mm

    # parse subEvent list for explicit sections
    for sub in obj.get("subEvent") or []:
        if not isinstance(sub, dict):
            continue
        name = (sub.get("name") or "").lower()
        start = sub.get("startDate")
        dt = None
        if start:
            try:
                dt = date_parser.parse(start)
            except Exception:
                dt = None

        if "early" in name and dt:
            out["early_prelims"] = dt
        elif "prelim" in name and "main" not in name and dt:
            out["prelims"] = dt
        elif ("main" in name or "main card" in name) and dt:
            out["main_card"] = dt

    # top-level startDate may indicate main card only if explicit
    if out["main_card"] is None and obj.get("startDate"):
        try:
            out["main_card"] = date_parser.parse(obj.get("startDate"))
        except Exception:
            pass

    return out


def parse_embedded_json_schedule(obj: Dict[str, Any]) -> Dict[str, Optional[datetime]]:
    """Best-effort mapping from embedded JSON structures to schedule.

    This function is conservative: it only sets fields when keys appear
    in the object with obvious names (e.g., 'schedule', 'startDate', 'card').
    """
    out = {"main_event": None, "early_prelims": None, "prelims": None, "main_card": None}

    # main event
    if isinstance(obj.get("mainEvent"), str) and not is_generic_title(obj.get("mainEvent")):
        out["main_event"] = obj.get("mainEvent").strip()
    else:
        # try to find matchup in description
        desc = obj.get("description") or ""
        if isinstance(desc, str):
            mm = _extract_matchup_from_text(desc)
            if mm:
                out["main_event"] = mm

    # try card arrays
    card = obj.get("card") or obj.get("fightCard") or obj.get("schedule")
    if isinstance(card, list):
        # look for items with name and startDate
        for item in card:
            if not isinstance(item, dict):
                continue
            name = (item.get("name") or "").lower()
            start = item.get("startDate")
            dt = None
            if start:
                try:
                    dt = date_parser.parse(start)
                except Exception:
                    dt = None

            if "early" in name and dt:
                out["early_prelims"] = dt
            elif "prelim" in name and "main" not in name and dt:
                out["prelims"] = dt
            elif ("main" in name or "main card" in name) and dt:
                out["main_card"] = dt

        # derive main_event from first card item that looks like a matchup
        for item in card:
            if not isinstance(item, dict):
                continue
            title = (item.get("name") or item.get("title") or "").strip()
            if title and (" vs " in title.lower() or " v " in title.lower()):
                out["main_event"] = title
                break

    # top-level mapping
    if out["main_card"] is None and obj.get("startDate"):
        try:
            out["main_card"] = date_parser.parse(obj.get("startDate"))
        except Exception:
            pass

    return out


def _extract_matchup_from_text(text: str) -> Optional[str]:
    """Find a matchup like 'Fighter A vs Fighter B' in free-form text."""
    if not text:
        return None
    # Normalize whitespace
    t = re.sub(r"\s+", " ", text)
    # Look for patterns containing ' vs ' or ' v '
    # Match 'Fighter A vs Fighter B' but stop at common trailing words like ' at ' or ' in '
    m = re.search(r"([A-Za-z0-9\.'\- ]{2,60}?)\s+v(?:s)?\.?\s+([A-Za-z0-9\.'\- ]{2,60}?)(?=(?:\s+at\b|\s+in\b|,|$))", t, flags=re.IGNORECASE)
    if m:
        left = m.group(1).strip()
        right = m.group(2).strip()
        candidate = f"{left} vs {right}"
        if not is_generic_title(candidate):
            return candidate
    return None

    # top-level mapping
    if out["main_card"] is None and obj.get("startDate"):
        try:
            out["main_card"] = date_parser.parse(obj.get("startDate"))
        except Exception:
            pass

    return out


def parse_html_schedule(soup: BeautifulSoup) -> Dict[str, Optional[datetime]]:
    """Parse explicit schedule times from HTML elements.

    This function only accepts times that are explicitly marked with a
    <time datetime="..."> tag or text that clearly labels the section
    (e.g., 'Early Prelims', 'Prelims', 'Main Card'). It will not infer
    times from unlabeled text.
    """
    out = {"main_event": None, "early_prelims": None, "prelims": None, "main_card": None}

    # Main event extraction: look for sections labelled 'Main Event' or 'Headliner'
    me_node = None
    for tag in soup.find_all(lambda tag: tag.name in ("p", "div", "span", "h2", "h3") and tag.get_text(strip=True)):
        txt = tag.get_text(strip=True).lower()
        if txt == "main event" or txt == "headliner":
            me_node = tag
            break
    if me_node:
        # try to find a following sibling element that clearly contains 'vs'
        candidate = None
        from bs4 import Tag

        for s in me_node.next_siblings:
            if isinstance(s, Tag):
                txt = s.get_text(" ", strip=True)
                if txt and (" vs " in txt.lower() or " v " in txt.lower()):
                    candidate = txt
                    break

        # fallback to the label text if it itself contains the matchup
        if not candidate:
            txt = me_node.get_text(" ", strip=True)
            mm = _extract_matchup_from_text(txt)
            if mm:
                candidate = mm

        if candidate:
            # candidate is already a cleaned matchup when possible
            out["main_event"] = _extract_matchup_from_text(candidate) or candidate.strip()
    else:
        # Fallback: find any header or strong tag that contains a matchup
        for tag in soup.find_all(lambda tag: tag.name in ("h1", "h2", "h3", "h4", "strong", "b", "div") and tag.get_text(strip=True)):
            txt = tag.get_text(" ", strip=True)
            mm = _extract_matchup_from_text(txt)
            if mm:
                out["main_event"] = mm
                break

    # Helper to find explicit <time datetime> near a label
    def find_time_by_label(label: str):
        # Find elements that explicitly contain the label and a <time> child
        candidates = soup.find_all(lambda tag: tag.name in ("p", "div", "span", "li", "dt", "dd") and tag.get_text(strip=True) and label.lower() in tag.get_text(strip=True).lower())
        from bs4 import NavigableString
        for node in candidates:
            # look for the label in the node's immediate text children to avoid
            # matching a large container that contains multiple labeled sections
            found_label = False
            for c in node.contents:
                if isinstance(c, NavigableString) and c.strip():
                    if label.lower() in c.strip().lower():
                        found_label = True
                        break
            if not found_label:
                continue
            # require an explicit <time> element inside the labeled node
            t = node.find("time")
            if t and t.has_attr("datetime"):
                try:
                    return date_parser.parse(t["datetime"])
                except Exception:
                    continue
        # If none of the labeled nodes had an explicit <time>, do not attempt
        # to parse a shared container which may contain multiple labels.
        return None

    out["early_prelims"] = find_time_by_label("Early Prelims")
    out["prelims"] = find_time_by_label("Prelims")
    out["main_card"] = find_time_by_label("Main Card")

    # Ensure we do not copy a single time into multiple fields. If the same
    # datetime was found for multiple labels, keep them only if they came
    # from distinct nodes (the above ensures that). Otherwise prefer the
    # most specific (main_card) and leave others None.
    try:
        if out["main_card"] and out["prelims"] and out["main_card"] == out["prelims"] and out["early_prelims"] == out["main_card"]:
            # ambiguous: keep only main_card
            out["early_prelims"] = None
            out["prelims"] = None
    except Exception:
        pass

    return out
