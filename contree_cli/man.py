"""Parse RST-style manual files into structured sections.

Manual files use title + underline format:

    Section Title
    =============

    Body text here.

The first title+underline is the document title. Subsequent ones
are section headings.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path


@dataclass(frozen=True)
class Section:
    title: str
    body: str


@dataclass(frozen=True)
class Manual:
    title: str
    sections: list[Section]

    def render(self) -> str:
        parts = [self.title, "=" * len(self.title)]
        for s in self.sections:
            parts.extend(("", s.title, "-" * len(s.title), s.body))
        return "\n".join(parts)

    def topics(self) -> dict[str, list[Section]]:
        result: dict[str, list[Section]] = {"all": list(self.sections)}
        for s in self.sections:
            key = s.title.lower().replace(" ", "_").replace("&", "and")
            if key:
                result[key] = [s]
                short = key.split("_")[0]
                if short and short not in result:
                    result[short] = [s]
        return result


def parse_manual(text: str) -> Manual:
    """Parse RST-style manual text into a Manual."""
    lines = text.split("\n")
    sections: list[Section] = []
    doc_title = ""
    title = ""
    body_lines: list[str] = []

    i = 0
    while i < len(lines):
        # Check if current line is a title with '===...' underline
        if (
            i + 1 < len(lines)
            and lines[i + 1].strip()
            and all(c == "=" for c in lines[i + 1].strip())
        ):
            if title or body_lines:
                sections.append(
                    Section(title=title, body="\n".join(body_lines).strip())
                )
            elif not doc_title and not sections:
                pass  # first title becomes doc_title
            title = lines[i].strip()
            if not doc_title:
                doc_title = title
                title = ""
            body_lines = []
            i += 2
        else:
            body_lines.append(lines[i])
            i += 1

    if title or body_lines:
        sections.append(Section(title=title, body="\n".join(body_lines).strip()))

    # Drop empty sections (gap between doc title and first heading)
    sections = [s for s in sections if s.title or s.body]

    return Manual(title=doc_title, sections=sections)


def load_file(name: str) -> Manual:
    """Load a manual file from the package directory."""
    path = Path(__file__).resolve().parent / name
    return parse_manual(path.read_text(encoding="utf-8"))


@cache
def agent_manual() -> Manual:
    return load_file("agent.md")


@cache
def user_manual() -> Manual:
    return load_file("manual.md")
