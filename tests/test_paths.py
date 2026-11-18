from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest


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
