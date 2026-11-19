from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from cloak_browse.paths import ensure_runtime_dir


@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX paths are used on POSIX")
def test_socket_path_fits_macos_limit_with_long_tmpdir(app_paths, monkeypatch):
    monkeypatch.setattr(
        "cloak_browse.paths.tempfile.gettempdir",
        lambda: "/var/folders/aa/" + "b" * 30 + "/T",
    )
    path = app_paths.harness(str(uuid4())).runtime_dir / "bu.sock"
    assert path.is_relative_to(Path("/tmp"))
    assert len(os.fsencode(path)) < 104


@pytest.mark.skipif(os.name == "nt", reason="Shared /tmp namespace is POSIX-only")
def test_runtime_parent_is_scoped_to_each_account(app_paths, monkeypatch):
    monkeypatch.setattr("cloak_browse.paths.os.getuid", lambda: 1001)
    first = app_paths.harness(str(uuid4())).runtime_dir
    monkeypatch.setattr("cloak_browse.paths.os.getuid", lambda: 1002)
    second = app_paths.harness(str(uuid4())).runtime_dir
    assert first.parent == Path("/tmp/cloak-browse-1001")
    assert second.parent == Path("/tmp/cloak-browse-1002")
    assert first.parent != second.parent


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and permissions")
def test_runtime_directories_are_created_private_and_reusable(tmp_path):
    path = tmp_path / "account" / "session"
    assert ensure_runtime_dir(path) == path
    for directory in (path.parent, path):
        assert directory.stat().st_mode & 0o777 == 0o700
        assert directory.stat().st_uid == os.getuid()
    assert ensure_runtime_dir(path) == path


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and permissions")
@pytest.mark.parametrize("level", ["account", "session"])
@pytest.mark.parametrize("unsafe", ["writable", "symlink", "owner", "file"])
def test_unsafe_runtime_directories_are_refused(tmp_path, monkeypatch, level, unsafe):
    path = tmp_path / "account" / "session"
    path.parent.mkdir(mode=0o700)
    target = path.parent if level == "account" else path
    if target != path.parent:
        target.mkdir(mode=0o700)
    if unsafe == "writable":
        target.chmod(0o777)
    elif unsafe == "owner":
        uid = os.getuid()
        monkeypatch.setattr("cloak_browse.paths.os.getuid", lambda: uid + 1)
    else:
        target.rmdir()
        if unsafe == "symlink":
            other = tmp_path / "attacker"
            other.mkdir()
            target.symlink_to(other, target_is_directory=True)
        else:
            target.write_text("not a directory")
    with pytest.raises(PermissionError, match="unsafe runtime directory"):
        ensure_runtime_dir(path)
    if unsafe == "writable":
        assert target.stat().st_mode & 0o777 == 0o777
