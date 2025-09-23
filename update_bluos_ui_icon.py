from pathlib import Path
path = Path("app/static/bluos-ui.js")
text = path.read_text()
text = text.replace("    const btn = $('#btnPlayPause');\n    const meta = $('#playerMeta');\n    const vol = $('#bluosVolume');\n    const state = String(d?.state || '').toLowerCase();\n    if (btn) btn.textContent = (state === 'play' || state === 'stream') ? '' : '';\n", "    const btn = $('#btnPlayPause');\n    const meta = $('#playerMeta');\n    const vol = $('#bluosVolume');\n    const state = String(d?.state || '').toLowerCase();\n    if (btn) btn.textContent = (state === 'play' || state === 'stream') ? '?' : '?';\n")
path.write_text(text)
