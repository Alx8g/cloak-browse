from __future__ import annotations

import io
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cloak_browse import cli, harness, worker
from cloak_browse.lease import AdvisoryRunLease, RunLeaseTimeout


def test_utf8_output_reconfigures_both_streams(monkeypatch):
    stdout, stderr = Mock(), Mock()
    monkeypatch.setattr(worker.sys, "stdout", stdout)
    monkeypatch.setattr(worker.sys, "stderr", stderr)
    worker.force_utf8_output()
    for stream in (stdout, stderr):
        stream.reconfigure.assert_called_once_with(
            encoding="utf-8", errors="backslashreplace"
        )


def test_utf8_output_accepts_streams_without_reconfigure(monkeypatch):
    monkeypatch.setattr(worker.sys, "stdout", io.StringIO())
    monkeypatch.setattr(worker.sys, "stderr", io.StringIO())
    worker.force_utf8_output()


def test_worker_configures_utf8_before_executing_code(monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "force_utf8_output", lambda: calls.append("utf8"))
    monkeypatch.setattr(
        worker, "_apply_helper_fixes", lambda h: calls.append("helpers")
    )
    monkeypatch.setattr(worker.runpy, "run_module", lambda *a, **k: calls.append("run"))
    worker.main()
    assert calls == ["utf8", "helpers", "run"]


def test_session_lease_is_exclusive_and_released(tmp_path):
    path = tmp_path / "run.lock"
    with (
        AdvisoryRunLease(path, 0.01),
        pytest.raises(RunLeaseTimeout),
        AdvisoryRunLease(path, 0.01),
    ):
        pytest.fail("second lease acquired")
    with AdvisoryRunLease(path, 0.01):
        pass


@pytest.mark.parametrize(
    "flag,value",
    [("--timeout", "301"), ("--lock-timeout", "0.5"), ("--lock-timeout", "61")],
)
def test_run_limits(flag, value):
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "pass", flag, value])
    assert exc.value.code == 2


