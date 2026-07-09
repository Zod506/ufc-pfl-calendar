"""Entry point for the calendar generator.

Fetches UFC and PFL events, aggregates them and writes an ICS file to
`output/calendar.ics` on every run.
"""
from __future__ import annotations

import logging
from typing import List

from ufc import get_ufc_events
from pfl import get_pfl_events
from calendar_builder import build_calendar

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> int:
    events: List = []
    logger.info("Fetching UFC events...")
    events.extend(get_ufc_events())

    logger.info("Fetching PFL events...")
    events.extend(get_pfl_events())

    logger.info("Building calendar with %d events", len(events))
    out = build_calendar(events)
    logger.info("Calendar written to %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())