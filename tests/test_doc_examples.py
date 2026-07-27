"""Validate every documented `contree` invocation against the real parser.

Extracts command lines from fenced bash blocks and `{terminal-shell}`
directives in README.md, docs/, the built-in manuals (agent.md,
manual.md) and the rendered skill bodies, then checks each one:

- the subcommand path must resolve (`contree <path> --help` exits 0);
- every flag used in the example must appear in that help text
  (or in the root `contree --help` for global flags).

This catches stale flags (`-K`, `--tagged`, `tag -d`) and renamed or
removed subcommands whenever the CLI and the docs drift apart.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from functools import cache
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

DOC_FILES = sorted(
    {
        REPO / "README.md",
        REPO / "contree_cli" / "agent.md",
        REPO / "contree_cli" / "manual.md",
        *(p for p in (REPO / "docs").rglob("*.md") if "_build" not in p.parts),
    }
)

FENCE_RE = re.compile(r"```(?:bash|shell|console)\n(.*?)```", re.S)
DIRECTIVE_RE = re.compile(r"```\{terminal-shell\}\s+(contree[^\n]*)")
# meta-syntax placeholders that make a line non-literal
PLACEHOLDER_RE = re.compile(r"<[a-zA-Z][^>]*>|\.\.\.|\bUUID\b|\bIMAGE\b|\bREF\b")


def extract_contree_lines(text: str) -> list[str]:
    lines: list[str] = []
    for block in FENCE_RE.findall(text):
        # join backslash continuations
        block = block.replace("\\\n", " ")
        for raw in block.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            # drop trailing inline comments
            line = re.split(r"\s+#", line)[0]
            # the payload after ` -- ` is the remote command; drop it so
            # quotes with `&&` inside do not confuse the extraction
            line = line.split(" -- ")[0] + (" --" if " -- " in line else "")
            # unwrap eval $(...), VAR=$(...), pipes/redirects: keep the
            # contree part only
            for m in re.finditer(r"contree\s[^|;)&<>]*", line):
                lines.append(m.group(0).strip())
    lines.extend(m.strip() for m in DIRECTIVE_RE.findall(text))
    return lines


def rendered_skill_bodies() -> list[tuple[str, str]]:
    from contree_cli.skill import (
        AmpSkill,
        ClaudeAgentSkill,
        ClaudeSkill,
        ClaudeSubagentSkill,
        ClineSkill,
        CodexSkill,
        OpenCodeSkill,
    )

    out = []
    for cls in (
        ClaudeSkill,
        CodexSkill,
        OpenCodeSkill,
        AmpSkill,
        ClineSkill,
        ClaudeSubagentSkill,
        ClaudeAgentSkill,
    ):
        skill = cls(path=Path("/tmp/doc-lint"))
        out.append((f"skill:{cls.__name__}", skill.render()))
    return out


@cache
def help_text(subpath: tuple[str, ...]) -> str | None:
    """Return `contree <subpath> --help` output, or None if the path is invalid."""
    proc = subprocess.run(
        [sys.executable, "-m", "contree_cli", *subpath, "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout + proc.stderr


ROOT_HELP_KEY: tuple[str, ...] = ()


def split_invocation(tokens: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Split into (path, pre_flags, post_flags).

    ``pre_flags`` appear before the subcommand and must be global;
    ``post_flags`` follow it and must belong to the subcommand itself
    (the global parser never sees them). ``path`` greedily collects
    non-flag tokens; resolve_subcommand() later shrinks it to the real
    command prefix, and the leftover tokens are positional values.
    """
    path: list[str] = []
    pre_flags: list[str] = []
    post_flags: list[str] = []
    i = 1
    expects_value = False
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            break  # payload follows
        if tok.startswith("-"):
            flag = tok.split("=", 1)[0]
            (post_flags if path else pre_flags).append(flag)
            # values never start with "-" and are consumed lazily below
            expects_value = "=" not in tok
            i += 1
            continue
        if expects_value:
            expects_value = False
            i += 1
            continue
        if path and path[0] in ("run", "r"):
            # direct-mode run: the first positional starts the remote
            # command, whose own flags are not contree's business
            break
        if PLACEHOLDER_RE.search(tok) or tok.startswith(("$", "'", '"')):
            i += 1
            continue
        path.append(tok)
        i += 1
    return path, pre_flags, post_flags


def resolve_subcommand(path: list[str]) -> tuple[tuple[str, ...], str] | None:
    """Longest prefix of *path* that resolves to a real subcommand help.

    A non-empty path must resolve at depth >= 1: falling back to the
    root parser would make any misspelled command "valid".
    """
    lowest = 0 if not path else 1
    for n in range(len(path), lowest - 1, -1):
        sub = tuple(path[:n])
        text = help_text(sub)
        if text is not None:
            return sub, text
    return None


FLAG_LEXICON_RE = re.compile(r"(?<![\w-])--?[a-zA-Z][\w-]*")


@cache
def flag_lexicon(subpath: tuple[str, ...]) -> frozenset[str]:
    text = help_text(subpath) or ""
    return frozenset(FLAG_LEXICON_RE.findall(text))


def collect_cases() -> list[tuple[str, str]]:
    cases: list[tuple[str, str]] = []
    for f in DOC_FILES:
        for line in extract_contree_lines(f.read_text(encoding="utf-8")):
            cases.append((str(f.relative_to(REPO)), line))
    for name, body in rendered_skill_bodies():
        for line in extract_contree_lines(body):
            cases.append((name, line))
    # dedupe, keep first source for the message
    seen: dict[str, str] = {}
    for src, line in cases:
        seen.setdefault(line, src)
    return [(src, line) for line, src in seen.items()]


CASES = collect_cases()


def test_docs_have_examples() -> None:
    assert len(CASES) > 200


@pytest.mark.parametrize(
    ("source", "line"),
    CASES,
    ids=[f"{src}::{line[:60]}" for src, line in CASES],
)
def test_documented_command_is_valid(source: str, line: str) -> None:
    try:
        tokens = shlex.split(line, posix=True)
    except ValueError:
        pytest.skip(f"unparseable shell line: {line!r}")
    assert tokens and tokens[0] == "contree"

    path, pre_flags, post_flags = split_invocation(tokens)

    resolved = resolve_subcommand(path)
    assert resolved is not None, f"{source}: no such command path {path!r} in {line!r}"
    sub, text = resolved

    # A leftover positional that lands on a pure-namespace command
    # (usage shows only a {a,b,c} choice set) is a typo, not a value.
    leftover = path[len(sub) :]
    choices_match = re.search(r"\{([\w,-]+)\}", text)
    if leftover and choices_match:
        choices = set(choices_match.group(1).split(","))
        assert leftover[0] in choices, (
            f"{source}: {leftover[0]!r} is not a subcommand of"
            f" `contree {' '.join(sub)}` (from {line!r})"
        )

    for flag in pre_flags:
        if PLACEHOLDER_RE.search(flag):
            continue
        assert flag in flag_lexicon(ROOT_HELP_KEY), (
            f"{source}: {flag!r} is not a global contree flag (from {line!r})"
        )
    for flag in post_flags:
        if PLACEHOLDER_RE.search(flag):
            continue
        assert flag in flag_lexicon(sub), (
            f"{source}: flag {flag!r} not accepted by"
            f" `contree {' '.join(sub)}` (from {line!r})"
        )
