import sys
sys.path.insert(0, '.')
from fetcher import fetch_html
import re

soup = fetch_html('https://www.ufc.com/event/ufc-fight-night-ankalaev-vs-rountree-jr')
if soup:
    text = soup.get_text(' ', strip=True)
    text_clean = re.sub(r'\s+', ' ', text)
    print("Text around prelim/card mentions:")
    for kw in ['Prelims', 'Early', 'Main Card', 'Start Times']:
        idx = text_clean.find(kw)
        if idx >= 0:
            print(f"  '{kw}' at pos {idx}: {repr(text_clean[max(0,idx-30):idx+150])}")
    
    div = soup.find('div', class_='c-how-to-watch--event-main-card-list')
    if div:
        print("\nc-how-to-watch div:", repr(div.get_text(' ', strip=True)))
    else:
        print("\nNo c-how-to-watch div found")
