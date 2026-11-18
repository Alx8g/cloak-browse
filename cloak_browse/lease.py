from __future__ import annotations

import importlib
import os
import time
from pathlib import Path
from types import TracebackType
from typing import BinaryIO

from .paths import ensure_private_dir


class RunLeaseTimeout(RuntimeError):
    pass


class AdvisoryRunLease:
    def __init__(self, path: Path, timeout: float) -> None:
        self.path = path
        self.timeout = timeout
        self.file: BinaryIO | None = None

    def __enter__(self) -> AdvisoryRunLease:
        ensure_private_dir(self.path.parent)
        self.file = self.path.open("a+b")
        self.file.seek(0, os.SEEK_END)
        if self.file.tell() == 0:
            self.file.write(b"\0")
            self.file.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            self.file.seek(0)
            try:
                if os.name == "nt":
                    msvcrt = importlib.import_module("msvcrt")

                    msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self.file.close()
                    self.file = None
                    raise RunLeaseTimeout(
                        f"another run holds {self.path}; waited {self.timeout:g}s"
                    ) from None
                time.sleep(0.05)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        assert self.file is not None
        try:
            self.file.seek(0)
            if os.name == "nt":
                msvcrt = importlib.import_module("msvcrt")

                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()
            self.file = None
