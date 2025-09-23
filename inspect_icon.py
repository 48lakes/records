from pathlib import Path
path = Path('app/static/app.js')
text = path.read_text(encoding='utf-8')
start = text.find('function updatePlayPauseIcon')
print(start)
print(text[start:start+200])
