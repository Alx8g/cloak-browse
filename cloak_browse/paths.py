from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_cache_path


@dataclass(frozen=True)
class HarnessPaths:
    runtime_dir: Path
    tmp_dir: Path
    config_dir: Path


@dataclass(frozen=True)
class AppPaths:
    cache_dir: Path
    session_file: Path
    stop_file: Path

    def harness(self, session_id: str) -> HarnessPaths:
        runtime_root = Path("/tmp") if os.name != "nt" else Path(tempfile.gettempdir())
        runtime_name = (
            f"cloak-browse-{os.getuid()}" if os.name != "nt" else "cloak-browse"
        )
        runtime_dir = runtime_root / runtime_name / session_id
        private_dir = self.cache_dir / "harness" / session_id
        return HarnessPaths(
            runtime_dir=runtime_dir,
            tmp_dir=private_dir / "tmp",
            config_dir=private_dir / "config",
        )


def app_paths(environment: Mapping[str, str] | None = None) -> AppPaths:
    env = os.environ if environment is None else environment
    override = env.get("CLOAK_BROWSE_CACHE_DIR")
    cache_dir = (
        Path(override).expanduser().resolve()
        if override
        else Path(user_cache_path("cloak-browse", appauthor=False))
    )
    return AppPaths(
        cache_dir=cache_dir,
        session_file=cache_dir / "session.json",
        stop_file=cache_dir / "stop.json",
    )


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path, 0o700)
    return path


def ensure_runtime_dir(path: Path) -> Path:
    if os.name == "nt":
        return ensure_private_dir(path)
    for directory in (path.parent, path):
        with contextlib.suppress(FileExistsError):
            directory.mkdir(mode=0o700)
        info = directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o022
        ):
            raise PermissionError(
                f"unsafe runtime directory {directory}: must be a real directory "
                "owned by the current user and not group/world-writable"
            )
    return path
