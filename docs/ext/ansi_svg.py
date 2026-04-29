"""Translate ANSI-colored terminal text into SVG terminal window."""

from __future__ import annotations

import os
import re
from pathlib import Path

os.environ["FORCE_COLOR"] = "1"
from xml.sax.saxutils import escape

FONT_SIZE = 12
CHAR_WIDTH = 7.4
LINE_HEIGHT = 15
PADDING_X = 10
PADDING_Y = 8
HEADER_HEIGHT = 24

# ANSI SGR color codes → SVG fill colors (terminal palette)
COLORS = {
    30: "#1e1e1e",
    31: "#e06c75",
    32: "#98c379",
    33: "#e5c07b",
    34: "#61afef",
    35: "#c678dd",
    36: "#56b6c2",
    37: "#abb2bf",
    90: "#5c6370",
    91: "#e06c75",
    92: "#98c379",
    93: "#e5c07b",
    94: "#61afef",
    95: "#c678dd",
    96: "#56b6c2",
    97: "#ffffff",
}

DEFAULT_FG = "#c5c8c6"
BG_COLOR = "#1e1e1e"
TITLE_BAR_COLOR = "#292929"

ANSI_RE = re.compile(r"\x1b\[([0-9;]*)m")

TEMPLATE = (Path(__file__).parent / "terminal-template.svg").read_text(encoding="utf-8")


ANSI256_BASIC = [
    "#000000",
    "#aa0000",
    "#00aa00",
    "#aa5500",
    "#0000aa",
    "#aa00aa",
    "#00aaaa",
    "#aaaaaa",
    "#555555",
    "#ff5555",
    "#55ff55",
    "#ffff55",
    "#5555ff",
    "#ff55ff",
    "#55ffff",
    "#ffffff",
]


def ansi256_to_hex(n: int) -> str:
    if n < 16:
        return ANSI256_BASIC[n]
    if n < 232:
        n -= 16
        r = (n // 36) * 51
        g = ((n % 36) // 6) * 51
        b = (n % 6) * 51
        return f"#{r:02x}{g:02x}{b:02x}"
    gray = 8 + (n - 232) * 10
    return f"#{gray:02x}{gray:02x}{gray:02x}"


def parse_ansi_spans(text: str) -> list[tuple[str, str, bool]]:
    """Parse ANSI text into (text, color, bold) spans."""
    spans: list[tuple[str, str, bool]] = []
    color = DEFAULT_FG
    bold = False
    pos = 0

    for m in ANSI_RE.finditer(text):
        chunk = text[pos : m.start()]
        if chunk:
            spans.append((chunk, color, bold))
        pos = m.end()

        codes = [int(c) for c in m.group(1).split(";") if c] if m.group(1) else [0]
        i = 0
        while i < len(codes):
            code = codes[i]
            if code == 0:
                color, bold = DEFAULT_FG, False
            elif code == 1:
                bold = True
            elif code == 38 and i + 2 < len(codes) and codes[i + 1] == 5:
                color = ansi256_to_hex(codes[i + 2])
                i += 2
            elif code == 39:
                color = DEFAULT_FG
            elif code in COLORS:
                color = COLORS[code]
            i += 1

    tail = text[pos:]
    if tail:
        spans.append((tail, color, bold))
    return spans


def render_line_svg(spans: list[tuple[str, str, bool]], y: float) -> str:
    """Render one line of spans as SVG <text> with <tspan>s."""
    if not spans:
        return ""

    parts: list[str] = []
    for text, color, bold in spans:
        escaped = escape(text).replace(" ", "&#160;")
        attrs = f'fill="{color}"'
        if bold:
            attrs += ' font-weight="bold"'
        parts.append(f"<tspan {attrs}>{escaped}</tspan>")

    x = PADDING_X
    return f'<text x="{x}" y="{y:.1f}">{"".join(parts)}</text>'


def ansi_to_svg_lines(text: str) -> list[str]:
    """Convert ANSI text to list of SVG <text> elements."""
    lines = text.rstrip("\n").split("\n")
    svg_lines: list[str] = []
    for i, line in enumerate(lines):
        y = HEADER_HEIGHT + PADDING_Y + (i + 1) * LINE_HEIGHT
        spans = parse_ansi_spans(line)
        svg_line = render_line_svg(spans, y)
        if svg_line:
            svg_lines.append(svg_line)
    return svg_lines


def render_terminal(title: str, ansi_text: str) -> str:
    """Render complete SVG terminal window with ANSI-colored content."""
    content_lines = ansi_to_svg_lines(ansi_text)
    num_lines = ansi_text.rstrip("\n").count("\n") + 1
    max_visible = max(
        (len(ANSI_RE.sub("", line)) for line in ansi_text.split("\n")),
        default=80,
    )
    cols = max(max_visible, 80) + 2
    width = cols * CHAR_WIDTH + PADDING_X * 2
    height = HEADER_HEIGHT + PADDING_Y * 2 + num_lines * LINE_HEIGHT

    content = "\n    ".join(content_lines)

    return TEMPLATE.format(
        title=escape(title),
        content=content,
        width=width,
        height=height,
        bg_color=BG_COLOR,
        title_bar_color=TITLE_BAR_COLOR,
        font_size=FONT_SIZE,
        line_height=LINE_HEIGHT,
    )
