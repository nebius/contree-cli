from __future__ import annotations

import base64
import os
import time

from contree_client.models import ClosableStreamRepr

from contree_cli.pty_utils import StdInReader

# stop() must return well inside STOP_TIMEOUT even under the pathological
# cases below; bounded but generous so CI jitter doesn't make this flaky.
STOP_BOUND = StdInReader.STOP_TIMEOUT + 1.0


def read_all(reader: StdInReader) -> list:
    items = []
    while True:
        item = reader.queue.get(timeout=5)
        items.append(item)
        if item.close:
            break
    return items


def content_of(items: list) -> bytes:
    return b"".join(
        i.value.encode() if i.encoding == "ascii" else base64.b64decode(i.value)
        for i in items
        if not i.close
    )


class TestNormalReadThenEOF:
    def test_content_captured_and_closed(self):
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"hello\n")
        os.close(write_fd)
        with StdInReader(fd=read_fd) as reader:
            items = read_all(reader)
        assert content_of(items) == b"hello\n"
        assert items[-1].close

    def test_empty_pipe_closes_immediately(self):
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        with StdInReader(fd=read_fd) as reader:
            items = read_all(reader)
        assert len(items) == 1
        assert items[0].close


class TestStopDoesNotHang:
    def test_full_queue(self):
        """stop() must return even if the reader is blocked publishing
        to a full queue (regression: queue.put() can't be interrupted by
        the wake signal that unblocks a blocked read). The queue is
        pre-filled directly so this is deterministic -- it doesn't rely
        on pipe buffering/timing to actually produce backpressure."""
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"hello\n")
        os.close(write_fd)
        reader = StdInReader(maxsize=1, fd=read_fd)
        reader.queue.put(ClosableStreamRepr(value="x", encoding="ascii", close=False))
        reader.start()
        start = time.monotonic()
        reader.stop()
        elapsed = time.monotonic() - start
        assert elapsed < STOP_BOUND
        assert not reader.is_alive()

    def test_open_producer_no_eof(self):
        """stop() must return even when the write end is still open and
        no data has ever arrived. A blocked read has no cross-thread
        interrupt on Windows, so the underlying thread may still be
        stuck in it afterward -- stop() only guarantees queue waiters
        unblock, not that the OS-level read itself has exited."""
        read_fd, write_fd = os.pipe()
        try:
            reader = StdInReader(fd=read_fd)
            reader.start()
            start = time.monotonic()
            reader.stop()
            elapsed = time.monotonic() - start
        finally:
            os.close(write_fd)
        assert elapsed < STOP_BOUND
        item = reader.queue.get(timeout=1)
        assert item.close

    def test_invalid_fd(self):
        reader = StdInReader(fd=-1)
        reader.start()
        start = time.monotonic()
        reader.stop()
        elapsed = time.monotonic() - start
        assert elapsed < STOP_BOUND
        assert not reader.is_alive()
