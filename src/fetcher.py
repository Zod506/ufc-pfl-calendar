"""HTTP fetching and HTML helpers used by providers.

This module provides a thin wrapper around `requests` and `BeautifulSoup`
for retrieving pages and extracting JSON-LD where available. It is
designed for robustness and clear error messages when pages change.
"""
from __future__ import annotations

from typing import Optional, Any
import json
import logging

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


USER_AGENT = (
    "ufc-pfl-calendar/1.0 (+https://github.com/)" " python-requests"
)


def fetch_html(url: str, timeout: int = 15) -> Optional[BeautifulSoup]:
    """Fetch a URL and return a BeautifulSoup object or None on error.

    Args:
        url: The URL to fetch.
        timeout: Request timeout in seconds.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html"}
    try:
        with requests.Session() as s:
            resp = s.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return BeautifulSoup(resp.content, "lxml")
    except requests.RequestException as exc:
        logger.warning("Failed to fetch %s: %s", url, exc)
        return None


def extract_json_ld(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Extract JSON-LD scripts from a BeautifulSoup tree.

    Returns a list of parsed JSON objects (may be empty).
    """
    out: list[dict[str, Any]] = []
    if soup is None:
        return out

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            text = tag.string
            if not text:
                continue
            parsed = json.loads(text)
            # JSON-LD can be a list or an object
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict):
                        out.append(item)
            elif isinstance(parsed, dict):
                out.append(parsed)
        except Exception:
            # Don't fail the whole process for one bad JSON-LD block
            logger.debug("Invalid JSON-LD skipped")
            continue

    return out


def extract_embedded_json(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Attempt to extract JSON objects embedded inside <script> tags.

    This searches for JavaScript assignments containing large JSON blobs,
    such as `window.__INITIAL_STATE__ = { ... };` or `var data = {...};`.
    It returns all parsed JSON objects found.
    """
    results: list[dict[str, Any]] = []
    if soup is None:
        return results

    import re

    # Look for script tags without a type or with type text/javascript
    candidates = []
    for tag in soup.find_all("script"):
        if tag.string:
            candidates.append(tag.string)

    # Simple heuristic: find top-level JSON objects in the script text
    # Find large JSON-like blocks (avoid very small objects)
    json_obj_re = re.compile(r"\{[\s\S]{200,}\}", re.DOTALL)

    for script_text in candidates:
        # Some pages pack JSON after an equals sign
        try:
            # Find fragments that look like JSON objects
            for m in json_obj_re.finditer(script_text):
                txt = m.group(0)
                # Quick filter: must contain common event keys
                if any(k in txt for k in ('event', 'card', 'mainEvent', 'startDate', 'venue')):
                    try:
                        obj = json.loads(txt)
                        if isinstance(obj, dict):
                            results.append(obj)
                    except Exception:
                        # Not strict JSON — skip
                        continue
        except re.error:
            continue

    return results
