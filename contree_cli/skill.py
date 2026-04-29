from __future__ import annotations

import abc
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import contree_cli.config as config_mod

SKILL_NAME = "contree"
SKILL_REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS installed_skills (
    path TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


@cache
def skill_body_template() -> str:
    path = Path(__file__).resolve().parent / "skill_body.md"
    return path.read_text(encoding="utf-8")


def skill_version() -> str:
    try:
        from importlib.metadata import version

        return version("contree-cli")
    except Exception:
        return "unknown"


def parse_version(v: str) -> tuple[int, ...]:
    parts: list[int] = []
    for segment in v.split("."):
        try:
            parts.append(int(segment))
        except ValueError:
            break
    return tuple(parts)


def default_codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".codex"


def default_claude_home() -> Path:
    return Path.home() / ".claude"


# ── registry ─────────────────────────────────────────────


@contextmanager
def connect_registry() -> Iterator[sqlite3.Connection]:
    db_path = config_mod.CONTREE_HOME / "skills.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SKILL_REGISTRY_SCHEMA)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def normalize_install_path(path: Path) -> str:
    return str(path.expanduser())


def remember_installed(skill: Skill) -> None:
    normalized = normalize_install_path(skill.path)
    with connect_registry() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO installed_skills (path, kind, updated_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP)",
            (normalized, skill.kind),
        )


def forget_installed(skill: Skill) -> None:
    normalized = normalize_install_path(skill.path)
    with connect_registry() as conn:
        conn.execute("DELETE FROM installed_skills WHERE path = ?", (normalized,))


# ── text constants ───────────────────────────────────────


SKILL_DESCRIPTION = """\
Use when the user needs to operate ConTree sandboxes through the contree CLI: \
choose or resume explicit sessions, run commands in VM-isolated environments, \
inspect images without spawning a VM, stage file changes, branch or roll back \
sandbox state, reuse tagged images, or automate rollback-safe agent workflows.\
"""

BUNDLED_INTRO = """\
This skill is installed by `contree skill install` (version `{version}`). \
It uses the `contree` CLI from PATH.\
"""

BUNDLED_FIRST_STEP = """\
1. Use `contree` from PATH — no bundled wrapper needed.
2. Output formats (global `-f` flag BEFORE the subcommand):
   `-f json` (JSONL), `-f json-pretty`, `-f csv`, `-f tsv`,
   `-f plain`, `-f table`, `-f toml`, `-f default`
3. For the full built-in manual: `contree agent`\
"""

BUNDLED_FALLBACK = """\

## Quick reference

```bash
contree -S <key> use tag:alpine:latest
contree -S <key> run -s -- apt-get update
contree -S <key> run -- make test
contree agent                          # full manual
contree <command> --help               # per-command help
```\
"""

BUNDLED_REFERENCES = """\

## Where to look next

- Run `contree agent` for the full built-in manual.
- Run `contree agent <topic>` for details on a specific topic.
- Run `contree <command> --help` for per-command syntax.
"""

SUBAGENT_DESCRIPTION = """\
Use proactively when the user needs to operate ConTree sandboxes through the \
contree CLI, especially for explicit session management, rollback-safe \
execution, image inspection, file staging, branching, and tagged environment \
reuse.\
"""

SUBAGENT_INTRO = """\
This Claude Code subagent is installed by `contree skill install` in \
Claude-compatible Markdown format. It assumes the `contree` CLI is installed \
and available on `PATH`.\
"""

SUBAGENT_FIRST_STEP = "1. Use the local `contree` executable from `PATH`."

AGENT_DESCRIPTION = (
    "Use proactively when the user needs sandboxed execution "
    "through ConTree — sessions, images, run, rollback, tagging."
)

AGENT_TEMPLATE = """\
---
name: {name}
description: >-
  {description}
tools:
  - Bash
  - Read
  - Grep
skills:
  - contree
---

ConTree sandbox agent. Preloads the `contree` skill for all operations.

When there are multiple approaches to a task, launch separate subagents
with isolated contree sessions (`-S <unique_key>`) for each approach,
then compare results.

Run `contree agent` for the full built-in manual.
Run `contree agent <topic>` for details on a specific topic.
"""

OPENAI_DESCRIPTION = """\
Run ConTree workflows through a bundled dependency-free CLI\
"""

OPENAI_TEMPLATE = """\
interface:
  display_name: "ConTree"
  short_description: "{description}"
  default_prompt: "Use ${name} to operate ConTree with explicit sessions, \
read-only inspection first, and small rollback-safe sandbox steps."
"""

CODEX_RULES = 'prefix_rule(pattern=["contree"], decision="allow")\n'


# ── Skill base ───────────────────────────────────────────


