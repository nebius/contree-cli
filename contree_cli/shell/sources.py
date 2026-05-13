"""Named completion sources for the shell.

Each source produces candidate completions for one kind of value
(image, operation, branch, profile, sandbox path, ...). Sources are
keyed by short name and looked up by the completer via
:mod:`contree_cli.shell.argmap`, which maps ``(command_path, dest)``
pairs to source names.

Sources accept ``client=None`` and ``store=None`` and return ``[]`` so
they remain safe in tests and during partial setup.
"""

from __future__ import annotations

import json
import logging
import os
import posixpath
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from contree_cli.config import Config
from contree_cli.mapped_file import split_mapped_value
from contree_cli.output import FORMATTERS
from contree_cli.shell.cache import SourceCache

if TYPE_CHECKING:
    from contree_cli.client import ContreeClient
    from contree_cli.session import SessionStore

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Context passed to sources at completion time
# ---------------------------------------------------------------------------


SandboxListFn = Callable[
    [str, str],
    "list[dict[str, object]] | None",
]


@dataclass(frozen=True)
class CompletionContext:
    client: ContreeClient | None
    store: SessionStore | None
    cache: SourceCache | None
    profile: str
    cwd: str
    tokens: tuple[str, ...]
    list_dir: SandboxListFn | None = None


SourceFn = Callable[[str, CompletionContext], list[str]]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


EDITOR_ALIASES: tuple[str, ...] = ("vim", "vi", "nvim", "nano")
SKILL_SPEC_PREFIXES: tuple[str, ...] = (
    "claude:",
    "claude:~",
    "codex:~",
    "opencode:~",
    "amp:~",
    "cline:~",
    "claude-subagent:",
    "claude-agent:",
)
TAG_LETTERS: tuple[str, ...] = ("u", "g", "m")
PS_KIND_CHOICES: tuple[str, ...] = ("instance", "import", "delete")
PS_STATUS_CHOICES: tuple[str, ...] = (
    "PENDING",
    "ASSIGNED",
    "EXECUTING",
    "SUCCESS",
    "FAILED",
    "CANCELLED",
)


def with_trailing_space(names: Iterable[str], text: str) -> list[str]:
    return [n + " " for n in names if n.startswith(text)]


# ---------------------------------------------------------------------------
# Live data sources
# ---------------------------------------------------------------------------


def fetch_images(ctx: CompletionContext) -> list[dict[str, object]]:
    """Return the list of images (cached, profile-namespaced)."""
    if ctx.client is None:
        return []
    if ctx.cache is not None:
        cached = ctx.cache.get(scope="", kind="images", ttl=60.0)
        if isinstance(cached, list):
            return cached
    try:
        resp = ctx.client.get("/v1/images", params={"limit": "100"})
        data = json.loads(resp.read())
        images: list[dict[str, object]] = data.get("images", [])
    except Exception:
        log.debug("image source: API call failed", exc_info=True)
        return []
    if ctx.cache is not None:
        ctx.cache.set(scope="", kind="images", value=images)
    return images


def complete_image(text: str, ctx: CompletionContext) -> list[str]:
    """Complete image references: ``tag:NAME`` or UUID.

    Bare text matching a tag name auto-prefixes the candidate with
    ``tag:``; explicit ``tag:`` text filters by the full candidate.
    """
    images = fetch_images(ctx)
    results: list[str] = []
    for img in images:
        tag = img.get("tag")
        if isinstance(tag, str) and tag:
            prefixed = f"tag:{tag}"
            if text.startswith("tag:"):
                if prefixed.startswith(text):
                    results.append(prefixed + " ")
            elif tag.startswith(text):
                results.append(prefixed + " ")
        uuid_str = img.get("uuid")
        if isinstance(uuid_str, str) and uuid_str.startswith(text):
            results.append(uuid_str + " ")
    return results


def fetch_operations(ctx: CompletionContext) -> list[dict[str, object]]:
    """Return recent operations (cached short TTL, profile-namespaced)."""
    if ctx.client is None:
        return []
    if ctx.cache is not None:
        cached = ctx.cache.get(scope="", kind="operations", ttl=5.0)
        if isinstance(cached, list):
            return cached
    ops: list[dict[str, object]]
    try:
        resp = ctx.client.get("/v1/operations", params={"limit": "100"})
        data = json.loads(resp.read())
        if isinstance(data, dict):
            ops = list(data.get("operations", []))
        elif isinstance(data, list):
            ops = list(data)
        else:
            ops = []
    except Exception:
        log.debug("operation source: API call failed", exc_info=True)
        return []
    if ctx.cache is not None:
        ctx.cache.set(scope="", kind="operations", value=ops)
    return ops


def complete_operation(text: str, ctx: CompletionContext) -> list[str]:
    ops = fetch_operations(ctx)
    results: list[str] = []
    for op in ops:
        uuid_str = op.get("uuid")
        if isinstance(uuid_str, str) and uuid_str.startswith(text):
            results.append(uuid_str + " ")
    return results


