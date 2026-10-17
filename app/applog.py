"""Application-wide log levels → logs/launcher.log (+ stderr for warning/error)."""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LEVELS = {
    "debug": 10,
    "info": 20,
    "warning": 30,
    "error": 40,
}

DEFAULT_LEVEL = "info"

_lock = threading.Lock()
_level_name = DEFAULT_LEVEL
_level_num = LEVELS[DEFAULT_LEVEL]
_log_path: Path | None = None


def configure(log_dir: Path, level_name: str | None = None) -> None:
    global _log_path, _level_name, _level_num
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    with _lock:
        _log_path = log_dir / "launcher.log"
        if level_name:
            _set_level_unlocked(level_name)


def set_level(level_name: str) -> str:
    with _lock:
        return _set_level_unlocked(level_name)


def _set_level_unlocked(level_name: str) -> str:
    global _level_name, _level_num
    key = (level_name or DEFAULT_LEVEL).strip().lower()
    if key not in LEVELS:
        key = DEFAULT_LEVEL
    _level_name = key
    _level_num = LEVELS[key]
    return _level_name


def get_level() -> str:
    with _lock:
        return _level_name


def get_log_path() -> str:
    with _lock:
        return str(_log_path) if _log_path else ""


def _emit(level_name: str, message: str, detail: Any = None) -> None:
    num = LEVELS.get(level_name, 20)
    with _lock:
        if num < _level_num:
            return
        path = _log_path
        current = _level_name
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    extra = ""
    if detail is not None and detail != "":
        extra = f" | {detail}"
    line = f"{ts} [{level_name.upper():7}] {message}{extra}"
    if path:
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
    # Always mirror warnings/errors to console; debug/info only when level allows
    if num >= LEVELS["warning"] or current == "debug":
        try:
            print(line, flush=True)
        except Exception:
            pass


def debug(message: str, detail: Any = None) -> None:
    _emit("debug", message, detail)


def info(message: str, detail: Any = None) -> None:
    _emit("info", message, detail)


def warning(message: str, detail: Any = None) -> None:
    _emit("warning", message, detail)


def error(message: str, detail: Any = None) -> None:
    _emit("error", message, detail)