@dataclass(frozen=True)
class Skill(abc.ABC):
    """Agent-tool skill with a resolved install path."""

    path: Path

    name: str = SKILL_NAME
    kind: str = ""

    @property
    def allowed_tools(self) -> str:
        return "Bash(contree:*)"

    @property
    def description(self) -> str:
        return SKILL_DESCRIPTION

    def intro(self) -> str:
        return BUNDLED_INTRO.format(version=skill_version())

    def first_step(self) -> str:
        return BUNDLED_FIRST_STEP

    def fallback(self) -> str:
        return BUNDLED_FALLBACK

    def references(self) -> str:
        return BUNDLED_REFERENCES

    def frontmatter(self) -> str:
        return f'---\nname: "{self.name}"\ndescription: "{self.description}"\n---\n'

    def body(self) -> str:
        return skill_body_template().format(
            intro=self.intro(),
            first_step=self.first_step(),
            fallback=self.fallback(),
            references=self.references(),
        )

    def render(self) -> str:
        """Generate SKILL.md content (frontmatter + body)."""
        fm = self.frontmatter()
        return fm + "\n" + self.body() if fm else self.body()

    def openai_yaml(self) -> str:
        return OPENAI_TEMPLATE.format(description=OPENAI_DESCRIPTION, name=SKILL_NAME)

    @classmethod
    @abc.abstractmethod
    def home_dir(cls) -> Path: ...

    @classmethod
    def skills_dir(cls) -> Path:
        return cls.home_dir() / "skills"

    @classmethod
    def resolve_path(cls, hint: str) -> Path:
        if not hint:
            return (Path(f".{cls.kind}") / "skills" / SKILL_NAME).resolve()
        if hint == "~":
            return cls.skills_dir() / SKILL_NAME
        return Path(hint).expanduser().resolve()

    @property
    def installed_version(self) -> str:
        vf = self.path / ".version"
        if vf.is_file():
            return vf.read_text(encoding="utf-8").strip()
        return ""

    @property
    def needs_upgrade(self) -> bool:
        v = self.installed_version
        if not v:
            return True
        return parse_version(v) < parse_version(skill_version())

    @property
    def exists(self) -> bool:
        return self.path.exists()

    def install(self, *, force: bool = False) -> None:
        if self.path.exists() and not force:
            raise FileExistsError(self.path)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f"{SKILL_NAME}-",
            dir=self.path.parent,
        ) as tmp_str:
            tmp = Path(tmp_str)
            self.build_tree(tmp)
            if self.path.exists():
                shutil.rmtree(self.path)
            tmp.replace(self.path)

    def build_tree(self, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        agents = dest / "agents"
        agents.mkdir(parents=True, exist_ok=True)

        (dest / ".version").write_text(skill_version(), encoding="utf-8")
        (dest / "SKILL.md").write_text(self.render(), encoding="utf-8")
        (agents / "openai.yaml").write_text(self.openai_yaml(), encoding="utf-8")

    def remove(self) -> None:
        shutil.rmtree(self.path)

    def __hash__(self) -> int:
        return hash((self.kind, self.path))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Skill):
            return NotImplemented
        return self.kind == other.kind and self.path == other.path


# ── Concrete skill types ─────────────────────────────────


@dataclass(frozen=True)
class ClaudeSkill(Skill):
    kind: str = "claude"

    @classmethod
    def home_dir(cls) -> Path:
        return default_claude_home()

    def frontmatter(self) -> str:
        return (
            "---\n"
            f'name: "{self.name}"\n'
            f'description: "{self.description}"\n'
            f"allowed-tools: {self.allowed_tools}\n"
            "---\n"
        )


@dataclass(frozen=True)
class CodexSkill(Skill):
    kind: str = "codex"

    @classmethod
    def home_dir(cls) -> Path:
        return default_codex_home()

    @classmethod
    def rules_path(cls) -> Path:
        return cls.home_dir() / "rules" / f"{SKILL_NAME}.rules"

    def install(self, *, force: bool = False) -> None:
        super().install(force=force)
        rules = self.rules_path()
        rules.parent.mkdir(parents=True, exist_ok=True)
        rules.write_text(CODEX_RULES, encoding="utf-8")

    def remove(self) -> None:
        super().remove()
        rules = self.rules_path()
        if rules.exists():
            rules.unlink()


@dataclass(frozen=True)
class OpenCodeSkill(Skill):
    kind: str = "opencode"

    @classmethod
    def home_dir(cls) -> Path:
        raw = os.environ.get("OPENCODE_HOME")
        if raw:
            return Path(raw).expanduser()
        return Path.home() / ".config" / "opencode"


@dataclass(frozen=True)
class AmpSkill(Skill):
    kind: str = "amp"

    @classmethod
    def home_dir(cls) -> Path:
        return Path.home() / ".config" / "agents"


@dataclass(frozen=True)
class ClineSkill(Skill):
    kind: str = "cline"

    @classmethod
    def home_dir(cls) -> Path:
        raw = os.environ.get("CLINE_DIR")
        if raw:
            return Path(raw).expanduser()
        return Path.home() / ".cline"


