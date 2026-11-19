from __future__ import annotations

import argparse
import math
from collections.abc import Callable, Sequence

from ._version import __version__
from .runtime import CloakBrowseRuntime, StartOptions
from .worker import force_utf8_output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cloak-browse",
        description="Stealth browser controlled by browser-harness",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    commands = parser.add_subparsers(dest="command")

    start = commands.add_parser(
        "start",
        help="Own the browser in the foreground, or use --background",
        description=(
            "Stay running as the browser owner. "
            "Use --background to return after readiness."
        ),
    )
    start.add_argument(
        "--background",
        action="store_true",
        help="Detach the owner and return only after browser and harness readiness",
    )
    start.add_argument(
        "--startup-timeout",
        type=_positive_float,
        default=120.0,
        help="Background readiness deadline in seconds (default: 120)",
    )
    start.add_argument("--proxy", help="Proxy URL (http://user:pass@host:port)")
    start.add_argument("--profile", help="Persistent profile directory path")
    start.add_argument(
        "--headless",
        action="store_true",
        help="Run headless (stealth patches still active)",
    )
    start.add_argument(
        "--humanize",
        action="store_true",
        help="Enable human-like mouse/keyboard behavior",
    )
    start.add_argument(
        "--backend",
        choices=["patchright", "playwright"],
        default="playwright",
        help="Deprecated compatibility option; only playwright is supported",
    )

    run = commands.add_parser("run", help="Run trusted Python against the browser")
    run.add_argument("code", help="Trusted Python code to execute")
    run.add_argument(
        "--lock-timeout",
        type=_lock_timeout,
        default=15.0,
        help="Maximum wait for another run (1 to 60 seconds; default: 15)",
    )
    run.add_argument(
        "--timeout",
        type=_run_timeout,
        default=None,
        help="Maximum execution time in seconds",
    )

    commands.add_parser("stop", help="Stop the managed browser and harness")

    status = commands.add_parser("status", help="Show detailed status")
    status.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Write exactly one JSON value to stdout",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    runtime_factory: Callable[[], CloakBrowseRuntime] = CloakBrowseRuntime,
) -> int:
    force_utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    runtime = runtime_factory()
    if args.command == "start":
        options = StartOptions(
            proxy=args.proxy,
            profile=args.profile,
            headless=args.headless,
            humanize=args.humanize,
            backend=args.backend,
        )
        if args.background:
            from .background import start_background

            return start_background(options, runtime.paths, args.startup_timeout)
        return runtime.start(options)
    if args.command == "run":
        return runtime.run(args.code, args.timeout, args.lock_timeout)
    if args.command == "stop":
        return runtime.stop()
    if args.command == "status":
        return runtime.status(args.json_output)
    parser.error(f"unknown command {args.command!r}")


def _positive_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("timeout must be greater than zero")
    return number


def _run_timeout(value: str) -> float:
    number = _positive_float(value)
    if number > 300:
        raise argparse.ArgumentTypeError("timeout must be at most 300 seconds")
    return number


def _lock_timeout(value: str) -> float:
    number = _positive_float(value)
    if not 1 <= number <= 60:
        raise argparse.ArgumentTypeError("lock timeout must be between 1 and 60")
    return number


if __name__ == "__main__":
    raise SystemExit(main())
