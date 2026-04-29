import dataclasses
import grp
import hashlib
import os
import pwd


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
        parts = spec.split(":")
        if not parts or not parts[0]:
            raise ValueError(f"invalid file spec {spec!r}: host_path is required")

        host_path = parts[0]
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


def _resolve_uid(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return pwd.getpwnam(value).pw_uid
    except KeyError:
        return 0


def _resolve_gid(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return grp.getgrnam(value).gr_gid
    except KeyError:
        return 0


def _parse_mode(value: str) -> int:
    try:
        return int(value, 8)
    except ValueError:
        return 0