def complete_session(text: str, ctx: CompletionContext) -> list[str]:
    """Complete session keys plus their underscore-suffix shorthand."""
    if ctx.store is None:
        return []
    try:
        sessions = ctx.store.list_sessions()
    except Exception:
        log.debug("session source: store call failed", exc_info=True)
        return []
    results: list[str] = []
    for s in sessions:
        key = s.session_key
        if key.startswith(text):
            results.append(key + " ")
        suffix = key.rsplit("_", 1)[-1] if "_" in key else ""
        if suffix and suffix != key and suffix.startswith(text):
            results.append(suffix + " ")
    return results


def complete_branch(text: str, ctx: CompletionContext) -> list[str]:
    if ctx.store is None:
        return []
    try:
        branches = ctx.store.list_branches()
    except Exception:
        log.debug("branch source: store call failed", exc_info=True)
        return []
    return [name + " " for name, _active in branches if name.startswith(text)]


# ---------------------------------------------------------------------------
# Sandbox path sources (use /inspect/<uuid>/list)
# ---------------------------------------------------------------------------


def list_sandbox_dir(
    ctx: CompletionContext,
    image_uuid: str,
    dir_path: str,
) -> list[dict[str, object]] | None:
    """Cache-aware wrapper around ``/v1/inspect/<uuid>/list``.

    Returns ``None`` on hard failures so the caller can short-circuit.
    """
    if ctx.client is None:
        return None
    cache_kind = f"files:{dir_path}"
    if ctx.cache is not None:
        cached = ctx.cache.get(scope=image_uuid, kind=cache_kind, ttl=30.0)
        if isinstance(cached, list):
            return cached
    try:
        from contree_cli.client import resolve_image

        uuid = resolve_image(ctx.client, image_uuid)
        resp = ctx.client.get(
            f"/v1/inspect/{uuid}/list",
            params={"path": dir_path},
        )
        data = json.loads(resp.read())
        files: list[dict[str, object]] = data.get("files", [])
    except Exception:
        log.debug("sandbox source: API call failed", exc_info=True)
        return None
    if ctx.cache is not None:
        ctx.cache.set(scope=image_uuid, kind=cache_kind, value=files)
    return files


def complete_sandbox(
    text: str,
    ctx: CompletionContext,
    *,
    dirs_only: bool = False,
    rooted_at: str | None = None,
) -> list[str]:
    """Generic sandbox path completion.

    *rooted_at* forces the resolution root (used for the instance-path
    segment of ``--file`` mapped specs which always starts at ``/``).
    """
    if ctx.store is None or ctx.client is None:
        return []
    session = ctx.store.session
    if session is None:
        return []
    image_uuid = session.current_image

    if "/" in text:
        last_slash = text.rindex("/")
        user_dir = text[: last_slash + 1] or "/"
        prefix = text[last_slash + 1 :]
        if rooted_at is not None and not user_dir.startswith("/"):
            user_dir = rooted_at.rstrip("/") + "/" + user_dir
        resolved = (
            ctx.store.resolve_path(user_dir)
            if rooted_at is None
            else (
                posixpath.normpath(user_dir) if user_dir.startswith("/") else user_dir
            )
        )
        api_dir = resolved if resolved == "/" else resolved + "/"
    else:
        user_dir = ""
        prefix = text
        if rooted_at is not None:
            api_dir = "/" if rooted_at == "/" else rooted_at + "/"
        else:
            resolved = ctx.store.resolve_path("")
            api_dir = resolved if resolved == "/" else resolved + "/"

    if ctx.list_dir is not None:
        entries = ctx.list_dir(image_uuid, api_dir)
    else:
        entries = list_sandbox_dir(ctx, image_uuid, api_dir)
    if entries is None:
        return []

    results: list[str] = []
    for entry in entries:
        path = entry.get("path", "")
        if not isinstance(path, str) or not path:
            continue
        is_dir = bool(entry.get("is_dir"))
        if dirs_only and not is_dir:
            continue
        name = path.rsplit("/", 1)[-1]
        if not name.startswith(prefix):
            continue
        full = user_dir + name
        if is_dir:
            full += "/"
        else:
            full += " "
        results.append(full)
    return results


def complete_sandbox_path(text: str, ctx: CompletionContext) -> list[str]:
    return complete_sandbox(text, ctx)


def complete_sandbox_dir(text: str, ctx: CompletionContext) -> list[str]:
    return complete_sandbox(text, ctx, dirs_only=True)


# ---------------------------------------------------------------------------
# Local file system path source
# ---------------------------------------------------------------------------


def complete_host_path(text: str, ctx: CompletionContext) -> list[str]:
    """Complete a local filesystem path. Honours ``~`` and ``./``."""
    expanded = os.path.expanduser(text) if text.startswith("~") else text
    if "/" in expanded:
        slash = expanded.rindex("/")
        directory = expanded[: slash + 1] or "/"
        prefix = expanded[slash + 1 :]
        user_directory = text[: text.rindex("/") + 1] if "/" in text else ""
    else:
        directory = "."
        prefix = expanded
        user_directory = ""

    try:
        entries = list(os.scandir(directory))
    except OSError:
        return []

    results: list[str] = []
    for entry in entries:
        name = entry.name
        if not name.startswith(prefix):
            continue
        is_dir = entry.is_dir(follow_symlinks=False)
        full = user_directory + name + ("/" if is_dir else " ")
        results.append(full)
    return results


