from __future__ import annotations

import argparse
from pathlib import Path

from contree_cli.cli import (
    agent,
    auth,
    build,
    cat,
    cd,
    cp,
    env,
    file,
    images,
    ls,
    operation,
    run,
    session,
    skill,
    tag,
    use,
)
from contree_cli.client import CLI_USER_AGENT
from contree_cli.config import CONFIG_FILE
from contree_cli.output import FORMATTERS
from contree_cli.shell import setup_parser as shell_setup_parser
from contree_cli.types import (
    COMMAND_REGISTRY,
    FLAGS,
    ArgumentsFormatter,
    SetupFn,
    get_command_docs,
)

EPILOG = """\
examples:
  contree use tag:ubuntu:latest       set session image
  eval $(contree use tag:ubuntu:latest)  set + export env var
  contree run -- uname -a            run command in session image
  contree run --shell -- 'echo hi'   shell mode
  contree run --file ./app.py:/app.py --disposable -- python /app.py
  contree run --file ./src:/app/src -- make -C /app/src
  contree images --prefix=ubuntu
  contree ps -q
  contree op ls                       same as `contree ps`
  contree op show UUID1 UUID2         multi-UUID show
  contree op cancel UUID1 UUID2       multi-UUID cancel (or --all)
  contree show OPERATION_UUID
  contree tag IMAGE_UUID latest
  contree ls /etc                    list files in session image
  contree cat /etc/os-release        show file from session image
  contree auth                       save token (secure prompt)
  contree auth switch staging
  contree man                        user manual
  contree agent                      coding-agent manual

for users:
  contree man

for coding agents (required bootstrap):
  1) read: contree agent
  2) inspect command syntax: contree <command> --help
  3) only then execute task commands

before running tasks:
  ensure auth exists; if missing/invalid, ask user to run `contree auth`

high-signal read-only commands:
  contree images | ps | show UUID | ls [PATH] | cat PATH | session | session show

mutating commands (change remote or local session state):
  contree use IMAGE | run -- CMD | file edit PATH | file cp SRC DEST
  contree tag UUID TAG | kill UUID | cd PATH | session checkout BRANCH

environment variables:
  CONTREE_PROFILE          Active config profile (selects which profile to use)
  CONTREE_SESSION          Explicit session name (for multi-terminal workflows).
                           If unset, contree auto-generates <cwd>+<8hex> (derived
                           from profile+ppid+tty); export your own for stable
                           reuse. You can also pass -S/--session instead.
  CONTREE_SESSION_DB       Path to session SQLite database
  CONTREE_NO_UPDATE_CHECK  Set to any value to disable PyPI update checks

registration-time fallbacks (only read by `contree auth`, not at runtime):
  CONTREE_TOKEN / NEBIUS_API_KEY        Token used when --token is omitted
  CONTREE_URL                           URL used when --url is omitted
  CONTREE_PROJECT / NEBIUS_AI_PROJECT   Project ID used when --project is omitted
"""

DESCRIPTION = """\
ConTree CLI - command-line client for the ConTree sandbox platform.

Run sandboxes, manage images, inspect filesystems, and track operations
through the ConTree REST API.

Authentication:
  Bearer token + project ID. Default API URL:
    https://api.tokenfactory.nebius.com/sandboxes/

  Use `contree auth --help` to configure persistent credentials.

Coding-agent bootstrap (important):
  Agents should read `contree agent` before executing task commands.
"""

parser = argparse.ArgumentParser(
    description=DESCRIPTION,
    epilog=EPILOG,
    formatter_class=ArgumentsFormatter,
)
parser.add_argument(
    *FLAGS["version"],
    action="version",
    version=CLI_USER_AGENT,
)
parser.add_argument(
    *FLAGS["profile"],
    default=None,
    help="Use this profile for the current command",
)
parser.add_argument(
    *FLAGS["token"],
    default=None,
    help="API token (overrides profile for this invocation)",
)


def _strip_trailing_slashes(value: str) -> str:
    return value.rstrip("/")


parser.add_argument(
    *FLAGS["url"],
    default=None,
    type=_strip_trailing_slashes,
    help="API base URL (overrides profile for this invocation)",
)
parser.add_argument(
    *FLAGS["project"],
    default=None,
    help="Project ID (overrides profile for this invocation)",
)
parser.add_argument(
    *FLAGS["config"],
    type=Path,
    default=CONFIG_FILE,
    dest="config_path",
    help="Config file path",
)
parser.add_argument(
    *FLAGS["log_level"],
    default="info",
    choices=("debug", "info", "warning", "error", "critical"),
    help="Logging level",
)
parser.add_argument(
    *FLAGS["format"],
    default="default",
    choices=sorted(FORMATTERS),
    dest="output_format",
    help="Output format",
)
parser.add_argument(
    *FLAGS["session"],
    dest="session_key",
    default=None,
    help="Session key override (alternative to CONTREE_SESSION)",
)
subparsers = parser.add_subparsers(dest="command", required=True)


AGENT_HELP_NOTE = """\
agent note:
  Before using this command in an automated workflow, read:
    contree agent
"""


def with_agent_note(epilog: str | None, command_name: str) -> str | None:
    if command_name in {"man", "agent"}:
        return epilog
    if epilog:
        return f"{epilog.rstrip()}\n\n{AGENT_HELP_NOTE.rstrip()}"
    return AGENT_HELP_NOTE


def register(
    name: str,
    help: str,
    setup_fn: SetupFn,
    aliases: list[str] | None = None,
) -> None:
    aliases_list = aliases or []
    COMMAND_REGISTRY.append((name, help, setup_fn, aliases_list))
    description, epilog = get_command_docs(setup_fn)
    p = subparsers.add_parser(
        name,
        help=help,
        aliases=aliases_list,
        description=description,
        epilog=with_agent_note(epilog, name),
        formatter_class=ArgumentsFormatter,
    )
    handler, loader = setup_fn(p)
    p.set_defaults(handler=handler, load_args=loader)


register("use", "Set or show current session image", use.setup_parser, aliases=["ci"])
register("run", "Spawn a sandbox instance", run.setup_parser, aliases=["r"])
register("build", "Build image from Dockerfile", build.setup_parser, aliases=["bd"])
register("images", "List and import images", images.setup_parser, aliases=["i", "img"])
register("tag", "Tag an image", tag.setup_parser, aliases=["t"])
register(
    "ps",
    "List operations (alias for `operation ls`)",
    operation.setup_list_parser,
)
register(
    "kill",
    "Cancel operations (alias for `operation cancel`)",
    operation.setup_cancel_parser,
)
register(
    "show",
    "Show operation result (alias for `operation show`)",
    operation.setup_show_parser,
)
register(
    "operation",
    "Manage operations (list/show/cancel)",
    operation.setup_parser,
    aliases=["op"],
)
register("ls", "List files in image", ls.setup_parser)
register("cat", "Show file content from image", cat.setup_parser)
register("cp", "Copy file from image to local path", cp.setup_parser)
register("file", "Manage files in session image", file.setup_parser, aliases=["f"])
register(
    "session",
    "Manage session branches and history",
    session.setup_parser,
    aliases=["s"],
)
register("auth", "Configure authentication", auth.setup_parser)
register("skill", "Manage agent skills", skill.setup_parser)
register("cd", "Change working directory", cd.setup_parser)
register("env", "Manage session environment variables", env.setup_parser)
register("agent", "Show manual", agent.setup_parser, aliases=["man"])
register("shell", "Interactive shell mode", shell_setup_parser, aliases=["sh"])
