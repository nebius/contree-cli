"""Parse and match ``.dockerignore`` rules against build-context paths.

Rules are matched in order against POSIX-style paths relative to the context
root. The last matching rule wins (``!`` re-includes a previously ignored
path). Globs: ``*`` matches anything except ``/``, ``**`` matches zero or
more path components, ``?`` matches one character, ``[...]`` is a class.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DockerignoreRule:
    negate: bool
    regex: re.Pattern[str]
    raw: str


def parse_dockerignore(context_dir: Path) -> tuple[DockerignoreRule, ...]:
    """Read ``.dockerignore`` from ``context_dir`` and return the rule list."""
    path = context_dir / ".dockerignore"
    if not path.is_file():
        return ()
    rules: list[DockerignoreRule] = []
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        if negate:
            line = line[1:].strip()
        regex_str = pattern_to_regex(line)
        rules.append(
            DockerignoreRule(
                negate=negate,
                regex=re.compile(regex_str),
                raw=raw_line,
            )
        )
    return tuple(rules)


def is_ignored(rel_path: str, rules: tuple[DockerignoreRule, ...]) -> bool:
    """Apply ordered rules to the path and its parents; the last match wins."""
    parts = rel_path.split("/")
    candidates = ["/".join(parts[:index]) for index in range(1, len(parts) + 1)]
    ignored = False
    for rule in rules:
        if any(rule.regex.fullmatch(candidate) for candidate in candidates):
            ignored = not rule.negate
    return ignored


def may_include_descendants(rel_path: str, rules: tuple[DockerignoreRule, ...]) -> bool:
    """Keep an ignored directory only if a later exception can reach inside it."""
    parts = rel_path.split("/")
    candidates = ["/".join(parts[:index]) for index in range(1, len(parts) + 1)]
    last_match = -1
    for index, rule in enumerate(rules):
        if any(rule.regex.fullmatch(candidate) for candidate in candidates):
            last_match = index
    if last_match < 0 or rules[last_match].negate:
        return True
    return any(
        rule.negate
        and pattern_may_match_descendant(rule.raw.strip()[1:].strip(), rel_path)
        for rule in rules[last_match + 1 :]
    )


def pattern_may_match_descendant(pattern: str, rel_path: str) -> bool:
    """Compare fixed components; retain ambiguous patterns that can cross '/'."""
    components = pattern.strip("/").split("/")
    for pattern_part, path_part in zip(components, rel_path.split("/"), strict=False):
        unfinished_class = any(
            "]" not in suffix for suffix in pattern_part.split("[")[1:]
        )
        if "**" in pattern_part or unfinished_class:
            return True
        if re.fullmatch(pattern_to_regex(pattern_part), path_part) is None:
            return False
    return True


def pattern_to_regex(pattern: str) -> str:
    """Translate a ``.dockerignore`` glob into a Python regex string.

    Handles trailing ``/`` (directory + all contents), ``**`` (any number of
    path components), ``*`` (one segment), ``?`` (one char), ``[...]`` class.
    Other regex metacharacters are escaped.
    """
    is_dir = pattern.endswith("/")
    if is_dir:
        pattern = pattern.rstrip("/")
    pattern = pattern.lstrip("/")

    out: list[str] = []
    i = 0
    while i < len(pattern):
        match pattern[i : i + 3], pattern[i : i + 2], pattern[i]:
            case ("**/", _, _):
                out.append("(?:.*/)?")
                i += 3
            case (_, "**", _):
                out.append(".*")
                i += 2
            case (_, _, "*"):
                out.append("[^/]*")
                i += 1
            case (_, _, "?"):
                out.append("[^/]")
                i += 1
            case (_, _, "["):
                end = pattern.find("]", i + 1)
                if end == -1:
                    out.append(re.escape("["))
                    i += 1
                else:
                    out.append(pattern[i : end + 1])
                    i = end + 1
            case (_, _, "/"):
                out.append("/")
                i += 1
            case (_, _, ch):
                out.append(re.escape(ch))
                i += 1

    regex = "".join(out)
    if is_dir:
        regex += "(?:/.*)?"
    return regex
