from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import asdict
from typing import Any, TextIO
from uuid import uuid4

from .paths import AppPaths, ensure_private_dir
from .runtime import StartOptions
from .session import SessionStore


def start_background(
    options: StartOptions,
    paths: AppPaths,
    timeout: float,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    """Launch an independent owner and wait for its verified running record."""
    store = SessionStore(paths)
    existing = store.load()
    if existing.error or existing.record is not None:
        print(
            "error: session state already exists; inspect status before starting",
            file=stderr,
        )
        return 1
    ensure_private_dir(paths.cache_dir)
    command = [sys.executable, "-u", "-m", "cloak_browse.cli", "start"]
    for name, value in asdict(options).items():
        flag = "--" + name.replace("_", "-")
        if value is True:
            command.append(flag)
        elif value is not None and value is not False:
            command.extend([flag, str(value)])
    launch_id = str(uuid4())
    environment = dict(os.environ)
    environment["CLOAK_BROWSE_LAUNCH_ID"] = launch_id
    environment["CLOAK_BROWSE_CACHE_DIR"] = str(paths.cache_dir)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True
    log_path = paths.cache_dir / f"start-{time.time_ns()}.log"
    try:
        descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as log:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                env=environment,
                close_fds=True,
                **kwargs,
            )
    except OSError as exc:
        print(f"error: cannot launch browser owner: {exc}", file=stderr)
        return 1
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            print(
                f"error: browser owner exited {exit_code}; inspect {log_path}",
                file=stderr,
            )
            return 1
        read = store.load()
        session = read.record
        if (
            session is not None
            and session.session_id == launch_id
            and session.phase == "running"
        ):
            print(f"CloakBrowse ready. Startup log: {log_path}", file=stdout)
            return 0
        time.sleep(0.1)
    read = store.load()
    if read.record is not None and read.record.session_id == launch_id:
        store.request_stop(read.record.session_id, "startup-timeout")
    print(
        f"error: startup did not become ready within {timeout:g}s; "
        f"inspect {log_path} and run status. The owner may still be shutting down.",
        file=stderr,
    )
    return 124
