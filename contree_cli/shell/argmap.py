"""Central registry mapping argparse actions to completion sources.

Keys are ``(command_path, dest)`` pairs where ``command_path`` is the
tuple of canonical subcommand names visited during the parser walk
(aliases such as ``op`` for ``operation`` or ``ls`` for ``list`` are
normalised via :func:`contree_cli.shell.argspec.canonical_name`). Empty
tuple is the root parser. The source name on the right must exist in
``SOURCES`` (see ``sources.py``).

Adding a new completable argument is a one-line entry below.
"""

from __future__ import annotations

ArgKey = tuple[tuple[str, ...], str]


# Mapping of (command_path, action.dest) to a registered source name.
# Empty tuple is the root parser (top-level flags like --profile).
ARG_SOURCES: dict[ArgKey, str] = {
    # Top-level flags on the root parser.
    ((), "profile"): "profile",
    # use / tag -- image references.
    (("use",), "image"): "image",
    (("tag",), "args"): "image",
    # show / kill / wait -- operation UUIDs.
    (("show",), "uuid"): "operation",
    (("kill",), "uuids"): "operation",
    (("session", "wait"), "op_ids"): "operation",
    (("operation", "show"), "uuids"): "operation",
    (("operation", "cancel"), "uuids"): "operation",
    # Session keys.
    (("session", "use"), "session_name"): "session",
    (("session", "show"), "session_name"): "session",
    (("session", "delete"), "keys"): "session",
    # Branch names.
    (("session", "branch"), "branch_name"): "branch",
    (("session", "branch"), "from_branch"): "branch",
    (("session", "checkout"), "checkout_branch"): "branch",
    # Profile names (auth subtree).
    (("auth",), "profile"): "profile",
    (("auth", "switch"), "profile_name"): "profile",
    (("auth", "remove"), "profile_name"): "profile",
    # Sandbox filesystem.
    (("ls",), "path"): "sandbox-path",
    (("cat",), "path"): "sandbox-path",
    (("cp",), "path"): "sandbox-path",
    (("cp",), "dest"): "host-path",
    (("cd",), "path"): "sandbox-dir",
    (("file", "edit"), "path"): "sandbox-path",
    (("file", "edit"), "editor"): "editor",
    (("file", "cp"), "src"): "host-path",
    (("file", "cp"), "dest"): "sandbox-path",
    # run -- exec inside the sandbox.
    (("run",), "command_args"): "sandbox-path",
    (("run",), "cwd"): "sandbox-dir",
    (("run",), "file"): "mapped-file",
    (("run",), "use"): "image",
    # build -- Dockerfile build context and file paths on the host.
    (("build",), "context"): "host-path",
    (("build",), "dockerfile"): "host-path",
    # env / skill.
    (("env",), "vars"): "env-key",
    (("skill", "install"), "specs"): "skill-spec",
    (("skill", "remove"), "specs"): "skill-spec",
    (("skill", "upgrade"), "specs"): "skill-spec",
}


def lookup(command_path: tuple[str, ...], dest: str) -> str | None:
    """Return the source name registered for ``(command_path, dest)``."""
    return ARG_SOURCES.get((command_path, dest))
