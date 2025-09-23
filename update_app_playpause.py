from pathlib import Path
path = Path("app/static/app.js")
text = path.read_text(encoding="utf-8")
old_line = "    btn.textContent = audio.paused ? '?' : '?';"
new_line = "    btn.textContent = audio.paused ? '\\u25B6' : '\\u23F8';"
if old_line not in text:
    raise SystemExit('expected play/pause line not found')
text = text.replace(old_line, new_line)
path.write_text(text, encoding="utf-8")
