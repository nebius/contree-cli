import dataclasses
import hashlib
import logging
import os
import pathlib

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True, slots=True)
class MappedFile:
    host_path: str
    instance_path: str
    uid: int
    gid: int
    mode: int
    uid_explicit: bool = False
    gid_explicit: bool = False
    mode_explicit: bool = False

    def sha256(self) -> str:
        """Return hex SHA256 digest of the host file (streamed)."""
        h = hashlib.sha256()
        with open(self.host_path, "rb") as fh:
            while chunk := fh.read(256 * 1024):
                h.update(chunk)
        return h.hexdigest()

    @classmethod
    def parse(cls, spec: str) -> "MappedFile":
        drive = pathlib.PurePath(spec).drive
        parts = spec[len(drive) :].split(":")
        if not parts or not (drive + parts[0]):
            raise ValueError(f"invalid file spec {spec!r}: host_path is required")

        host_path = drive + parts[0]
        instance_path: str | None = None
        uid: int | None = None
        gid: int | None = None
        mode: int | None = None
        uid_explicit = False
        gid_explicit = False
        mode_explicit = False

        for part in parts[1:]:
            if part.startswith("/"):
                if instance_path is not None:
                    raise ValueError(
                        f"invalid file spec {spec!r}: duplicate instance path"
                    )
                instance_path = part
            elif part.startswith("u"):
                uid = _resolve_uid(part[1:])
                uid_explicit = True
            elif part.startswith("g"):
                gid = _resolve_gid(part[1:])
                gid_explicit = True
            elif part.startswith("m"):
                mode = _parse_mode(part[1:])
                mode_explicit = True
            else:
                raise ValueError(
                    f"invalid file spec {spec!r}: "
                    f"unknown field {part!r} "
                    f"(expected /path, u<uid>, g<gid>, or m<mode>)"
                )

        needs_stat = instance_path is None or uid is None or gid is None or mode is None
        if needs_stat:
            try:
                st = os.stat(host_path)
            except OSError as exc:
                raise ValueError(f"cannot stat host file {host_path!r}: {exc}") from exc
            if instance_path is None:
                instance_path = host_path
            if uid is None:
                uid = st.st_uid
            if gid is None:
                gid = st.st_gid
            if mode is None:
                mode = st.st_mode & 0o7777

        assert instance_path is not None
        assert uid is not None
        assert gid is not None
        assert mode is not None
        return cls(
            host_path=host_path,
            instance_path=instance_path,
            uid=uid,
            gid=gid,
            mode=mode,
            uid_explicit=uid_explicit,
            gid_explicit=gid_explicit,
            mode_explicit=mode_explicit,
        )


try:
    import grp
    import pwd

    def _resolve_uid(value: str) -> int:
        try:
            return int(value)
        except ValueError:
            pass
        try:
            return pwd.getpwnam(value).pw_uid
        except KeyError:
            logger.warning("Unknown user %r, using uid 0", value)
            return 0

    def _resolve_gid(value: str) -> int:
        try:
            return int(value)
        except ValueError:
            pass
        try:
            return grp.getgrnam(value).gr_gid
        except KeyError:
            logger.warning("Unknown group %r, using gid 0", value)
            return 0

    MAPPING_RULES = (
        "Attach file or directory (repeatable, dirs recurse). "
        "Format: host[:inst_path][:uUID][:gGID][:mMODE]. "
        "Tagged options (u/g/m) in any order; "
        "uid/gid resolved locally from pwd/grp; "
        "defaults from host stat."
    )

except ImportError:

    def _resolve_uid(value: str) -> int:
        try:
            return int(value)
        except ValueError:
            logger.warning(
                "Cannot resolve user %r (no pwd module), using uid 0",
                value,
            )
            return 0

    def _resolve_gid(value: str) -> int:
        try:
            return int(value)
        except ValueError:
            logger.warning(
                "Cannot resolve group %r (no grp module), using gid 0",
                value,
            )
            return 0

    MAPPING_RULES = (
        "Attach file or directory (repeatable, dirs recurse). "
        "Format: host[:inst_path][:uUID][:gGID][:mMODE]. "
        "Tagged options (u/g/m) in any order; "
        "only numeric uid/gid supported on this platform; "
        "uid/gid default to 0, mode from host stat."
    )


def _parse_mode(value: str) -> int:
    try:
        return int(value, 8)
    except ValueError:
        return 0
