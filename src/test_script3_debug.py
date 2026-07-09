import sys
sys.path.insert(0, '.')
from fetcher import fetch_html
import re

soup = fetch_html('https://www.ufc.com/event/ufc-fight-night-ankalaev-vs-rountree-jr')
if soup:
    text = soup.get_text(' ', strip=True)
    text_clean = re.sub(r'\s+', ' ', text)
    print("Page Title:", soup.title.string if soup.title else "No Title")
    print("Text length:", len(text_clean))
    print("Sample text:", repr(text_clean[:500]))
    
    # Try searching for Ankalaev or Rountree
    for kw in ['Ankalaev', 'Rountree', 'Prelim', 'Main']:
        idx = text_clean.lower().find(kw.lower())
        if idx >= 0:
            print(f"Keyword '{kw}' found at {idx}: {repr(text_clean[max(0,idx-50):idx+150])}")
