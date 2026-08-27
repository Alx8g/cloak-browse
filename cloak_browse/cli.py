"""cloak-browse: stealth browser + browser-harness in one CLI."""

import argparse
import json
import multiprocessing
import os
import signal
import site
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

CDP_PORT = 9333
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
BH_NAME = "cloak"
IS_WINDOWS = sys.platform == "win32"
SESSION_DIR = Path.home() / ".cache" / "cloak-browse"
SESSION_FILE = SESSION_DIR / "session.json"


# --- session metadata ---


def _save_session(**fields):
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    existing = _load_session()
    existing.update(fields)
    existing["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    temporary = SESSION_FILE.with_name(f".{SESSION_FILE.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        os.replace(temporary, SESSION_FILE)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _load_session() -> dict:
    try:
        return json.loads(SESSION_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _clear_session():
    try:
        SESSION_FILE.unlink()
    except FileNotFoundError:
        pass


# --- cross-process run lease ---


class RunLeaseTimeout(RuntimeError):
    """Raised when another cloak-browse run keeps the session lease too long."""


class AdvisoryRunLease:
    """Exclusive OS advisory lease held for one complete `run` lifecycle."""

    def __init__(self, path: Path, timeout: float):
        self.path = path
        self.timeout = timeout
        self._file = None
        self._held = False

    def _try_acquire(self) -> bool:
        assert self._file is not None
        self._file.seek(0)
        if IS_WINDOWS:
            import msvcrt

            try:
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False

        import fcntl

        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+b")
        self._file.seek(0, os.SEEK_END)
        if self._file.tell() == 0:
            self._file.write(b"\0")
            self._file.flush()

        deadline = time.monotonic() + self.timeout
        while True:
            if self._try_acquire():
                self._held = True
                return self
            if time.monotonic() >= deadline:
                self._file.close()
                self._file = None
                raise RunLeaseTimeout(
                    f"another cloak-browse run holds {self.path}; waited {self.timeout:g}s"
                )
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def release(self):
        if self._file is None:
            return
        try:
            if self._held:
                self._file.seek(0)
                if IS_WINDOWS:
                    import msvcrt

                    msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._held = False
            self._file.close()
            self._file = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback):
        self.release()
        return False


def _run_lock_path() -> Path:
    return SESSION_DIR / "run.lock"


# --- CDP probes ---


def _cdp_version() -> dict | None:
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=2) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _cdp_alive() -> bool:
    return _cdp_version() is not None


def _cdp_healthy() -> bool:
    """Require CDP discovery and a live WebSocket, not HTTP alone."""
    info = _cdp_version()
    if not isinstance(info, dict):
        return False
    ws_url = info.get("webSocketDebuggerUrl")
    return isinstance(ws_url, str) and bool(ws_url) and _cdp_ws_probe(ws_url)


def _cdp_ws_probe(ws_url: str, timeout: float = 3.0) -> bool:
    try:
        import websockets.sync.client as wsc
        conn = wsc.connect(ws_url, open_timeout=timeout, close_timeout=1)
        conn.close()
        return True
    except Exception:
        return False


def _wait_cdp_ready(timeout: float = 15.0) -> dict | None:
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        info = _cdp_version()
        if info:
            ws_url = info.get("webSocketDebuggerUrl")
            if ws_url and _cdp_ws_probe(ws_url):
                return info
            last_err = "CDP HTTP up but WebSocket not ready"
        time.sleep(0.3)
    if last_err:
        print(f"  warning: {last_err}", file=sys.stderr)
    return None


def _cdp_targets() -> list[dict]:
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/list", timeout=2) as r:
            return json.loads(r.read())
    except Exception:
        return []


# --- daemon management (cross-platform via browser-harness IPC) ---


def _bh_daemon_alive() -> bool:
    try:
        from browser_harness._ipc import ping
        return ping(BH_NAME, timeout=1.0)
    except Exception:
        return False


def _identify_bh_daemon() -> int | None:
    try:
        from browser_harness._ipc import identify

        pid = identify(BH_NAME, timeout=2.0)
        return pid if type(pid) is int and pid > 0 else None
    except Exception:
        return None


def _bh_connection_healthy() -> bool:
    """Confirm the daemon can make a CDP-backed request, not merely ping."""
    try:
        from browser_harness._ipc import connect, request

        conn, token = connect(BH_NAME, timeout=2.0)
        try:
            response = request(conn, token, {"meta": "connection_status"})
        finally:
            conn.close()
    except Exception:
        return False
    return (
        isinstance(response, dict)
        and "error" not in response
        and isinstance(response.get("target_id"), str)
        and isinstance(response.get("page"), (dict, type(None)))
    )


def _stop_bh_daemon():
    try:
        from browser_harness._ipc import connect, request, identify, cleanup_endpoint
        pid = identify(BH_NAME, timeout=2.0)
        try:
            c, token = connect(BH_NAME, timeout=5.0)
            request(c, token, {"meta": "shutdown"})
            c.close()
        except Exception:
            pass
        if pid:
            # Wait for the IPC shutdown to take effect, then force-kill if the
            # daemon is wedged. Uses the cross-platform reaper — the old
            # os.kill(pid, 0) poll is a no-op on Windows (signal 0 is
            # CTRL_C_EVENT), so the fallback never fired there.
            deadline = time.time() + 5
            while time.time() < deadline and _pid_alive(pid):
                time.sleep(0.1)
            _kill_pid(pid)
        cleanup_endpoint(BH_NAME)
    except Exception:
        pass


def _start_bh_daemon(*, stop_existing: bool = True) -> bool:
    if stop_existing:
        _stop_bh_daemon()
    env = {**os.environ, "BU_NAME": BH_NAME, "BU_CDP_URL": CDP_URL}

    spawn_kwargs = {}
    daemon_python = sys.executable
    if IS_WINDOWS:
        spawn_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
        # uv's Windows tool python.exe is a launcher process. The harness runs
        # in its child interpreter, so comparing identify() with the launcher's
        # PID rejects the daemon we just started. Launch the base interpreter
        # directly and supply this tool's import roots so PID identity remains
        # exact without weakening the endpoint ownership check.
        daemon_python = getattr(sys, "_base_executable", sys.executable)
        if daemon_python != sys.executable:
            import_paths = [path for path in site.getsitepackages() if path]
            if env.get("PYTHONPATH"):
                import_paths.append(env["PYTHONPATH"])
            env["PYTHONPATH"] = os.pathsep.join(import_paths)
    else:
        spawn_kwargs["start_new_session"] = True

    proc = subprocess.Popen(
        [
            daemon_python,
            "-c",
            "from browser_harness.daemon import main; import asyncio; asyncio.run(main())",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **spawn_kwargs,
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        if _bh_daemon_alive():
            # Persist a PID plus OS-issued start identity. PID alone can be
            # reused, so run-time recovery rejects a daemon unless both match.
            # A live endpoint is not enough: it must identify as the process
            # just spawned, rather than a listener left by another invocation.
            if _identify_bh_daemon() != proc.pid:
                _kill_pid(proc.pid)
                print("error: harness endpoint is not the daemon just started", file=sys.stderr)
                return False
            start_identity = _process_start_identity(proc.pid)
            if start_identity is None:
                _kill_pid(proc.pid)
                print("error: cannot record harness process identity", file=sys.stderr)
                return False
            _save_session(daemonPid=proc.pid, daemonStartIdentity=start_identity)
            return True
        time.sleep(0.3)

    # Readiness never proved ownership, but the Popen handle still proves this
    # exact process is ours. Reap it before returning so a failed start cannot
    # leave an unmanaged daemon behind.
    _kill_pid(proc.pid)
    from browser_harness._ipc import log_path
    log = log_path(BH_NAME)
    tail = ""
    if log.exists():
        lines = log.read_text().strip().splitlines()
        tail = lines[-1] if lines else ""
    print(f"error: browser-harness daemon didn't start: {tail}", file=sys.stderr)
    return False


# --- stale session cleanup ---


_BROWSER_PROC_NAMES = {
    "chrome", "chromium", "headless_shell", "cloakbrowser", "node",
    "chrome.exe", "chromium.exe", "headless_shell.exe", "cloakbrowser.exe", "node.exe",
}


def _pids_on_debug_port() -> list[int]:
    """PIDs of browser/driver processes that bind our CDP port.

    Deterministic discovery via the unique `--remote-debugging-port=<port>`
    argument we control — version-independent, no private Playwright internals.
    Restricted to known browser/driver executables so an unrelated process that
    merely mentions the port string (e.g. a shell running a cloak-browse
    command) is never matched. Used to reap an orphan left by an ungraceful
    `start` exit.
    """
    needle = f"--remote-debugging-port={CDP_PORT}"
    pids: list[int] = []
    try:
        if IS_WINDOWS:
            names = ",".join(f"'{n}'" for n in sorted(_BROWSER_PROC_NAMES))
            ps = (
                "Get-CimInstance Win32_Process | Where-Object { "
                f"$_.CommandLine -like '*{needle}*' -and $_.Name -in @({names})"
                " } | ForEach-Object { $_.ProcessId }"
            )
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, text=True, timeout=15,
            ).stdout
            toks = out.split()
        else:
            out = subprocess.run(
                ["ps", "-eo", "pid=,comm=,args="], capture_output=True, text=True, timeout=15
            ).stdout
            toks = []
            for line in out.splitlines():
                parts = line.split(None, 2)
                if len(parts) < 3:
                    continue
                pid, comm, argv = parts
                if needle in argv and os.path.basename(comm) in _BROWSER_PROC_NAMES:
                    toks.append(pid)
        for tok in toks:
            try:
                pids.append(int(tok))
            except ValueError:
                pass
    except Exception:
        pass
    return pids


def _pid_alive(pid) -> bool:
    if pid is None:
        return False
    if IS_WINDOWS:
        # os.kill(pid, 0) is NOT a liveness probe on Windows — signal 0 is
        # CTRL_C_EVENT, which raises for out-of-group PIDs. Ask the OS instead.
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            return str(int(pid)) in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError):
        return False


def _process_start_identity(pid: int) -> str | None:
    """Return an OS-issued process-start identity to guard PID reuse."""
    if type(pid) is not int or pid <= 0:
        return None
    if IS_WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes

            class FILETIME(ctypes.Structure):
                _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetProcessTimes.argtypes = (
                wintypes.HANDLE,
                ctypes.POINTER(FILETIME),
                ctypes.POINTER(FILETIME),
                ctypes.POINTER(FILETIME),
                ctypes.POINTER(FILETIME),
            )
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return None
            try:
                created, exited, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
                if not kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(created),
                    ctypes.byref(exited),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                ):
                    return None
                value = (created.dwHighDateTime << 32) | created.dwLowDateTime
                return f"win:{value}"
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return None

    try:
        # Linux field 22 is process start time in clock ticks. Parse after the
        # final ')' because a process name may itself contain spaces or ')'.
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat[stat.rfind(")") + 2:].split()
        return f"proc:{fields[19]}"
    except (FileNotFoundError, IndexError, OSError):
        pass

    try:
        value = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        ).stdout.strip()
        return f"ps:{value}" if value else None
    except (OSError, subprocess.SubprocessError):
        return None


