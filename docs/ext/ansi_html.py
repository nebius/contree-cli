"""Translate ANSI-colored terminal text into an HTML terminal window."""

from __future__ import annotations

import html
import json
import re

PADDING_X = 10
PADDING_Y = 8
HEADER_HEIGHT = 24

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

SYSTEM_FONT = (
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
)
MONO_FONT = (
    "JetBrains Mono, SF Mono, SFMono-Regular, Menlo, Monaco, Cascadia Mono, "
    "Segoe UI Mono, Roboto Mono, Oxygen Mono, Ubuntu Monospace, Source Code Pro, "
    "Fira Mono, Droid Sans Mono, Consolas, Courier New, monospace"
)

# Characters that confuse MDX/JSX parsers inside text nodes.
_MDX_TEXT_ENTITIES = str.maketrans(
    {
        "{": "&#123;",
        "}": "&#125;",
        "`": "&#96;",
        "$": "&#36;",
        "_": "&#95;",
        "*": "&#42;",
    }
)


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


def escape_text(text: str, *, mdx: bool) -> str:
    """Escape text for HTML; MDX builds also neutralize markdown/JSX syntax."""
    escaped = html.escape(text)
    if mdx:
        escaped = escaped.translate(_MDX_TEXT_ENTITIES)
    return escaped


def _html_style(props: dict[str, str | int]) -> str:
    """Render a CSS declaration string for plain HTML."""
    parts: list[str] = []
    for key, value in props.items():
        css_key = re.sub(r"[A-Z]", lambda m: "-" + m.group(0).lower(), key)
        if isinstance(value, int):
            parts.append(f"{css_key}: {value}px")
        else:
            parts.append(f"{css_key}: {value}")
    return "; ".join(parts)


def _style_attr(props: dict[str, str | int], *, mdx: bool) -> str:
    if mdx:
        return f"style={{{json.dumps(props, separators=(',', ':'))}}}"
    return f'style="{_html_style(props)}"'


def _class_attr(*, mdx: bool) -> str:
    return 'className="contree-terminal"' if mdx else 'class="contree-terminal"'


def render_line_html(spans: list[tuple[str, str, bool]], *, mdx: bool) -> str:
    """Render one line of spans as HTML with inline styles."""
    if not spans:
        return ""

    parts: list[str] = []
    for text, color, bold in spans:
        style_props: dict[str, str | int] = {"color": color}
        if bold:
            style_props["fontWeight"] = "bold"
        parts.append(
            f"<span {_style_attr(style_props, mdx=mdx)}>"
            f"{escape_text(text, mdx=mdx)}</span>"
        )

    return "".join(parts)


def ansi_to_html_lines(text: str, *, mdx: bool) -> str:
    """Convert ANSI text to HTML line spans joined for MDX (no raw newlines)."""
    lines = text.rstrip("\n").split("\n")
    br = "<br />" if mdx else "<br>"
    return br.join(render_line_html(parse_ansi_spans(line), mdx=mdx) for line in lines)


def render_terminal(title: str, ansi_text: str, *, mdx: bool = False) -> str:
    """Render complete HTML terminal window with ANSI-colored content."""
    content = ansi_to_html_lines(ansi_text, mdx=mdx)
    escaped_title = escape_text(title, mdx=mdx)

    dot_style = {
        "display": "inline-block",
        "width": 10,
        "height": 10,
        "borderRadius": "50%",
    }
    red_dot_style = {**dot_style, "background": "#ff5f57", "marginRight": 6}
    yellow_dot_style = {**dot_style, "background": "#febc2e", "marginRight": 6}
    green_dot_style = {**dot_style, "background": "#28c840"}

    outer_style = {
        "border": "1px solid rgba(255,255,255,0.15)",
        "borderRadius": 8,
        "overflow": "hidden",
        "margin": "1rem 0",
    }

    header_style = {
        "background": TITLE_BAR_COLOR,
        "height": HEADER_HEIGHT,
        "display": "flex",
        "alignItems": "center",
        "padding": "0 12px",
        "position": "relative",
    }

    dots_row_style = {
        "display": "flex",
        "alignItems": "center",
        "flexShrink": 0,
    }

    title_style = {
        "position": "absolute",
        "left": 0,
        "right": 0,
        "textAlign": "center",
        "fontFamily": SYSTEM_FONT,
        "fontWeight": "bold",
        "color": "#999",
    }

    pre_style = {
        "margin": 0,
        "borderRadius": 0,
        "padding": f"{PADDING_Y}px {PADDING_X}px",
        "background": BG_COLOR,
        "overflowX": "auto",
    }

    code_style = {
        "fontFamily": MONO_FONT,
        "color": DEFAULT_FG,
        "whiteSpace": "pre-wrap",
    }

    # Single-line output: MDX treats newlines inside HTML as paragraph breaks.
    return (
        f"<div {_class_attr(mdx=mdx)} {_style_attr(outer_style, mdx=mdx)}>"
        f"<div {_style_attr(header_style, mdx=mdx)}>"
        f"<div {_style_attr(dots_row_style, mdx=mdx)}>"
        f"<span {_style_attr(red_dot_style, mdx=mdx)}></span>"
        f"<span {_style_attr(yellow_dot_style, mdx=mdx)}></span>"
        f"<span {_style_attr(green_dot_style, mdx=mdx)}></span>"
        f"</div>"
        f"<div {_style_attr(title_style, mdx=mdx)}>{escaped_title}</div>"
        f"</div>"
        f"<pre {_style_attr(pre_style, mdx=mdx)}>"
        f"<code {_style_attr(code_style, mdx=mdx)}>{content}</code>"
        f"</pre>"
        f"</div>"
    )
