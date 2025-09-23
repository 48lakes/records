from pathlib import Path
path = Path("app/static/app.js")
text = path.read_text(encoding="utf-8")
text = text.replace("    btn.textContent = audio.paused ? '' : '';", "    btn.textContent = audio.paused ? '?' : '?';")
if "updatePlayPauseIcon();\n" not in text:
    marker = "    setupEventListeners();\n\n    // Load initial data (paged)\n"
    if marker in text:
        text = text.replace(marker, "    setupEventListeners();\n    updatePlayPauseIcon();\n\n    // Load initial data (paged)\n")
path.write_text(text, encoding="utf-8")