def _kill_pid(pid):
    if pid is None or not _pid_alive(pid):
        return
    if IS_WINDOWS:
        # /T also reaps child processes (e.g. chromium renderers, the driver's
        # browser). CTRL_BREAK_EVENT only works on process-group leaders.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(int(pid))],
            capture_output=True,
        )
        return
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.5)
        if _pid_alive(pid):
            os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


def _cleanup_stale_session():
    session = _load_session()
    _stop_bh_daemon()
    if session:
        _kill_pid(session.get("daemonPid"))
    # Reap an orphaned browser deterministically by the CDP port it holds,
    # whether or not a session file survived the crash.
    for pid in _pids_on_debug_port():
        _kill_pid(pid)
    _clear_session()


def _verified_owned_daemon(session: dict) -> tuple[bool, str]:
    """Prove that the live harness is the daemon this cloak session started."""
    expected_pid = session.get("daemonPid")
    expected_start = session.get("daemonStartIdentity")
    if type(expected_pid) is not int or expected_pid <= 0 or not isinstance(expected_start, str):
        return False, "session record has no daemon PID/start identity"

    identified_pid = _identify_bh_daemon()
    if identified_pid != expected_pid:
        return False, "live harness PID does not match the session record"
    if not _pid_alive(expected_pid):
        return False, "recorded harness PID is no longer live"
    if _process_start_identity(expected_pid) != expected_start:
        return False, "live harness start identity does not match the session record"
    return True, ""


