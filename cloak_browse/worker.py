from __future__ import annotations

import contextlib
import os
import runpy
import sys
import time
from typing import Any, cast


def force_utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            cast(Any, stream).reconfigure(encoding="utf-8", errors="backslashreplace")


def _install_js_expect(h: Any) -> None:
    """Install a narrow typed JS boundary without changing `js` compatibility."""
    if getattr(h, "_cloak_js_expect_fixed", False):
        return

    expected_types = {
        "string": str,
        "list": list,
        "object": dict,
        "bool": bool,
    }

    def js_expect(
        expression: str, expected_type: str, target_id: str | None = None
    ) -> Any:
        """Evaluate JS and require an exact string, list, object, or bool result."""
        if not isinstance(expected_type, str):
            raise TypeError(
                "js_expect expected_type must be string, list, object, or bool"
            )
        expected = expected_types.get(expected_type)
        if expected is None:
            raise TypeError(
                "js_expect expected_type must be string, list, object, or bool"
            )
        value = h.js(expression, target_id=target_id)
        if type(value) is not expected:
            raise TypeError(
                f"js_expect expected {expected_type}, got {type(value).__name__}"
            )
        return value

    h.js_expect = js_expect
    h._cloak_js_expect_fixed = True


def _apply_helper_fixes(h: Any) -> None:
    """Preserve CloakBrowse's key, JS return, and native-dialog compatibility."""
    if getattr(h, "_cloak_helper_fixed", False):
        return
    _install_js_expect(h)

    _printable_keys = {
        ".": (190, "Period"),
        ",": (188, "Comma"),
        "/": (191, "Slash"),
        "\\": (220, "Backslash"),
        ";": (186, "Semicolon"),
        "'": (222, "Quote"),
        "[": (219, "BracketLeft"),
        "]": (221, "BracketRight"),
        "-": (189, "Minus"),
        "=": (187, "Equal"),
        "`": (192, "Backquote"),
        " ": (32, "Space"),
        "!": (49, "Digit1"),
        "@": (50, "Digit2"),
        "#": (51, "Digit3"),
        "$": (52, "Digit4"),
        "%": (53, "Digit5"),
        "^": (54, "Digit6"),
        "&": (55, "Digit7"),
        "*": (56, "Digit8"),
        "(": (57, "Digit9"),
        ")": (48, "Digit0"),
        "_": (189, "Minus"),
        "+": (187, "Equal"),
        "{": (219, "BracketLeft"),
        "}": (221, "BracketRight"),
        "|": (220, "Backslash"),
        ":": (186, "Semicolon"),
        '"': (222, "Quote"),
        "<": (188, "Comma"),
        ">": (190, "Period"),
        "?": (191, "Slash"),
        "~": (192, "Backquote"),
    }

    def _key_triplet(key: str) -> tuple[int, str, str]:
        if key in h._KEYS:
            return tuple(h._KEYS[key])
        if len(key) != 1:
            return 0, key, ""
        if "a" <= key <= "z" or "A" <= key <= "Z":
            upper = key.upper()
            return ord(upper), f"Key{upper}", key
        if "0" <= key <= "9":
            return ord(key), f"Digit{key}", key
        vk, code = _printable_keys.get(key, (ord(key), key))
        return vk, code, key

    def press_key(key: str, modifiers: int = 0) -> None:
        vk, code, text = _key_triplet(key)
        base = {
            "key": key,
            "code": code,
            "modifiers": modifiers,
            "windowsVirtualKeyCode": vk,
            "nativeVirtualKeyCode": vk,
        }
        h.cdp(
            "Input.dispatchKeyEvent",
            type="keyDown",
            **base,
            **({"text": text} if text else {}),
        )
        h.cdp("Input.dispatchKeyEvent", type="keyUp", **base)

    def has_executable_return_statement(expression: str) -> bool:
        i = 0
        n = len(expression)
        state = "code"
        quote = ""
        ignore_depths: list[int] = []
        pending_ignore_block = False
        while i < n:
            ch = expression[i]
            nxt = expression[i + 1] if i + 1 < n else ""
            if state == "code":
                if ch in ("'", '"', "`"):
                    state = "string"
                    quote = ch
                    i += 1
                    continue
                if ch == "/" and nxt == "/":
                    state = "line_comment"
                    i += 2
                    continue
                if ch == "/" and nxt == "*":
                    state = "block_comment"
                    i += 2
                    continue
                if expression.startswith("=>", i):
                    pending_ignore_block = True
                    i += 2
                    continue
                if expression.startswith("function", i):
                    before = expression[i - 1] if i > 0 else ""
                    after = expression[i + 8] if i + 8 < n else ""
                    if not (before == "_" or before.isalnum()) and not (
                        after == "_" or after.isalnum()
                    ):
                        pending_ignore_block = True
                    i += 8
                    continue
                if expression.startswith("class", i):
                    before = expression[i - 1] if i > 0 else ""
                    after = expression[i + 5] if i + 5 < n else ""
                    if not (before == "_" or before.isalnum()) and not (
                        after == "_" or after.isalnum()
                    ):
                        pending_ignore_block = True
                    i += 5
                    continue
                if ch == "{":
                    if pending_ignore_block:
                        ignore_depths.append(1)
                        pending_ignore_block = False
                    else:
                        ignore_depths = [d + 1 for d in ignore_depths]
                    i += 1
                    continue
                if ch == "}":
                    next_depths = []
                    for depth in ignore_depths:
                        if depth > 1:
                            next_depths.append(depth - 1)
                    ignore_depths = next_depths
                    i += 1
                    continue
                if expression.startswith("return", i):
                    before = expression[i - 1] if i > 0 else ""
                    after = expression[i + 6] if i + 6 < n else ""
                    if not (before == "_" or before.isalnum()) and not (
                        after == "_" or after.isalnum()
                    ):
                        j = i + 6
                        while j < n and expression[j].isspace():
                            j += 1
                        if not ignore_depths and (j >= n or expression[j] != ":"):
                            return True
                    i += 6
                    continue
                if not ch.isspace():
                    pending_ignore_block = False
                i += 1
                continue
            if state == "line_comment":
                if ch == "\n":
                    state = "code"
                i += 1
                continue
            if state == "block_comment":
                if ch == "*" and nxt == "/":
                    state = "code"
                    i += 2
                    continue
                i += 1
                continue
            if state == "string":
                if ch == "\\":
                    i += 2
                    continue
                if ch == quote:
                    state = "code"
                    quote = ""
                i += 1
                continue
        return False

    old_click_at_xy = h.click_at_xy

    def click_at_xy(x: float, y: float, button: str = "left", clicks: int = 1) -> Any:
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


def main() -> None:
    os.environ["BH_TELEMETRY"] = "0"
    force_utf8_output()
    import browser_harness.helpers as helpers

    _apply_helper_fixes(helpers)
    runpy.run_module("browser_harness.run", run_name="__main__")


if __name__ == "__main__":
    main()
