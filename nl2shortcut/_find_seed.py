import re

with open(r'C:\Users\Deng2\Desktop\nl2shortcut\nl2shortcut\database.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Find SEED_SQL
for kw in ['SEED_SQL', 'SEED', '_SEED']:
    m = re.search(kw + r'\s*=\s*["\']+', content)
    if m:
        idx = m.start()
        # find matching closing triple quote
        quote = content[content.index('=', idx) + 1:].strip()[:3]
        if quote.startswith('"') or quote.startswith("'"):
            closer = quote
            closer_idx = content.find(closer, idx + len(kw) + len(closer) + 3)
            if closer_idx > 0:
                print(content[idx:closer_idx + len(closer)])
                break
else:
    print("SEED_SQL not found, searching for select/insert patterns...")
    # maybe it's a Python list, not SQL string
    for kw in ['SHORTCUT_DATA', '_data', 'KEY_MAP']:
        idx = content.find(kw)
        if idx > 0:
            print(f"Found {kw} at {idx}")
            print(content[max(0, idx-50):idx+500])