def _stop_verified_owned_daemon(session: dict) -> bool:
    """Stop only a daemon that still proves it belongs to this cloak session."""
    verified, reason = _verified_owned_daemon(session)
    if not verified:
        print(f"recovery refused: {reason}", file=sys.stderr)
        return False

    pid = session["daemonPid"]
    try:
        from browser_harness._ipc import connect, request

        conn, token = connect(BH_NAME, timeout=2.0)
        try:
            request(conn, token, {"meta": "shutdown"})
        finally:
            conn.close()
    except Exception:
        # A wedged but identity-matched daemon may not accept shutdown. It is
        # still safe to signal exactly that recorded process below.
        pass

    deadline = time.monotonic() + 5.0
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _pid_alive(pid):
        # Recheck immediately before signalling: PID reuse after the IPC check
        # must turn this into a refusal, never a signal to an unrelated process.
        verified, reason = _verified_owned_daemon(session)
        if not verified:
            print(f"recovery refused: {reason}", file=sys.stderr)
            return False
        _kill_pid(pid)
    if _pid_alive(pid):
        print("recovery refused: verified harness daemon did not stop", file=sys.stderr)
        return False

    try:
        from browser_harness._ipc import cleanup_endpoint

        cleanup_endpoint(BH_NAME)
    except Exception:
        print("recovery refused: could not clear verified harness endpoint", file=sys.stderr)
        return False
    return True


