"""``RUN ...`` - execute a command and capture the resulting image."""

from __future__ import annotations

import json
import logging
import shlex
import time
from dataclasses import dataclass, field
from typing import ClassVar

from contree_cli.client import decode_stream

from .context import BuildContext
from .keyword import DockerKeyword, parse_command_form

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = frozenset({"SUCCESS", "FAILED", "CANCELLED"})


@dataclass(frozen=True)
class RunKeyword(DockerKeyword):
    NAME: ClassVar[str] = "RUN"
    parts: tuple[str, ...] = field(default_factory=tuple)
    shell_form: bool = True

    @classmethod
    def parse(cls, args_text: str) -> RunKeyword:
        raw = args_text.strip()
        if not raw:
            raise ValueError("RUN requires a command")
        parts, shell_form = parse_command_form(raw)
        return cls(parts=tuple(parts), shell_form=shell_form)

    def serialize(self) -> str:
        if self.shell_form:
            return f"RUN {self.parts[0]}"
        return f"RUN {json.dumps(list(self.parts))}"

    def execute(self, ctx: BuildContext) -> None:
        sub_parts = tuple(ctx.substitute(p) for p in self.parts)
        contribution = (
            f"RUN shell={self.shell_form} parts={json.dumps(list(sub_parts))}"
        )
        chain = ctx.chain(contribution)
        branch_name = f"layer:{BuildContext.short_hash(chain)}"

        cached = ctx.try_cache_hit(branch_name)
        if cached is not None:
            ctx.parent_hash = chain
            ctx.pending.clear()
            return

        new_image, op_uuid = self.spawn(ctx, sub_parts)
        ctx.commit_layer(
            branch_name,
            new_image,
            kind="run",
            title=display_title(sub_parts, self.shell_form),
            operation_uuid=op_uuid,
        )
        ctx.parent_hash = chain
        ctx.pending.clear()

    def spawn(self, ctx: BuildContext, parts: tuple[str, ...]) -> tuple[str, str]:
        command, args, shell = build_command(parts, self.shell_form, ctx.user)
        payload: dict[str, object] = {
            "image": ctx.last_image,
            "command": command,
            "shell": shell,
            "disposable": False,
            "hostname": "linuxkit",
            "truncate_output_at": 65536,
        }
        if args:
            payload["args"] = args
        if ctx.timeout:
            payload["timeout"] = ctx.timeout
        if ctx.workdir and ctx.workdir != "/":
            payload["cwd"] = ctx.workdir
        if ctx.env:
            payload["env"] = dict(ctx.env)
        if ctx.pending:
            payload["files"] = ctx.pending_files_payload()

        resp = ctx.client.post_json("/v1/instances", payload)
        op = json.loads(resp.read())
        op_uuid: str = op["uuid"]
        logger.info(
            "RUN spawned op=%s: %s", op_uuid, display_title(parts, self.shell_form)
        )

        op = poll(ctx, op_uuid)
        check_success(op, parts, self.shell_form)
        result = op.get("result") or {}
        assert isinstance(result, dict)
        new_image = result.get("image")
        if not new_image:
            raise RuntimeError("RUN succeeded but no image was produced")
        log_streams(op)
        return str(new_image), op_uuid


def poll(ctx: BuildContext, op_uuid: str) -> dict[str, object]:
    delay = 0.5
    while True:
        time.sleep(delay)
        resp = ctx.client.get(f"/v1/operations/{op_uuid}")
        op = json.loads(resp.read())
        if op["status"] in TERMINAL_STATUSES:
            return op  # type: ignore[no-any-return]
        if delay < 5:
            delay += delay


def check_success(
    op: dict[str, object], parts: tuple[str, ...], shell_form: bool
) -> None:
    metadata = op.get("metadata") or {}
    assert isinstance(metadata, dict)
    instance_result = metadata.get("result") or {}
    assert isinstance(instance_result, dict)
    state = instance_result.get("state") or {}
    assert isinstance(state, dict)
    exit_code = state.get("exit_code")
    title = display_title(parts, shell_form)
    if op["status"] != "SUCCESS":
        stderr = decode_stream(instance_result.get("stderr"))
        raise RuntimeError(
            f"RUN {title!r} ended with {op['status']}: {op.get('error') or stderr}"
        )
    if isinstance(exit_code, int) and exit_code != 0:
        stdout = decode_stream(instance_result.get("stdout"))
        stderr = decode_stream(instance_result.get("stderr"))
        raise RuntimeError(
            f"RUN {title!r} exited with code {exit_code}\n"
            f"stdout: {stdout}\nstderr: {stderr}"
        )


def log_streams(op: dict[str, object]) -> None:
    metadata = op.get("metadata") or {}
    assert isinstance(metadata, dict)
    instance_result = metadata.get("result") or {}
    assert isinstance(instance_result, dict)
    stdout = decode_stream(instance_result.get("stdout"))
    stderr = decode_stream(instance_result.get("stderr"))
    if stdout:
        logger.info("stdout:\n%s", stdout)
    if stderr:
        logger.info("stderr:\n%s", stderr)


def build_command(
    parts: tuple[str, ...],
    shell_form: bool,
    user: str,
) -> tuple[str, list[str], bool]:
    """Map parsed RUN parts plus optional USER into an API payload triple."""
    if shell_form:
        expr = parts[0]
        if user:
            wrapped = wrap_with_user(expr, user)
            return wrapped, [], True
        return expr, [], True

    cmd = parts[0]
    args = list(parts[1:])
    if user:
        joined = shlex.join([cmd, *args])
        wrapped = wrap_with_user(joined, user)
        return wrapped, [], True
    return cmd, args, False


def wrap_with_user(expr: str, user: str) -> str:
    return f"su -s /bin/sh -c {shlex.quote(expr)} {shlex.quote(user)}"


def display_title(parts: tuple[str, ...], shell_form: bool) -> str:
    if shell_form:
        return f"RUN {parts[0]}"[:200]
    return f"RUN {json.dumps(list(parts))}"[:200]
