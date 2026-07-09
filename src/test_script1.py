import sys
sys.path.insert(0, '.')
from fetcher import fetch_html
import re

soup = fetch_html('https://www.ufc.com/watch/schedule')
if soup:
    text = soup.get_text(' ', strip=True)
    # Collapse whitespace
    text_clean = re.sub(r'\s+', ' ', text)
    # Find the TV SCHEDULE section
    idx = text_clean.find('TV SCHEDULE')
    if idx >= 0:
        print("=== TV SCHEDULE section ===")
        print(repr(text_clean[idx:idx+1500]))
    # Also look for UPCOMING section
    idx2 = text_clean.find('Upcoming')
    if idx2 >= 0:
        print("\n=== Upcoming section ===")
        print(repr(text_clean[idx2:idx2+500]))