def _terminate_browser_execution():
    """Best-effort CDP cancellation before recycling an owned daemon."""
    try:
        from browser_harness._ipc import connect, request

        conn, token = connect(BH_NAME, timeout=2.0)
        try:
            response = request(
                conn,
                token,
                {"method": "Runtime.terminateExecution", "params": {}, "session_id": None},
            )
        finally:
            conn.close()
        return isinstance(response, dict) and "error" not in response
    except Exception:
        return False


def _recover_owned_daemon(*, cancel_execution: bool = False) -> bool:
    """Recycle one identity-matched daemon against the existing CDP browser."""
    session = _load_session()
    verified, reason = _verified_owned_daemon(session)
    if not verified:
        print(f"recovery refused: {reason}", file=sys.stderr)
        return False
    if cancel_execution:
        _terminate_browser_execution()
    if not _stop_verified_owned_daemon(session):
        return False
    if not _start_bh_daemon(stop_existing=False):
        print("recovery failed: verified harness daemon did not restart", file=sys.stderr)
        return False
    if not _cdp_healthy() or not _bh_connection_healthy():
        print("recovery failed: restarted harness is not CDP-healthy", file=sys.stderr)
        return False
    return True


# --- commands ---


def cmd_start(args):
    from cloakbrowser import launch, launch_persistent_context
    from cloakbrowser.download import ensure_binary

    if _cdp_alive() or _load_session():
        _cleanup_stale_session()

    # Ensure stealth binary is downloaded
    print("checking stealth chromium binary...")
    binary_path = ensure_binary()
    print(f"  binary: {binary_path}")

    extra_args = [f"--remote-debugging-port={CDP_PORT}"]
    proxy = args.proxy or None
    headless = args.headless
    mode = "headless" if headless else "headed"
    backend = "playwright"
    if getattr(args, "backend", None) and args.backend != "playwright":
        print("  warning: --backend is deprecated; CloakBrowser uses stock Playwright only")

    print(f"launching stealth chromium ({mode}, {backend} backend, CDP on :{CDP_PORT})...")

    launch_kwargs = dict(
        headless=headless,
        proxy=proxy,
        args=extra_args,
        humanize=args.humanize,
    )

    if args.profile:
        launched = launch_persistent_context(
            user_data_dir=os.path.expanduser(args.profile),
            **launch_kwargs,
        )
        cleanup = launched.close
    else:
        launched = launch(**launch_kwargs)
        cleanup = launched.close

    info = _wait_cdp_ready(timeout=15.0)
    if not info:
        print("error: CDP endpoint didn't become ready", file=sys.stderr)
        cleanup()
        sys.exit(1)

    browser_version = info.get("Browser", "?")
    print(f"  browser: {browser_version}")
    print(f"  backend: {backend}")
    print(f"  CDP:     {CDP_URL}")
    print(f"  mode:    {mode}")

    _save_session(
        browserVersion=browser_version,
        backend=backend,
        mode=mode,
        cdpPort=CDP_PORT,
        profile=args.profile or "(temp)",
        proxy=args.proxy or None,
        humanize=args.humanize,
        startedAt=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )

    print("starting browser-harness daemon...")
    if _start_bh_daemon():
        print(f"  harness: ready (BU_NAME={BH_NAME})")
        print()
        print("usage:")
        print(f"  cloak-browse run \"new_tab('https://example.com')\"")
        print(f'  cloak-browse run "print(page_info())"')
        print(f'  cloak-browse run "print(js(\'document.body.innerText\')[:2000])"')
    else:
        print("  harness: failed (browser still running, you can retry manually)")

    print()
    print("ctrl+c to stop")

    _is_closing = False
    stop = False

    def handle_sig(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, handle_sig)
    if not IS_WINDOWS:
        signal.signal(signal.SIGTERM, handle_sig)

    while not stop:
        time.sleep(0.5)

    if _is_closing:
        return
    _is_closing = True

    print("\nshutting down...")
    _stop_bh_daemon()
    try:
        cleanup()
    except Exception:
        pass
    _clear_session()
    print("done.")


