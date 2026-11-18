from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import SessionRecord
from .paths import AppPaths, ensure_private_dir
from .process_identity import process_start_token


@dataclass(frozen=True)
class HarnessState:
    alive: bool
    error: str | None


@dataclass(frozen=True)
class HarnessResult:
    ok: bool
    error: str | None


class HarnessManager:
    def __init__(
        self,
        paths: AppPaths,
        runner: Callable[..., Any] = subprocess.run,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.paths = paths
        self.runner = runner
        self.environment = dict(os.environ if environment is None else environment)

    def alive(self, session: SessionRecord) -> HarnessState:
        script = (
            "import os\n"
            "from browser_harness.admin import daemon_alive\n"
            "raise SystemExit(0 if daemon_alive(os.environ['BU_NAME']) else 3)\n"
        )
        try:
            result = self._run(script, session, timeout=5, capture=True)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return HarnessState(False, _brief_error(exc))
        if result.returncode == 0:
            return HarnessState(True, None)
        if result.returncode == 3:
            return HarnessState(False, None)
        return HarnessState(False, self._result_error(result, session))

    def start(self, session: SessionRecord, timeout: float = 90.0) -> HarnessResult:
        script = (
            "import os\n"
            "from cloak_browse.harness import _start_daemon\n"
            "_start_daemon(float(os.environ['CLOAK_BROWSE_HARNESS_WAIT']))\n"
        )
        extra = {"CLOAK_BROWSE_HARNESS_WAIT": str(timeout)}
        try:
            result = self._run(
                script,
                session,
                timeout=timeout + 10,
                capture=True,
                extra_environment=extra,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return HarnessResult(False, _brief_error(exc))
        if result.returncode == 0:
            state = self.alive(session)
            if state.alive:
                return HarnessResult(True, None)
            return HarnessResult(
                False, state.error or "daemon did not answer its IPC ping"
            )
        return HarnessResult(False, self._result_error(result, session))

    def stop(self, session: SessionRecord) -> HarnessResult:
        script = "from cloak_browse.harness import _stop_daemon\n_stop_daemon()\n"
        try:
            result = self._run(script, session, timeout=25, capture=True)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return HarnessResult(False, _brief_error(exc))
        if result.returncode == 0:
            return HarnessResult(True, None)
        return HarnessResult(False, self._result_error(result, session))

    def execute(
        self,
        session: SessionRecord,
        code: str,
        timeout: float | None,
    ) -> int:
        prepared = self._prepare_execution(session)
        if not prepared.ok:
            print(f"error: harness recovery refused: {prepared.error}", file=sys.stderr)
            return 1
        command = [sys.executable, "-m", "cloak_browse.worker"]
        environment = self._environment(session)
        environment["BH_REQUIRE_EXISTING_DAEMON"] = "1"
        try:
            result = self.runner(
                command,
                input=code,
                text=True,
                env=environment,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            recovered = self._prepare_execution(session, cancel_execution=True)
            if not recovered.ok:
                print(
                    f"error: harness recovery failed: {recovered.error}",
                    file=sys.stderr,
                )
            return 124
        return int(result.returncode)

    def _prepare_execution(
        self,
        session: SessionRecord,
        cancel_execution: bool = False,
        allow_recovery: bool = True,
    ) -> HarnessResult:
        script = (
            "from cloak_browse.harness import _prepare_daemon\n"
            f"_prepare_daemon(cancel_execution={cancel_execution!r})\n"
        )
        try:
            result = self._run(script, session, timeout=40, capture=True)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return HarnessResult(False, _brief_error(exc))
        if result.returncode != 0:
            return HarnessResult(False, self._result_error(result, session))
        if result.stdout.strip() == "restart":
            if not allow_recovery:
                return HarnessResult(False, "restarted daemon is not CDP-healthy")
            started = self.start(session)
            if not started.ok:
                return started
            return self._prepare_execution(session, allow_recovery=False)
        return HarnessResult(True, None)

    def cleanup(self, session: SessionRecord) -> None:
        harness_paths = self.paths.harness(session.session_id)
        for path in (
            harness_paths.runtime_dir,
            harness_paths.tmp_dir,
            harness_paths.config_dir,
        ):
            _remove_private_tree(path, session.session_id)

    def log_tail(self, session: SessionRecord, lines: int = 5) -> str | None:
        log_path = self.paths.harness(session.session_id).tmp_dir / "bu.log"
        try:
            content = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        values = [line for line in content.splitlines() if line.strip()]
        return " | ".join(values[-lines:])[:1000] or None

    def _run(
        self,
        script: str,
        session: SessionRecord,
        timeout: float,
        capture: bool,
        extra_environment: Mapping[str, str] | None = None,
    ) -> Any:
        environment = self._environment(session)
        if extra_environment:
            environment.update(extra_environment)
        options: dict[str, Any] = {
            "env": environment,
            "timeout": timeout,
            "check": False,
            "text": True,
        }
        if capture:
            options["stdout"] = subprocess.PIPE
            options["stderr"] = subprocess.PIPE
        return self.runner([sys.executable, "-c", script], **options)

    def _environment(self, session: SessionRecord) -> dict[str, str]:
        harness_paths = self.paths.harness(session.session_id)
        for path in (
            harness_paths.runtime_dir,
            harness_paths.tmp_dir,
            harness_paths.config_dir,
        ):
            ensure_private_dir(path)
        return {
            **self.environment,
            "BU_NAME": session.daemon_name,
            "BU_CDP_URL": session.cdp_url,
            "BH_RUNTIME_DIR": str(harness_paths.runtime_dir),
            "BH_TMP_DIR": str(harness_paths.tmp_dir),
            "BH_CONFIG_DIR": str(harness_paths.config_dir),
            "BH_UPDATE_CHECK": "0",
            "BH_TELEMETRY": "0",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }

    def _result_error(self, result: Any, session: SessionRecord) -> str:
        stderr = str(getattr(result, "stderr", "") or "").strip().splitlines()
        stdout = str(getattr(result, "stdout", "") or "").strip().splitlines()
        tail = self.log_tail(session)
        values = [line.strip() for line in stderr[-2:] + stdout[-1:] if line.strip()]
        if tail:
            values.append(tail)
        return " | ".join(values)[:1200] or f"process exited {result.returncode}"


def _start_daemon(timeout: float) -> None:
    from browser_harness import _ipc as ipc

    name = os.environ["BU_NAME"]
    if ipc.ping(name, timeout=1.0):
        raise RuntimeError("refusing to replace an existing daemon")
    with Path(ipc.log_path(name)).open("ab") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "browser_harness.daemon"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log,
            **ipc.spawn_kwargs(),
        )
    ready = False
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    f"harness process exited with code {process.returncode}"
                )
            if ipc.ping(name, timeout=1.0):
                _record_daemon_identity()
                ready = True
                return
            time.sleep(0.1)
        raise RuntimeError(f"harness startup timed out after {timeout:g}s")
    finally:
        if not ready:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            else:
                process.wait()


def _identity_path() -> Path:
    return Path(os.environ["BH_CONFIG_DIR"]) / "daemon-identity.json"


def _record_daemon_identity() -> None:
    from browser_harness import _ipc as ipc

    pid = ipc.identify(os.environ["BU_NAME"], timeout=2.0)
    started = process_start_token(pid) if type(pid) is int else None
    if started is None:
        raise RuntimeError("cannot establish daemon process identity")
    _identity_path().write_text(json.dumps([pid, started]), encoding="utf-8")


def _prepare_daemon(cancel_execution: bool = False) -> None:
    from browser_harness import _ipc as ipc

    name = os.environ["BU_NAME"]
    connection = None
    healthy = False
    try:
        connection, token = ipc.connect(name, timeout=2.0)
        response = ipc.request(connection, token, {"meta": "connection_status"})
        healthy = (
            isinstance(response, dict)
            and "error" not in response
            and isinstance(response.get("target_id"), str)
            and isinstance(response.get("page"), (dict, type(None)))
        )
    except (OSError, ValueError):
        pass
    finally:
        if connection is not None:
            connection.close()
    if healthy and not cancel_execution:
        return
    expected_pid, expected_started = json.loads(
        _identity_path().read_text(encoding="utf-8")
    )
    pid = ipc.identify(name, timeout=2.0)
    if (
        type(pid) is not int
        or pid != expected_pid
        or process_start_token(pid) != expected_started
    ):
        raise RuntimeError("daemon PID/start identity does not match this session")
    if cancel_execution:
        connection = None
        try:
            connection, token = ipc.connect(name, timeout=2.0)
            ipc.request(
                connection,
                token,
                {
                    "method": "Runtime.terminateExecution",
                    "params": {},
                    "session_id": None,
                },
            )
        except (OSError, ValueError):
            pass
        finally:
            if connection is not None:
                connection.close()
    _stop_daemon(force=True, expected_identity=(expected_pid, expected_started))
    print("restart")


def _stop_daemon(
    force: bool = False, expected_identity: tuple[int, str] | None = None
) -> None:
    from browser_harness import _ipc as ipc

    name = os.environ["BU_NAME"]
    daemon_pid = ipc.identify(name, timeout=2.0)
    if daemon_pid is None:
        if ipc.ping(name, timeout=1.0) or Path(ipc.pid_path(name)).exists():
            raise RuntimeError(
                f"daemon {name!r} is reachable or recorded but did not provide "
                "a verifiable process identity"
            )
        return
    daemon_started = process_start_token(daemon_pid)
    if daemon_started is None:
        raise RuntimeError(f"cannot establish process identity for daemon {name!r}")
    if (
        expected_identity is not None
        and (daemon_pid, daemon_started) != expected_identity
    ):
        raise RuntimeError("daemon identity changed before shutdown")

    connection = None
    try:
        connection, token = ipc.connect(name, timeout=3.0)
        try:
            response = ipc.request(connection, token, {"meta": "shutdown"})
        except (OSError, ValueError):
            if not force:
                raise
            response = {"ok": True}
    finally:
        if connection is not None:
            with contextlib.suppress(OSError):
                connection.close()
    if (
        not isinstance(response, dict)
        or response.get("ok") is not True
        or bool(response.get("error"))
    ):
        error = response.get("error") if isinstance(response, dict) else None
        raise RuntimeError(error or f"daemon {name!r} did not confirm shutdown")

    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        current_started = process_start_token(daemon_pid)
        if current_started is None:
            break
        if current_started != daemon_started:
            raise RuntimeError(
                f"daemon PID {daemon_pid} was reused while shutdown was in progress"
            )
        time.sleep(0.1)
    else:
        if not force or process_start_token(daemon_pid) != daemon_started:
            raise RuntimeError(f"daemon {name!r} did not exit after shutdown")
        os.kill(daemon_pid, signal.SIGTERM)
        deadline = time.monotonic() + 2.0
        while process_start_token(daemon_pid) == daemon_started:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"daemon {name!r} survived termination")
            time.sleep(0.1)

    replacement = ipc.identify(name, timeout=0.2)
    if replacement is not None:
        raise RuntimeError(
            f"daemon endpoint {name!r} was replaced while shutdown was in progress"
        )
    for attempt in range(10):
        try:
            ipc.cleanup_endpoint(name)
            Path(ipc.pid_path(name)).unlink(missing_ok=True)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.1)


def _remove_private_tree(path: Path, session_id: str) -> None:
    if session_id not in path.parts or not path.exists():
        return
    if path.is_symlink():
        raise OSError(f"refusing to remove symlinked harness directory: {path}")
    last_error: OSError | None = None
    for attempt in range(20):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
        if attempt < 19:
            time.sleep(0.1)
    raise OSError(f"cannot remove harness directory {path}: {last_error}")


def _brief_error(error: BaseException) -> str:
    text = str(error).strip()
    return text[:500] if text else error.__class__.__name__
