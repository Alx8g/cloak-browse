from __future__ import annotations

from io import StringIO
from types import SimpleNamespace

import pytest

from cloak_browse.background import start_background
from cloak_browse.cli import main
from cloak_browse.runtime import StartOptions
from cloak_browse.session import SessionStore

OPTIONS = StartOptions(None, None, True, False, "playwright")


def test_ready_waits_for_child_running_record(monkeypatch, app_paths, session_record):
    calls = []
    store = SessionStore(app_paths)

    def launch(command, **kwargs):
        calls.append((command, kwargs))
        store.write(
            session_record.with_updates(
                owner_pid=789, session_id=kwargs["env"]["CLOAK_BROWSE_LAUNCH_ID"]
            )
        )
        return SimpleNamespace(pid=456, poll=lambda: None)

    monkeypatch.setattr("cloak_browse.background.subprocess.Popen", launch)
    output = StringIO()
    assert start_background(OPTIONS, app_paths, 2, output) == 0
    command, kwargs = calls[0]
    assert "--background" not in command
    assert "--headless" in command
    assert kwargs["env"]["CLOAK_BROWSE_CACHE_DIR"] == str(app_paths.cache_dir)
    assert kwargs["close_fds"] is True
    assert "ready" in output.getvalue()


def test_existing_session_is_not_replaced(monkeypatch, app_paths, session_record):
    store = SessionStore(app_paths)
    store.write(session_record)
    monkeypatch.setattr(
        "cloak_browse.background.subprocess.Popen",
        lambda *a, **k: pytest.fail("must not launch"),
    )
    assert start_background(OPTIONS, app_paths, 2, stderr=StringIO()) == 1
    assert store.load().record == session_record


def test_timeout_does_not_stop_another_owner(monkeypatch, app_paths, session_record):
    store = SessionStore(app_paths)

    def launch(*args, **kwargs):
        store.write(session_record)
        return SimpleNamespace(pid=456, poll=lambda: None)

    monkeypatch.setattr("cloak_browse.background.subprocess.Popen", launch)
    ticks = iter([0, 3])
    monkeypatch.setattr("cloak_browse.background.time.monotonic", lambda: next(ticks))
    assert start_background(OPTIONS, app_paths, 2, stderr=StringIO()) == 124
    assert not store.stop_requested(session_record.session_id)


def test_spawn_failure_is_reported(monkeypatch, app_paths):
    def launch(*args, **kwargs):
        raise OSError("launch denied")

    monkeypatch.setattr("cloak_browse.background.subprocess.Popen", launch)
    error = StringIO()
    assert start_background(OPTIONS, app_paths, 2, stderr=error) == 1
    assert "launch denied" in error.getvalue()


def test_early_exit_is_failure(monkeypatch, app_paths):
    monkeypatch.setattr(
        "cloak_browse.background.subprocess.Popen",
        lambda *a, **k: SimpleNamespace(pid=456, poll=lambda: 7),
    )
    error = StringIO()
    assert start_background(OPTIONS, app_paths, 2, stderr=error) == 1
    assert "exited 7" in error.getvalue()


def test_timeout_requests_only_owned_session_shutdown(
    monkeypatch, app_paths, session_record
):
    store = SessionStore(app_paths)

    def launch(*args, **kwargs):
        store.write(
            session_record.with_updates(
                owner_pid=789,
                phase="starting",
                session_id=kwargs["env"]["CLOAK_BROWSE_LAUNCH_ID"],
            )
        )
        return SimpleNamespace(pid=456, poll=lambda: None)

    monkeypatch.setattr("cloak_browse.background.subprocess.Popen", launch)
    ticks = iter([0, 3])
    monkeypatch.setattr("cloak_browse.background.time.monotonic", lambda: next(ticks))
    assert start_background(OPTIONS, app_paths, 2, stderr=StringIO()) == 124
    assert store.stop_requested(store.load().record.session_id)


@pytest.mark.parametrize("timeout", ["nan", "inf", "-inf", "0"])
def test_background_timeout_requires_finite_positive_number(timeout):
    with pytest.raises(SystemExit) as exc:
        main(["start", "--background", f"--startup-timeout={timeout}"])
    assert exc.value.code == 2