# ---------------------------------------------------------------------------
# Mapped --file value (whole-token replacement)
# ---------------------------------------------------------------------------


TAG_RE = re.compile(r"^[ugm]")


def complete_mapped_file(text: str, ctx: CompletionContext) -> list[str]:
    """Complete a ``--file host[:inst][:u][:g][:m]`` value.

    readline's delimiters do not include ``:``, so ``text`` is the entire
    value. Each candidate must therefore replace the whole token, not just
    the trailing segment.
    """
    # On Windows the host path may carry a drive prefix (``C:``) whose colon
    # is part of the path, not the host/instance separator. Mirror the
    # ``MappedFile.parse`` heuristic by peeling the drive off before splitting.
    drive, rest = os.path.splitdrive(text)
    parts = split_mapped_value(rest)
    head = parts[:-1]
    tail = parts[-1] if parts else ""
    prefix = drive + ((":".join(head) + ":") if head else "")

    # Segment 0: host path completion. Trailing "/" for dirs (no space) so the
    # user can keep typing "/foo" or ":m0" next.
    if not head:
        host_candidates = complete_host_path(drive + tail, ctx)
        return [cand.rstrip(" ") for cand in host_candidates]

    # Subsequent segments.
    if tail.startswith("/") or tail == "":
        # Instance path: complete against sandbox rooted at /, plus optional
        # tag tokens when the tail is empty.
        results: list[str] = []
        if tail.startswith("/"):
            sandbox_candidates = complete_sandbox(tail, ctx, rooted_at="/")
            for cand in sandbox_candidates:
                clean = cand.rstrip(" ")
                results.append(prefix + clean)
            return results
        # Empty tail right after a colon: offer tag tokens that have not been
        # seen yet, plus a "/" hint to start an instance path.
        seen_tags = {p[0] for p in head[1:] if p and p[0] in TAG_LETTERS}
        for tag in TAG_LETTERS:
            if tag not in seen_tags:
                results.append(prefix + tag)
        if not any(p.startswith("/") for p in head[1:]):
            results.append(prefix + "/")
        return results

    if TAG_RE.match(tail):
        # Tagged option being typed (e.g. "m0755"). No further completion
        # logic, just echo back so readline does not delete the value.
        return [prefix + tail]

    return []


# ---------------------------------------------------------------------------
# Misc sources
# ---------------------------------------------------------------------------


def complete_format(text: str, ctx: CompletionContext) -> list[str]:
    return with_trailing_space(sorted(FORMATTERS), text)


def complete_editor(text: str, ctx: CompletionContext) -> list[str]:
    return with_trailing_space(EDITOR_ALIASES, text)


def complete_skill_spec(text: str, ctx: CompletionContext) -> list[str]:
    candidates = [prefix for prefix in SKILL_SPEC_PREFIXES if prefix.startswith(text)]
    if not candidates and ("/" in text or text.startswith(".")):
        return complete_host_path(text, ctx)
    return candidates


def complete_profile(text: str, ctx: CompletionContext) -> list[str]:
    try:
        cfg = Config()
    except Exception:
        log.debug("profile source: failed to load Config", exc_info=True)
        return []
    return with_trailing_space(sorted(cfg.keys()), text)


def complete_env_key(text: str, ctx: CompletionContext) -> list[str]:
    if ctx.store is None:
        return []
    try:
        env = ctx.store.get_env()
    except Exception:
        log.debug("env-key source: store call failed", exc_info=True)
        return []
    return with_trailing_space(sorted(env.keys()), text)


def complete_command_name(text: str, ctx: CompletionContext) -> list[str]:
    from contree_cli.shell.parser import get_command_names

    builtins = ("cd", "pwd", "history", "help", "clear", "exit", "quit")
    aliases = ("ls", "cat", "vim", "vi", "nvim", "nano", "--format", "-f")
    names = sorted({*get_command_names(), *builtins, *aliases})
    return with_trailing_space(names, text)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


SOURCES: dict[str, SourceFn] = {
    "image": complete_image,
    "operation": complete_operation,
    "session": complete_session,
    "branch": complete_branch,
    "sandbox-path": complete_sandbox_path,
    "sandbox-dir": complete_sandbox_dir,
    "host-path": complete_host_path,
    "mapped-file": complete_mapped_file,
    "format": complete_format,
    "editor": complete_editor,
    "skill-spec": complete_skill_spec,
    "profile": complete_profile,
    "env-key": complete_env_key,
    "command-name": complete_command_name,
}


def complete_choices(
    choices: Iterable[object],
    text: str,
) -> list[str]:
    """Auto-bound source for actions with ``choices=``."""
    names = sorted(str(c) for c in choices)
    return with_trailing_space(names, text)
