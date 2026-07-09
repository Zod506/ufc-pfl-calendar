import sys
from pathlib import Path
import pytest
from bs4 import BeautifulSoup
from datetime import datetime, timezone

# Ensure src is on sys.path for imports when running tests from repository root
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ufc_parsers import parse_jsonld_schedule, parse_embedded_json_schedule, parse_html_schedule, is_generic_title


def test_is_generic_title():
    assert is_generic_title("MMA Schedule - 2026")
    assert is_generic_title("Upcoming Events")
    assert not is_generic_title("Poirier vs McGregor")


def test_parse_jsonld_schedule_basic():
    obj = {
        "name": "UFC Test",
        "startDate": "2026-05-10T00:00:00Z",
        "subEvent": [
            {"name": "Early Prelims", "startDate": "2026-05-10T02:00:00Z"},
            {"name": "Prelims", "startDate": "2026-05-10T04:00:00Z"},
            {"name": "Main Card", "startDate": "2026-05-10T06:00:00Z"},
        ],
        "performer": [{"name": "Fighter A"}, {"name": "Fighter B"}],
    }

    out = parse_jsonld_schedule(obj)
    assert out["main_event"] == "Fighter A vs Fighter B"
    assert out["early_prelims"].tzinfo is not None
    assert out["prelims"].tzinfo is not None
    assert out["main_card"].tzinfo is not None


def test_parse_embedded_json_schedule():
    obj = {"card": [{"name": "Early Prelims", "startDate": "2026-05-10T02:00:00Z"}]}
    out = parse_embedded_json_schedule(obj)
    assert out["early_prelims"] is not None


def test_parse_html_schedule():
    html = """
    <div>
      <p>Main Event</p>
      <div>Fighter X vs Fighter Y</div>
      <p>Early Prelims <time datetime='2026-05-10T02:00:00Z'></time></p>
      <p>Prelims <time datetime='2026-05-10T04:00:00Z'></time></p>
      <p>Main Card <time datetime='2026-05-10T06:00:00Z'></time></p>
    </div>
    """
    soup = BeautifulSoup(html, "lxml")
    out = parse_html_schedule(soup)
    assert out["main_event"] == "Fighter X vs Fighter Y"
    assert out["early_prelims"].tzinfo is not None
    assert out["prelims"].tzinfo is not None
    assert out["main_card"].tzinfo is not None


def test_only_main_card_present():
        html = """
        <div>
            <p>Main Card <time datetime='2026-05-10T06:00:00Z'></time></p>
            <!-- no explicit prelims/early times -->
        </div>
        """
        soup = BeautifulSoup(html, "lxml")
        out = parse_html_schedule(soup)
        assert out["main_card"] is not None
        assert out["prelims"] is None
        assert out["early_prelims"] is None


def test_extract_matchup_from_description():
        obj = {"description": "Tonight main event: Islam Makhachev vs Ilia Topuria at the arena"}
        out = parse_jsonld_schedule(obj)
        assert out["main_event"] == "Islam Makhachev vs Ilia Topuria"


def test_html_scan_for_matchup():
        html = """
        <div>
            <h2>Islam Makhachev vs Ilia Topuria</h2>
        </div>
        """
        soup = BeautifulSoup(html, "lxml")
        out = parse_html_schedule(soup)
        assert out["main_event"] == "Islam Makhachev vs Ilia Topuria"
