"""Design-system checks for the web UI.

1. Every text/background token pair used together meets WCAG 2.1 AA (4.5:1 for text,
   3:1 for large text / UI parts).
2. No raw colour values outside the ``:root`` token block (data: URIs excepted).

Run: uv run python scripts/check_design.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HTML = Path(__file__).resolve().parents[1] / "src/tunegrab/web/static/index.html"

# (foreground, background, minimum ratio, where it's used)
PAIRS = [
    ("--text-1", "--n-0", 4.5, "body text"),
    ("--text-1", "--n-1", 4.5, "cards, sidebar"),
    ("--text-2", "--n-1", 4.5, "secondary text on cards"),
    ("--text-2", "--n-3", 4.5, "pills, hovered rows"),
    ("--text-3", "--n-0", 4.5, "faint text on page"),
    ("--text-3", "--n-1", 4.5, "faint text on cards"),
    ("--text-3", "--n-2", 4.5, "faint text on raised surfaces"),
    ("--accent-ink", "--accent-strong", 4.5, "primary button label"),
    ("--accent-ink", "--accent", 4.5, "primary button hover"),
    ("--accent", "--n-1", 4.5, "success text"),
    ("--warn", "--n-1", 4.5, "warning text"),
    ("--bad", "--n-1", 4.5, "error text"),
    ("--bad", "--n-3", 4.5, "error pill"),
    ("--info", "--n-1", 4.5, "links, info text"),
    ("--info", "--n-3", 4.5, "info pill"),
    ("--n-5", "--n-1", 1.0, "hover border (decorative)"),
    ("--accent-strong", "--n-1", 3.0, "selected / progress UI part"),
]


def tokens(css: str) -> dict[str, str]:
    root = re.search(r":root\s*\{(.*?)\n\}", css, re.S)
    assert root, "no :root block"
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})", root.group(1)))


def luminance(hex_colour: str) -> float:
    def channel(c: int) -> float:
        v = c / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def ratio(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def main() -> int:
    html = HTML.read_text(encoding="utf-8")
    css = re.search(r"<style>(.*?)</style>", html, re.S)
    assert css
    tok = tokens(css.group(1))
    failed = 0
    print(f"{'pair':44} ratio  need")
    for fg, bg, need, where in PAIRS:
        r = ratio(tok[fg], tok[bg])
        ok = r >= need
        failed += not ok
        print(f"{'OK ' if ok else 'BAD'} {fg} on {bg:16} {r:5.2f}  {need:>4}  ({where})")
    # raw colours outside the token block (ignore data: URIs, which can't use variables)
    body = re.sub(r":root\s*\{.*?\n\}", "", css.group(1), count=1, flags=re.S)
    body = re.sub(r'url\("data:[^"]*"\)', "", body)
    raw = re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)", body)
    script = "".join(re.findall(r"<script>(.*?)</script>", html, re.S))
    raw += re.findall(r"(?<![\w&])#[0-9a-fA-F]{6}\b", script)
    if raw:
        failed += 1
        print("Raw colours outside tokens:", sorted(set(raw)))
    else:
        print("No raw colours outside the token block.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
