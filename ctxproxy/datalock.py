# Author: Maurizio Verde — LastReload
"""Non-blocking lifetime lock shared by the server and offline maintenance.

POSIX locks the data directory itself (no file creation, even on a first dry run).
Windows locks a persistent sibling file keyed by the canonical data directory.
Never unlink a lock file: another process may already have its inode open.
"""
from __future__ import annotations

import hashlib
import os

from . import compat


class DataLock:
    def __init__(self, data):
        self.data = os.path.normcase(os.path.realpath(data))
        self.fd = None

    def __enter__(self):
        if not os.path.isdir(self.data):
            raise ValueError("data directory does not exist: %s" % self.data)
        if compat.WINDOWS:  # pragma: no cover - Windows CI
            import msvcrt
            digest = hashlib.sha256(self.data.encode("utf-8")).hexdigest()
            path = os.path.join(os.path.dirname(self.data), ".mnemonic-proxy-%s.lock" % digest)
            self.fd = os.open(path, os.O_RDWR | os.O_CREAT | compat.O_BINARY, 0o600)
            try:
                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                os.close(self.fd)
                self.fd = None
                raise ValueError("data directory is in use by a proxy or maintenance command: %s" % self.data) from None
        else:
            import fcntl
            self.fd = os.open(self.data, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(self.fd)
                self.fd = None
                raise ValueError("data directory is in use by a proxy or maintenance command: %s" % self.data) from None
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            compat.unlock_fd(self.fd)
            os.close(self.fd)
            self.fd = None