@dataclass(frozen=True)
class ClaudeSubagentSkill(Skill):
    kind: str = "claude-subagent"

    @classmethod
    def home_dir(cls) -> Path:
        return default_claude_home()

    @classmethod
    def resolve_path(cls, hint: str) -> Path:
        if not hint:
            return (Path(".claude") / "agents" / f"{SKILL_NAME}-subagent.md").resolve()
        if hint == "~":
            return cls.home_dir() / "agents" / f"{SKILL_NAME}-subagent.md"
        return Path(hint).expanduser().resolve()

    @property
    def description(self) -> str:
        return SUBAGENT_DESCRIPTION

    def intro(self) -> str:
        return SUBAGENT_INTRO

    def first_step(self) -> str:
        return SUBAGENT_FIRST_STEP

    def fallback(self) -> str:
        return ""

    def references(self) -> str:
        return ""

    def frontmatter(self) -> str:
        return (
            "---\n"
            f"name: {self.name}\n"
            f"description: {self.description}\n"
            f"allowed-tools: {self.allowed_tools}\n"
            "---\n"
        )

    def install(self, *, force: bool = False) -> None:
        if self.path.exists() and not force:
            raise FileExistsError(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.render(), encoding="utf-8")

    def remove(self) -> None:
        self.path.unlink()


@dataclass(frozen=True)
class ClaudeAgentSkill(Skill):
    kind: str = "claude-agent"

    @classmethod
    def home_dir(cls) -> Path:
        return default_claude_home()

    @classmethod
    def resolve_path(cls, hint: str) -> Path:
        if not hint:
            return (Path(".claude") / "agents" / f"{SKILL_NAME}.md").resolve()
        if hint == "~":
            return cls.home_dir() / "agents" / f"{SKILL_NAME}.md"
        return Path(hint).expanduser().resolve()

    def render(self) -> str:
        return AGENT_TEMPLATE.format(
            name=self.name,
            description=AGENT_DESCRIPTION,
        )

    def install(self, *, force: bool = False) -> None:
        if self.path.exists() and not force:
            raise FileExistsError(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.render(), encoding="utf-8")

    def remove(self) -> None:
        self.path.unlink()


# ── Skill type registry ──────────────────────────────────

ALL_SKILL_TYPES: list[type[Skill]] = [
    CodexSkill,
    ClaudeSkill,
    OpenCodeSkill,
    AmpSkill,
    ClineSkill,
    ClaudeSubagentSkill,
    ClaudeAgentSkill,
]

SKILL_BY_KIND: dict[str, type[Skill]] = {cls.kind: cls for cls in ALL_SKILL_TYPES}

PATH_MARKERS: dict[str, type[Skill]] = {
    ".claude": ClaudeSkill,
    ".codex": CodexSkill,
    "opencode": OpenCodeSkill,
    "agents": AmpSkill,
    ".cline": ClineSkill,
}


def skill_from_spec(spec: str) -> Skill:
    """Parse a spec string into a Skill instance.

    claude:       → ClaudeSkill(path=$PWD/.claude/skills/contree)
    claude:~      → ClaudeSkill(path=~/.claude/skills/contree)
    codex:~       → CodexSkill(path=~/.codex/skills/contree)
    ./path        → guessed class with explicit path
    """
    if ":" in spec:
        kind, hint = spec.split(":", 1)
        hint = hint.lstrip("/")
        skill_cls = SKILL_BY_KIND.get(kind)
        if skill_cls is not None:
            return skill_cls(path=skill_cls.resolve_path(hint))
    path = Path(spec).expanduser().resolve()
    return guess_skill(path)


CLAUDE_SKILL_TYPES: frozenset[type[Skill]] = frozenset(
    {ClaudeSkill, ClaudeAgentSkill, ClaudeSubagentSkill}
)


def default_install_specs() -> Iterable[Skill]:
    """Return skill instances for default global install.

    Claude-based types require ``~/.claude`` to exist.
    All other types are installed unconditionally.
    """
    specs: list[Skill] = [
        skill_from_spec(f"{cls.kind}:~")
        for cls in ALL_SKILL_TYPES
        if cls not in CLAUDE_SKILL_TYPES
    ]
    if default_claude_home().is_dir():
        for cls in ALL_SKILL_TYPES:
            if cls in CLAUDE_SKILL_TYPES:
                specs.append(skill_from_spec(f"{cls.kind}:~"))
    return specs


def guess_skill(path: Path) -> Skill:
    """Guess skill type from a raw path."""
    normalized = path.expanduser()
    if normalized.suffix == ".md":
        return ClaudeSubagentSkill(path=normalized)
    for marker, cls in PATH_MARKERS.items():
        if marker in normalized.parts:
            return cls(path=normalized)
    return ClaudeSkill(path=normalized)


def list_installed() -> frozenset[Skill]:
    with connect_registry() as conn:
        rows = conn.execute(
            "SELECT path, kind FROM installed_skills ORDER BY path"
        ).fetchall()
        stale: list[str] = []
        results: list[Skill] = []
        for row in rows:
            kind, path = row["kind"], Path(row["path"])
            if not path.exists():
                stale.append(row["path"])
                continue
            cls = SKILL_BY_KIND.get(kind or "")
            if cls is not None:
                results.append(cls(path=path))
            else:
                results.append(guess_skill(path))
        if stale:
            conn.executemany(
                "DELETE FROM installed_skills WHERE path = ?",
                [(p,) for p in stale],
            )
            conn.commit()
    return frozenset(results)
