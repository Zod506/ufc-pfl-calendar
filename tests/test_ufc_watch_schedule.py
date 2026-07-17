import sys
from pathlib import Path
from datetime import date, datetime, timezone

from bs4 import BeautifulSoup

# Ensure src is on sys.path for imports when running tests from repository root
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import ufc
from models import FightEvent


def test_normalize_watch_event_short():
    assert ufc._normalize_watch_event_short("UFC 331: Some Headliner") == "UFC 331"
    assert ufc._normalize_watch_event_short("UFC Fight Night: Emmett vs Murphy") == "UFC FIGHT NIGHT"


def test_fetch_watch_schedule_parses_eest_and_fight_night(monkeypatch):
    html = """
    <div>
      Jul 12 Sun UFC 329 Early Prelims Sun Jul 12 00 EEST
      Jul 12 Sun UFC 329 Prelims Sun Jul 12 02 EEST
      Jul 12 Sun UFC 329 Main Card Sun Jul 12 05 EEST
      Jul 19 Sat UFC Fight Night: Emmett vs Murphy Main Card Sat Jul 19 19 EEST
    </div>
    """
    soup = BeautifulSoup(html, "lxml")

    monkeypatch.setattr(ufc, "fetch_html", lambda *args, **kwargs: soup)

    out = ufc._fetch_ufc_watch_schedule()

    numbered_key = next(k for k in out.keys() if k[0] == "UFC 329")
    assert out[numbered_key]["early_prelims"] is not None
    assert out[numbered_key]["prelims"] is not None
    assert out[numbered_key]["main_card"] is not None
    # EEST (UTC+3) to Riyadh (UTC+3) should preserve local hour.
    assert out[numbered_key]["main_card"].hour == 5

    fn_key = next(k for k in out.keys() if k[0] == "UFC FIGHT NIGHT")
    assert out[fn_key]["main_card"] is not None


def test_get_ufc_events_falls_back_to_public_listing(monkeypatch):
    monkeypatch.setattr(ufc, "_get_api_key", lambda: None)
    monkeypatch.setattr(ufc, "_fetch_ufc_watch_schedule", lambda: {})

    def fake_listing():
        return {
            (("sean", "omalley"), ("merab", "dvalishvili")): {
                "event_name": "UFC 329",
                "main_event": "Sean O'Malley vs Merab Dvalishvili",
                "url": "https://www.ufc.com/event/ufc-329",
                "event_date": date(2026, 7, 20),
                "venue": "The Sphere\nLas Vegas, NV, USA",
                "times": {
                    "early_prelims": datetime(2026, 6, 6, 13, 0, tzinfo=timezone.utc),
                    "prelims": datetime(2026, 6, 6, 15, 0, tzinfo=timezone.utc),
                    "main_card": datetime(2026, 6, 6, 18, 0, tzinfo=timezone.utc),
                },
            }
        }

    monkeypatch.setattr(ufc, "_fetch_ufc_events_listing_index", fake_listing)

    events = ufc.get_ufc_events()

    assert len(events) == 1
    assert events[0].organization == "UFC"
    assert events[0].event_name == "UFC 329"
    assert events[0].main_event == "Sean O'Malley vs Merab Dvalishvili"
