"""Check data-timestamp on UFC events listing page + event card structure."""
import sys
sys.path.insert(0, ".")
from fetcher import fetch_html
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import re

RIYADH = ZoneInfo("Asia/Riyadh")
UTC = timezone.utc

soup = fetch_html("https://www.ufc.com/events")
if soup:
    # data-timestamp elements
    ts_divs = soup.find_all(attrs={"data-timestamp": True})
    print(f"data-timestamp elements on /events: {len(ts_divs)}")
    for div in ts_divs[:20]:
        ts = div.get("data-timestamp")
        text = div.get_text(" ", strip=True)[:80]
        # Find the nearby event title
        nearby = ""
        for parent in div.parents:
            headings = parent.find_all(["h1","h2","h3","h4"])
            if headings:
                nearby = headings[0].get_text(" ", strip=True)[:60]
                break
        try:
            dt = datetime.fromtimestamp(int(ts), tz=UTC).astimezone(RIYADH)
            print(f"  ts={ts} -> {dt.strftime('%Y-%m-%d %I:%M %p Riyadh')} text={text!r}")
        except Exception as e:
            print(f"  ts={ts} ERROR: {e} text={text!r}")
    
    # Also look for how-to-watch sections for multiple events
    print()
    how_to_divs = soup.find_all("div", class_="c-how-to-watch--event-main-card-list")
    print(f"c-how-to-watch divs on /events: {len(how_to_divs)}")
    for div in how_to_divs[:5]:
        print(f"  text: {div.get_text(' ', strip=True)[:200]}")
        for li in div.find_all("li"):
            label = li.find(class_="c-listing-viewing-option__fight-card")
            time_div = li.find(attrs={"data-timestamp": True})
            if label and time_div:
                ts = time_div.get("data-timestamp")
                lab_text = label.get_text(strip=True)
                try:
                    dt = datetime.fromtimestamp(int(ts), tz=UTC).astimezone(RIYADH)
                    print(f"    {lab_text}: ts={ts} -> {dt.strftime('%Y-%m-%d %I:%M %p Riyadh')}")
                except Exception:
                    pass
else:
    print("FAILED")
