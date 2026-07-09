import sys
from pathlib import Path

from bs4 import BeautifulSoup

# Ensure src is on sys.path for imports when running tests from repository root
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import ufc


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
