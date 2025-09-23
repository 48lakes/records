from pathlib import Path
lines = Path('app/static/app.js').read_text(encoding='utf-8').splitlines()
for idx,line in enumerate(lines, start=1):
    if idx in range(1700, 1760):
        print(f"{idx:04d}: {line}")
