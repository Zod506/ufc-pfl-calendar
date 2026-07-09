import sys
sys.path.insert(0, '.')
from fetcher import fetch_html
import re

soup = fetch_html('https://www.ufc.com/event/ufc-329')
if soup:
    # Look for the how-to-watch list
    div = soup.find('div', class_='c-how-to-watch--event-main-card-list')
    if div:
        print("=== c-how-to-watch--event-main-card-list ===")
        print(repr(div.get_text(' ', strip=True)))
        print()
        print("=== Raw HTML ===")
        print(str(div)[:2000])
    else:
        print("DIV NOT FOUND - looking for alternatives...")
        # Search broadly
        for tag in soup.find_all(class_=re.compile(r'how-to-watch', re.IGNORECASE)):
            print(f"Found: {tag.name} class={tag.get('class')}")
            print(f"Text: {repr(tag.get_text(' ', strip=True)[:200])}")
            print()
