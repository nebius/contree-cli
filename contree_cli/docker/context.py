"""Mutable state shared across one ``contree build`` invocation."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from dataclasses import dataclass, field

from contree_cli.client import ContreeClient
from contree_cli.session import SessionStore

from .local_context import LocalContext

logger = logging.getLogger(__name__)

BUILD_TIMEOUT_DEFAULT = 600


@dataclass
class PendingFile:
    instance_path: str
    file_uuid: str
    sha256: str
    uid: int
    gid: int
    mode: str  # octal like "0644"


@dataclass
class BuildContext:
    client: ContreeClient
    store: SessionStore
    local: LocalContext
    build_args: dict[str, str] = field(default_factory=dict)
    declared_args: set[str] = field(default_factory=set)
    arg_defaults: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    workdir: str = "/"
    user: str = ""
    parent_hash: str = ""
    pending: list[PendingFile] = field(default_factory=list)
    no_cache: bool = False
    timeout: int = BUILD_TIMEOUT_DEFAULT
    last_image: str = ""
    last_op_uuid: str = ""

    def arg_values(self) -> dict[str, str]:
        """Effective values for every declared ARG (build-arg overrides default)."""
        return {
            name: self.build_args.get(name, self.arg_defaults.get(name, ""))
            for name in self.declared_args
        }

    def substitute(self, text: str) -> str:
        from .keyword import substitute

        merged = {**self.arg_values(), **self.env}
        return substitute(text, merged)

    def state_repr(self) -> str:
        return json.dumps(
            {
                "workdir": self.workdir,
                "user": self.user,
                "env": sorted(self.env.items()),
                "args": sorted(self.arg_values().items()),
            },
            sort_keys=True,
        )

    def pending_repr(self) -> str:
        return json.dumps(
            [
                {
                    "path": p.instance_path,
                    "sha": p.sha256,
                    "uid": p.uid,
                    "gid": p.gid,
                    "mode": p.mode,
                }
                for p in self.pending
            ],
            sort_keys=True,
        )

    def chain(self, contribution: str) -> str:
        h = hashlib.sha256()
        h.update(self.parent_hash.encode())
        h.update(b"\x00")
        h.update(self.state_repr().encode())
        h.update(b"\x00")
        h.update(contribution.encode())
        h.update(b"\x00")
        h.update(self.pending_repr().encode())
        return h.hexdigest()

    @staticmethod
    def short_hash(full: str) -> str:
        return full[:16]

    def pending_files_payload(self) -> dict[str, object]:
        return {
            p.instance_path: {
                "uuid": p.file_uuid,
                "uid": p.uid,
                "gid": p.gid,
                "mode": p.mode,
            }
            for p in self.pending
        }

    def try_cache_hit(self, branch_name: str) -> str | None:
        """Return cached image_uuid if ``branch_name`` exists and cache is enabled."""
        if self.no_cache:
            return None
        try:
            tip = self.store.branch_tip(branch_name)
        except ValueError:
            return None
        self.store.switch_branch(branch_name)
        self.last_image = tip.image_uuid
        logger.info("layer cache hit: %s -> %s", branch_name, tip.image_uuid)
        return tip.image_uuid

    def commit_layer(
        self,
        branch_name: str,
        image_uuid: str,
        *,
        kind: str,
        title: str,
        operation_uuid: str = "",
    ) -> None:
        """Materialize a fresh layer branch pointing at ``image_uuid``.

        Forks from the currently active branch (the parent layer). When the
        session is brand-new and has no active branch, the first ``set_image``
        bootstraps the implicit ``main`` branch before we fork.
        """
        with contextlib.suppress(ValueError):
            self.store.delete_branch(branch_name)

        if self.store.session is None:
            self.store.set_image(
                image_uuid,
                kind=kind,
                title=title,
                operation_uuid=operation_uuid,
            )
            self.store.create_branch(branch_name)
            self.store.switch_branch(branch_name)
        else:
            self.store.create_branch(branch_name)
            self.store.switch_branch(branch_name)
            self.store.set_image(
                image_uuid,
                kind=kind,
                title=title,
                operation_uuid=operation_uuid,
            )

        self.last_image = image_uuid
        self.last_op_uuid = operation_uuid
