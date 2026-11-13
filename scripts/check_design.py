"""Design-system checks for the web UI.

1. Every text/background token pair used together meets WCAG 2.1 AA (4.5:1 for text,
   3:1 for large text / UI parts) in the dark theme and in the light theme.
2. The light theme redefines every colour token (a forgotten one would keep its dark value).
3. No raw colour values outside the token blocks (data: URIs excepted).

Run: uv run python scripts/check_design.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src/tunegrab/web/static"

# (foreground, background, minimum ratio, where it's used)
PAIRS = [
    ("--text-1", "--n-0", 4.5, "body text"),
    ("--text-1", "--n-1", 4.5, "cards, sidebar"),
    ("--text-1", "--n-2", 4.5, "match review panel, save bar"),
    ("--text-1", "--n-3", 4.5, "hovered buttons"),
    ("--text-1", "--raised", 4.5, "toasts, save bar"),
    ("--text-2", "--raised", 4.5, "save bar status"),
    ("--warn", "--raised", 4.5, "unsaved-changes note"),
    ("--bad", "--raised", 4.5, "save error"),
    ("--text-2", "--n-1", 4.5, "secondary text on cards"),
    ("--text-2", "--n-3", 4.5, "pills, hovered rows"),
    ("--text-3", "--n-0", 4.5, "faint text on page"),
    ("--text-3", "--n-1", 4.5, "faint text on cards"),
    ("--text-3", "--n-2", 4.5, "faint text on raised surfaces"),
    ("--accent-ink", "--accent-strong", 4.5, "primary button label"),
    ("--accent-ink", "--accent-hover", 4.5, "primary button hover"),
    ("--accent", "--n-1", 4.5, "current tab icon"),
    ("--ok", "--n-1", 4.5, "success text"),
    ("--ok", "--n-3", 4.5, "success pill"),
    ("--warn", "--n-0", 4.5, "warning text on page"),
    ("--warn", "--n-1", 4.5, "warning text"),
    ("--warn", "--n-2", 4.5, "match review note and button"),
    ("--warn", "--n-3", 4.5, "warning pill"),
    ("--warn-ink", "--warn", 3.0, "tick on a partly picked release"),
    ("--bad", "--n-1", 4.5, "error text"),
    ("--bad", "--n-2", 4.5, "delete button label"),
    ("--bad", "--n-3", 4.5, "error pill"),
    ("--info", "--n-0", 4.5, "links on page"),
    ("--info", "--n-1", 4.5, "links, info text"),
    ("--info", "--n-3", 4.5, "info pill"),
    ("--n-5", "--n-1", 1.0, "hover border (decorative)"),
    ("--accent-strong", "--n-1", 3.0, "selected / progress UI part"),
    ("--accent-strong", "--n-3", 3.0, "progress bar on its track"),
]
# drawn over cover art, so they don't change with the theme
THEME_FREE = {"--on-image", "--on-image-line"}

DARK = re.compile(r":root\s*\{(.*?)\n\}", re.S)
LIGHT = re.compile(r"@media \(prefers-color-scheme: light\)\s*\{\s*:root\s*\{(.*?)\n  \}\n\}", re.S)
COLOUR = r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)"


def declared(block: str) -> dict[str, str]:
    """Colour tokens of one token block: name -> value."""
    return dict(re.findall(rf"(--[\w-]+):\s*({COLOUR})", block))


def themes(css: str) -> dict[str, dict[str, str]]:
    dark, light = DARK.search(css), LIGHT.search(css)
    assert dark, "no :root block"
    assert light, "no light theme block"
    base = declared(dark.group(1))
    return {"dark": base, "light": {**base, **declared(light.group(1))}}


def missing_in_light(css: str) -> list[str]:
    dark, light = DARK.search(css), LIGHT.search(css)
    assert dark and light
    return sorted(set(declared(dark.group(1))) - set(declared(light.group(1))) - THEME_FREE)


def luminance(hex_colour: str) -> float:
    def channel(c: int) -> float:
        v = c / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def ratio(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def raw_colours(css: str, html: str, script: str) -> list[str]:
    """Colours written outside the token blocks (data: URIs can't use variables)."""
    body = DARK.sub("", LIGHT.sub("", css, count=1), count=1)
    body = re.sub(r'url\("data:[^"]*"\)', "", body)
    raw = re.findall(COLOUR, body)
    raw += re.findall(rf'style="[^"]*({COLOUR})', html)
    raw += re.findall(r"(?<![\w&])#[0-9a-fA-F]{6}\b", script)
    return sorted(set(raw))


def main() -> int:
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    failed = 0
    for theme, tok in themes(css).items():
        print(f"{theme} theme")
        for fg, bg, need, where in PAIRS:
            r = ratio(tok[fg], tok[bg])
            ok = r >= need
            failed += not ok
            print(f"  {'OK ' if ok else 'BAD'} {fg:16} on {bg:16} {r:5.2f}  {need:>4}  ({where})")
    problems = {
        "Colour tokens the light theme doesn't set": missing_in_light(css),
        "Raw colours outside tokens": raw_colours(css, html, script),
    }
    for label, found in problems.items():
        if found:
            failed += 1
            print(f"{label}: {found}")
    if not any(problems.values()):
        print("Light theme sets every colour token; no raw colours outside the token blocks.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
