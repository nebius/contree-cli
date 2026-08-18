"""Stoppable background stdin reader.

Never mutates the fd's blocking mode: a shell's fd 0/1/2 commonly share
one open file description, so O_NONBLOCK on stdin would leak into
stdout/stderr too.
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
from abc import ABC, abstractmethod
from types import TracebackType

from contree_client.models import ClosableStreamRepr, StreamRepr

log = logging.getLogger(__name__)


class PlatformStdInReader(threading.Thread, ABC):
    """Publishes ClosableStreamRepr items to its queue; the final item
    always has close=True."""

    def __init__(self, maxsize: int = 16, fd: int = 0) -> None:
        super().__init__(daemon=True)
        self.queue: queue.Queue[ClosableStreamRepr] = queue.Queue(maxsize=maxsize)
        self.fd = fd

    def __enter__(self) -> PlatformStdInReader:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.stop()

    @abstractmethod
    def stop(self) -> None:
        """Block until the read loop has actually exited."""

    @abstractmethod
    def run(self) -> None: ...


if sys.platform == "win32":
    import msvcrt

    class Win32StdInReader(PlatformStdInReader):
        """Polls msvcrt: a blocked console read can't be interrupted
        from another thread, so stop_event is checked between polls."""

        POLL_INTERVAL = 0.05

        def __init__(self, maxsize: int = 16, fd: int = 0) -> None:
            super().__init__(maxsize=maxsize, fd=fd)
            self.stop_event = threading.Event()

        def stop(self) -> None:
            self.stop_event.set()
            self.join()

        def run(self) -> None:
            try:
                while not self.stop_event.is_set():
                    if msvcrt.kbhit():
                        ch = msvcrt.getwch()
                        sr = StreamRepr.from_bytes(ch.encode("utf-8", errors="replace"))
                        self.queue.put(
                            ClosableStreamRepr(
                                value=sr.value, encoding=sr.encoding, close=False
                            )
                        )
                    else:
                        self.stop_event.wait(self.POLL_INTERVAL)
            except OSError:
                pass
            self.queue.put(ClosableStreamRepr(value="", encoding="ascii", close=True))

    StdInReader = Win32StdInReader

else:
    import select

    class POSIXStdInReader(PlatformStdInReader):
        """Interruptible via a self-pipe: stop() writes to it, waking
        the select() blocked in run() without touching fd's own mode."""

        PIPE_READ_SIZE = 65536
        CHUNK_SIZE = 2**19  # 512KiB; base64 overhead keeps the wire payload near 1MB

        def __init__(self, maxsize: int = 16, fd: int = 0) -> None:
            super().__init__(maxsize=maxsize, fd=fd)
            self.wake_r, self.wake_w = os.pipe()

        def stop(self) -> None:
            os.write(self.wake_w, b"x")
            self.join()
            os.close(self.wake_r)
            os.close(self.wake_w)

        def run(self) -> None:
            try:
                os.read(self.fd, 0)
            except (OSError, TypeError):
                self.queue.put(
                    ClosableStreamRepr(value="", encoding="ascii", close=True)
                )
                return
            while True:
                buf = bytearray()
                while len(buf) < self.CHUNK_SIZE:
                    rlist, _, _ = select.select([self.fd, self.wake_r], [], [])
                    if self.wake_r in rlist:
                        break
                    try:
                        piece = os.read(
                            self.fd,
                            min(self.PIPE_READ_SIZE, self.CHUNK_SIZE - len(buf)),
                        )
                    except (OSError, TypeError):
                        piece = b""
                    if not piece:
                        break
                    buf += piece
                    if len(piece) < self.PIPE_READ_SIZE:
                        break
                if not buf:
                    break
                sr = StreamRepr.from_bytes(bytes(buf))
                self.queue.put(
                    ClosableStreamRepr(
                        value=sr.value, encoding=sr.encoding, close=False
                    )
                )
            self.queue.put(ClosableStreamRepr(value="", encoding="ascii", close=True))

    StdInReader = POSIXStdInReader
