from pathlib import Path
import sys
sys.path.insert(0,str((Path('.')/'src').resolve()))
from ufc_parsers import parse_html_schedule
from bs4 import BeautifulSoup
html='''
<div>
  <p>Main Event</p>
  <div>Fighter X vs Fighter Y</div>
  <p>Early Prelims <time datetime='2026-05-10T02:00:00Z'></time></p>
  <p>Prelims <time datetime='2026-05-10T04:00:00Z'></time></p>
  <p>Main Card <time datetime='2026-05-10T06:00:00Z'></time></p>
</div>
'''
soup=BeautifulSoup(html,'lxml')
print(parse_html_schedule(soup))