@pytest.mark.parametrize("outcome", ["ready", "exit", "timeout", "existing"])
def test_daemon_start_budget_and_owned_cleanup(monkeypatch, tmp_path, outcome):
    import browser_harness

    process = Mock(returncode=7 if outcome == "exit" else None)
    process.poll.return_value = 7 if outcome == "exit" else None
    pings = iter(
        [True]
        if outcome == "existing"
        else [False, False, True]
        if outcome == "ready"
        else [False, False]
    )
    ipc = SimpleNamespace(
        ping=lambda *a, **k: next(pings),
        log_path=lambda name: tmp_path / "daemon.log",
        spawn_kwargs=lambda: {},
    )
    monkeypatch.setattr(browser_harness, "_ipc", ipc)
    monkeypatch.setenv("BU_NAME", "cloak")
    spawn = Mock(return_value=process)
    monkeypatch.setattr(harness.subprocess, "Popen", spawn)
    monkeypatch.setattr(harness, "_record_daemon_identity", lambda: None)
    ticks = iter([0, 1, 20] if outcome == "ready" else [0, 1, 91])
    monkeypatch.setattr(harness.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(harness.time, "sleep", lambda _: None)
    if outcome == "ready":
        harness._start_daemon(90)
        process.terminate.assert_not_called()
    else:
        with pytest.raises(
            RuntimeError,
            match={
                "exit": "exited with code 7",
                "timeout": "timed out",
                "existing": "existing daemon",
            }[outcome],
        ):
            harness._start_daemon(90)
        if outcome == "timeout":
            process.terminate.assert_called_once()
            process.wait.assert_called_once_with(timeout=2)
        elif outcome == "existing":
            spawn.assert_not_called()


@pytest.mark.parametrize(
    "healthy,cancel,matches",
    [
        (True, False, True),
        (False, False, True),
        (True, True, True),
        (False, True, False),
    ],
)
def test_recovery_requires_recorded_identity(
    monkeypatch, tmp_path, capsys, healthy, cancel, matches
):
    import browser_harness

    monkeypatch.setenv("BU_NAME", "cloak")
    monkeypatch.setenv("BH_CONFIG_DIR", str(tmp_path))
    (tmp_path / "daemon-identity.json").write_text(json.dumps([123, "start:1"]))
    requests = []

    def request(connection, token, payload):
        requests.append(payload)
        return {"target_id": "tab", "page": {}} if healthy else {"error": "offline"}

    ipc = SimpleNamespace(
        connect=lambda *a, **k: (Mock(), "token"),
        request=request,
        identify=lambda *a, **k: 123 if matches else 999,
    )
    monkeypatch.setattr(browser_harness, "_ipc", ipc)
    monkeypatch.setattr(harness, "process_start_token", lambda pid: "start:1")
    stop = Mock()
    monkeypatch.setattr(harness, "_stop_daemon", stop)
    if not matches:
        with pytest.raises(RuntimeError, match="identity"):
            harness._prepare_daemon(cancel)
        stop.assert_not_called()
    else:
        harness._prepare_daemon(cancel)
        if cancel or not healthy:
            stop.assert_called_once_with(force=True, expected_identity=(123, "start:1"))
            assert capsys.readouterr().out == "restart\n"
        else:
            stop.assert_not_called()
    assert any("method" in r for r in requests) == (cancel and matches)


def test_helper_shims_preserve_typed_js_and_printable_keys():
    calls = []
    helpers = SimpleNamespace(
        _KEYS={},
        js=lambda *a, **k: [],
        press_key=lambda *a, **k: None,
        click_at_xy=lambda *a, **k: None,
        cdp=lambda *a, **k: calls.append((a, k)),
    )
    worker._apply_helper_fixes(helpers)
    assert helpers.js_expect("[]", "list") == []
    with pytest.raises(TypeError):
        helpers.js_expect("[]", "string")
    with pytest.raises(TypeError):
        helpers.js_expect("[]", "unknown")
    helpers.press_key("a")
    assert calls[0][1]["code"] == "KeyA"
    assert calls[0][1]["text"] == "a"
    assert helpers._has_return_statement("return 1")
    assert not helpers._has_return_statement("'return'")
    assert not helpers._has_return_statement("() => { return 1; }")


def test_start_default_gives_harness_full_budget(app_paths, session_record):
    calls = []

    def runner(command, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(command, 0, "", "")

    assert harness.HarnessManager(app_paths, runner=runner).start(session_record).ok
    assert calls[0]["env"]["CLOAK_BROWSE_HARNESS_WAIT"] == "90.0"
    assert calls[0]["timeout"] == 100.0


@pytest.mark.parametrize("failure", ["missing", "empty", "reused"])
def test_recovery_refuses_missing_or_reused_identity(monkeypatch, tmp_path, failure):
    import browser_harness

    monkeypatch.setenv("BU_NAME", "cloak")
    monkeypatch.setenv("BH_CONFIG_DIR", str(tmp_path))
    if failure != "missing":
        (tmp_path / "daemon-identity.json").write_text(
            "" if failure == "empty" else json.dumps([123, "old-start"])
        )
    monkeypatch.setattr(
        browser_harness,
        "_ipc",
        SimpleNamespace(
            connect=lambda *a, **k: (Mock(), "token"),
            request=lambda *a, **k: {},
            identify=lambda *a, **k: 123,
        ),
    )
    monkeypatch.setattr(harness, "process_start_token", lambda pid: "new-start")
    stop = Mock()
    monkeypatch.setattr(harness, "_stop_daemon", stop)
    with pytest.raises((OSError, ValueError, RuntimeError)):
        harness._prepare_daemon()
    stop.assert_not_called()


def test_shutdown_rechecks_recovery_identity(monkeypatch):
    import browser_harness

    connect = Mock()
    monkeypatch.setenv("BU_NAME", "cloak")
    monkeypatch.setattr(
        browser_harness,
        "_ipc",
        SimpleNamespace(identify=lambda *a, **k: 999, connect=connect),
    )
    monkeypatch.setattr(harness, "process_start_token", lambda pid: "new-start")
    with pytest.raises(RuntimeError, match="identity changed"):
        harness._stop_daemon(force=True, expected_identity=(123, "old-start"))
    connect.assert_not_called()


def test_failed_preparation_never_executes_user_code(app_paths, session_record, capsys):
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "", "identity mismatch")

    manager = harness.HarnessManager(app_paths, runner=runner)
    assert manager.execute(session_record, "must_not_execute()", 1) == 1
    assert len(calls) == 1
    assert "identity mismatch" in capsys.readouterr().err


def test_daemon_identity_is_recorded_only_when_verifiable(monkeypatch, tmp_path):
    import browser_harness

    monkeypatch.setenv("BU_NAME", "cloak")
    monkeypatch.setenv("BH_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(
        browser_harness, "_ipc", SimpleNamespace(identify=lambda *a, **k: 123)
    )
    monkeypatch.setattr(harness, "process_start_token", lambda pid: None)
    with pytest.raises(RuntimeError, match="identity"):
        harness._record_daemon_identity()
    assert not (tmp_path / "daemon-identity.json").exists()
    monkeypatch.setattr(harness, "process_start_token", lambda pid: "start:123")
    harness._record_daemon_identity()
    assert json.loads((tmp_path / "daemon-identity.json").read_text()) == [
        123,
        "start:123",
    ]
