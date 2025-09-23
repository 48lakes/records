from pathlib import Path
lines = Path('app/static/app.js').read_text(encoding='utf-8').splitlines()
for idx in range(240, 260):
    print(f"{idx+1:04d}: {lines[idx]}")
