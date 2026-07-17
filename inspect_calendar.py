from pathlib import Path
text = Path('output/calendar.ics').read_text(encoding='utf-8')
for marker in ['UFC', 'PFL']:
    print(marker, 'present' if marker in text else 'missing')
print('event_count', text.count('BEGIN:VEVENT'))
print('sample_events')
for line in text.splitlines():
    if line.startswith('SUMMARY:'):
        print(line)
