import sys
from pathlib import Path

def main():
    if len(sys.argv) < 3:
        print("Usage: extract_pdf_text.py <input.pdf> <output.txt>")
        sys.exit(2)
    inp = Path(sys.argv[1])
    outp = Path(sys.argv[2])
    try:
        from pdfminer.high_level import extract_text
    except Exception as e:
        print(f"Failed to import pdfminer: {e}")
        sys.exit(1)
    if not inp.exists():
        print(f"Input does not exist: {inp}")
        sys.exit(1)
    text = extract_text(str(inp))
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(text, encoding="utf-8")
    print(f"Wrote text: {outp}")

if __name__ == "__main__":
    main()

