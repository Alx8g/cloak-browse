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