def _install_js_expect(h):
    """Install a narrow typed JS boundary without changing `js` compatibility."""
    if getattr(h, "_cloak_js_expect_fixed", False):
        return

    expected_types = {
        "string": str,
        "list": list,
        "object": dict,
        "bool": bool,
    }

    def js_expect(expression: str, expected_type: str, target_id=None):
        """Evaluate JS and require an exact string, list, object, or bool result."""
        if not isinstance(expected_type, str):
            raise TypeError("js_expect expected_type must be string, list, object, or bool")
        expected = expected_types.get(expected_type)
        if expected is None:
            raise TypeError("js_expect expected_type must be string, list, object, or bool")
        value = h.js(expression, target_id=target_id)
        if type(value) is not expected:
            raise TypeError(f"js_expect expected {expected_type}, got {type(value).__name__}")
        return value

    h.js_expect = js_expect
    h._cloak_js_expect_fixed = True


def _apply_helper_fixes(h):
    """Patch browser-harness edge cases at the cloak-browse boundary.

    The installed harness is pulled from git, so cloak-browse owns the compatibility
    shim for behaviors that break browser tasks in practice: printable key dispatch,
    naive JS return wrapping, and clicks that synchronously open native dialogs.
    """
    if getattr(h, "_cloak_helper_fixed", False):
        return
    _install_js_expect(h)

    _printable_keys = {
        ".": (190, "Period"), ",": (188, "Comma"), "/": (191, "Slash"),
        "\\": (220, "Backslash"), ";": (186, "Semicolon"), "'": (222, "Quote"),
        "[": (219, "BracketLeft"), "]": (221, "BracketRight"), "-": (189, "Minus"),
        "=": (187, "Equal"), "`": (192, "Backquote"), " ": (32, "Space"),
        "!": (49, "Digit1"), "@": (50, "Digit2"), "#": (51, "Digit3"),
        "$": (52, "Digit4"), "%": (53, "Digit5"), "^": (54, "Digit6"),
        "&": (55, "Digit7"), "*": (56, "Digit8"), "(": (57, "Digit9"),
        ")": (48, "Digit0"), "_": (189, "Minus"), "+": (187, "Equal"),
        "{": (219, "BracketLeft"), "}": (221, "BracketRight"), "|": (220, "Backslash"),
        ":": (186, "Semicolon"), '"': (222, "Quote"), "<": (188, "Comma"),
        ">": (190, "Period"), "?": (191, "Slash"), "~": (192, "Backquote"),
    }

    def _key_triplet(key):
        if key in h._KEYS:
            return h._KEYS[key]
        if len(key) != 1:
            return 0, key, ""
        if "a" <= key <= "z" or "A" <= key <= "Z":
            upper = key.upper()
            return ord(upper), f"Key{upper}", key
        if "0" <= key <= "9":
            return ord(key), f"Digit{key}", key
        vk, code = _printable_keys.get(key, (ord(key), key))
        return vk, code, key

    def press_key(key, modifiers=0):
        vk, code, text = _key_triplet(key)
        base = {"key": key, "code": code, "modifiers": modifiers,
                "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
        h.cdp("Input.dispatchKeyEvent", type="keyDown", **base,
              **({"text": text} if text else {}))
        h.cdp("Input.dispatchKeyEvent", type="keyUp", **base)

    def has_executable_return_statement(expression):
        i = 0
        n = len(expression)
        state = "code"
        quote = ""
        ignore_depths = []
        pending_ignore_block = False
        while i < n:
            ch = expression[i]
            nxt = expression[i + 1] if i + 1 < n else ""
            if state == "code":
                if ch in ("'", '"', "`"):
                    state = "string"; quote = ch; i += 1; continue
                if ch == "/" and nxt == "/":
                    state = "line_comment"; i += 2; continue
                if ch == "/" and nxt == "*":
                    state = "block_comment"; i += 2; continue
                if expression.startswith("=>", i):
                    pending_ignore_block = True; i += 2; continue
                if expression.startswith("function", i):
                    before = expression[i - 1] if i > 0 else ""
                    after = expression[i + 8] if i + 8 < n else ""
                    if not (before == "_" or before.isalnum()) and not (after == "_" or after.isalnum()):
                        pending_ignore_block = True
                    i += 8; continue
                if expression.startswith("class", i):
                    before = expression[i - 1] if i > 0 else ""
                    after = expression[i + 5] if i + 5 < n else ""
                    if not (before == "_" or before.isalnum()) and not (after == "_" or after.isalnum()):
                        pending_ignore_block = True
                    i += 5; continue
                if ch == "{":
                    if pending_ignore_block:
                        ignore_depths.append(1)
                        pending_ignore_block = False
                    else:
                        ignore_depths = [d + 1 for d in ignore_depths]
                    i += 1; continue
                if ch == "}":
                    next_depths = []
                    for depth in ignore_depths:
                        if depth > 1:
                            next_depths.append(depth - 1)
                    ignore_depths = next_depths
                    i += 1; continue
                if expression.startswith("return", i):
                    before = expression[i - 1] if i > 0 else ""
                    after = expression[i + 6] if i + 6 < n else ""
                    if not (before == "_" or before.isalnum()) and not (after == "_" or after.isalnum()):
                        j = i + 6
                        while j < n and expression[j].isspace():
                            j += 1
                        if not ignore_depths and (j >= n or expression[j] != ":"):
                            return True
                    i += 6; continue
                if not ch.isspace():
                    pending_ignore_block = False
                i += 1; continue
            if state == "line_comment":
                if ch == "\n":
                    state = "code"
                i += 1; continue
            if state == "block_comment":
                if ch == "*" and nxt == "/":
                    state = "code"; i += 2; continue
                i += 1; continue
            if state == "string":
                if ch == "\\":
                    i += 2; continue
                if ch == quote:
                    state = "code"; quote = ""
                i += 1; continue
        return False

    old_click_at_xy = h.click_at_xy

    def click_at_xy(x, y, button="left", clicks=1):
        try:
            return old_click_at_xy(x, y, button=button, clicks=clicks)
        except TimeoutError:
            deadline = time.time() + 1.0
            while time.time() < deadline:
                try:
                    response = h._send({"meta": "pending_dialog"})
                    if not isinstance(response, dict):
                        raise TypeError("pending_dialog reply must be a mapping")
                    if response.get("dialog"):
                        return None
                except TypeError:
                    raise
                except Exception:
                    pass
                time.sleep(0.05)
            raise

    press_key.__doc__ = getattr(h.press_key, "__doc__", None)
    click_at_xy.__doc__ = getattr(h.click_at_xy, "__doc__", None)
    h.press_key = press_key
    h.click_at_xy = click_at_xy
    h._has_return_statement = has_executable_return_statement
    h._cloak_helper_fixed = True


@dataclass(frozen=True)
class ChildRunResult:
    timed_out: bool
    terminated: bool
    exit_code: int | None


def _run_code_child(code: str, environment: dict[str, str]):
    """Execute user code in a process the parent can reliably terminate."""
    _force_utf8_output()
    os.environ.update(environment)
    try:
        import browser_harness.helpers as helpers

        _apply_helper_fixes(helpers)
        namespace = {key: getattr(helpers, key) for key in dir(helpers) if not key.startswith("_")}
        exec(code, namespace)
    except BaseException:  # noqa: BLE001 - preserve CLI failure semantics
        import traceback

        traceback.print_exc()
        raise SystemExit(1)


def _terminate_child(process, grace: float = 2.0) -> bool:
    """Terminate then force-kill a child; do not report timeout while it survives."""
    process.terminate()
    process.join(grace)
    if process.is_alive():
        process.kill()
        process.join(grace)
    return not process.is_alive()


def _execute_run_child(code: str, timeout: float | None) -> ChildRunResult:
    context = multiprocessing.get_context("spawn")
    child = context.Process(target=_run_code_child, args=(code, {"BU_NAME": BH_NAME}))
    child.start()
    child.join(timeout)
    if timeout is not None and child.is_alive():
        return ChildRunResult(
            timed_out=True,
            terminated=_terminate_child(child),
            exit_code=child.exitcode,
        )
    return ChildRunResult(timed_out=False, terminated=True, exit_code=child.exitcode)


def _prepare_run() -> bool:
    if not _cdp_healthy():
        print(
            "error: no CDP-healthy stealth browser running — run `cloak-browse start` first",
            file=sys.stderr,
        )
        return False

    if not _bh_daemon_alive():
        print("harness daemon not running, starting...", file=sys.stderr)
        # This is a fresh launch, not recovery. Do not stop an endpoint that
        # did not prove it belongs to the recorded cloak session.
        if not _start_bh_daemon(stop_existing=False):
            return False

    if _bh_connection_healthy():
        return True

    print("harness CDP connection is unhealthy; attempting verified recovery...", file=sys.stderr)
    return _recover_owned_daemon()


def cmd_run(args):
    try:
        with AdvisoryRunLease(_run_lock_path(), args.lock_timeout):
            if not _prepare_run():
                sys.exit(1)

            result = _execute_run_child(args.code, args.timeout)
            if result.timed_out:
                if not result.terminated:
                    print("error: timed-out child could not be terminated", file=sys.stderr)
                    sys.exit(1)

                # The child cannot issue more helper calls now. The recovery
                # path first proves daemon ownership, then cancels a potentially
                # active JS evaluation and recycles that daemon. User code is
                # deliberately never replayed.
                _recover_owned_daemon(cancel_execution=True)
                print(f"error: run timed out after {args.timeout:g}s", file=sys.stderr)
                sys.exit(124)
            if result.exit_code:
                sys.exit(1)
    except RunLeaseTimeout as exc:
        print(f"error: run lease unavailable: {exc}", file=sys.stderr)
        sys.exit(75)


def cmd_stop(args):
    _stop_bh_daemon()
    _clear_session()
    print("harness daemon stopped.")
    if _cdp_alive():
        print("note: stealth browser still running (ctrl+c the `start` process to close it)")


def cmd_status(args):
    cdp_up = _cdp_alive()
    bh_up = _bh_daemon_alive()
    session = _load_session()

    print(f"stealth browser:  {'running' if cdp_up else 'not running'}")
    if cdp_up:
        info = _cdp_version()
        if info:
            print(f"  browser:  {info.get('Browser', '?')}")
            ws_url = info.get("webSocketDebuggerUrl", "")
            ws_ok = _cdp_ws_probe(ws_url) if ws_url else False
            print(f"  CDP WS:   {'ok' if ws_ok else 'unreachable'}")
        targets = _cdp_targets()
        pages = [t for t in targets if t.get("type") == "page"]
        print(f"  tabs:     {len(pages)}")
        for t in pages[:5]:
            title = t.get("title", "")[:40]
            url = t.get("url", "")[:60]
            print(f"    - {title or '(untitled)'} | {url}")

    print(f"harness daemon:   {'running' if bh_up else 'not running'} (BU_NAME={BH_NAME})")

    if session:
        print("session:")
        print(f"  backend:  {session.get('backend', '?')}")
        print(f"  mode:     {session.get('mode', '?')}")
        print(f"  profile:  {session.get('profile', '?')}")
        print(f"  started:  {session.get('startedAt', '?')}")
        if session.get("proxy"):
            proxy = session["proxy"]
            if "@" in proxy:
                proxy = proxy.split("@")[-1]
            print(f"  proxy:    {proxy}")

    if args.json_output:
        result = {
            "browser": "running" if cdp_up else "stopped",
            "daemon": "running" if bh_up else "stopped",
            "cdpPort": CDP_PORT,
            **session,
        }
        if cdp_up:
            result["tabs"] = len(pages)
        print(json.dumps(result, indent=2))


def _positive_timeout(value: str) -> float:
    result = float(value)
    if not 0 < result <= 300:
        raise argparse.ArgumentTypeError("must be greater than zero and at most 300")
    return result


def _lock_timeout(value: str) -> float:
    result = float(value)
    if not 1 <= result <= 60:
        raise argparse.ArgumentTypeError("must be between 1 and 60")
    return result


def _force_utf8_output():
    # Windows consoles default to cp1252; any non-Latin1 char in page
    # content or tab titles otherwise crashes the whole command.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError):
            pass


def main():
    _force_utf8_output()
    p = argparse.ArgumentParser(
        prog="cloak-browse",
        description="Stealth browser controlled by browser-harness",
    )
    sub = p.add_subparsers(dest="command")

    sp = sub.add_parser("start", help="Launch stealth browser + harness")
    sp.add_argument("--proxy", help="Proxy URL (http://user:pass@host:port)")
    sp.add_argument("--profile", help="Persistent profile directory path")
    sp.add_argument("--headless", action="store_true", help="Run headless (stealth patches still active)")
    sp.add_argument("--humanize", action="store_true", help="Enable human-like mouse/keyboard behavior")
    sp.add_argument("--backend", choices=["playwright", "patchright"], default="playwright",
                    help="Deprecated compatibility flag; CloakBrowser now uses stock Playwright only")
    sp.set_defaults(func=cmd_start)

    sp = sub.add_parser("run", help="Run code against the stealth browser")
    sp.add_argument("code", help="Python code to execute")
    sp.add_argument("--timeout", type=_positive_timeout, default=None, help="Max execution time in seconds")
    sp.add_argument(
        "--lock-timeout",
        type=_lock_timeout,
        default=15.0,
        help="Max time to wait for another run against this persistent session (default: 15)",
    )
    sp.set_defaults(func=cmd_run)

    sub.add_parser("stop", help="Stop harness daemon").set_defaults(func=cmd_stop)

    sp = sub.add_parser("status", help="Show detailed status")
    sp.add_argument("--json", dest="json_output", action="store_true", help="JSON output")
    sp.set_defaults(func=cmd_status)

    args = p.parse_args()
    if not args.command:
        p.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
