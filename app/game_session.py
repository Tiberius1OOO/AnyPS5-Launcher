"""Track a single running game process; prevent duplicate launches."""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010

_lock = threading.Lock()
_session: dict[str, Any] = {
    "running": False,
    "pid": None,
    "exe": "",
    "title": "",
    "cwd": "",
    "logPath": "",
    "startedAt": "",
    "finishedAt": "",
    "exitCode": None,
    "error": "",
}
_proc: subprocess.Popen | None = None
_log_handle = None
_monitor_stop = threading.Event()


def get_status() -> dict[str, Any]:
    with _lock:
        _refresh_unlocked()
        return dict(_session)


def is_running() -> bool:
    return bool(get_status().get("running"))


def _image_name(exe: str | Path) -> str:
    return Path(str(exe or "")).name


def _processes_for_exe(exe: str | Path) -> list[int]:
    """Return PIDs still running for this exact executable path (Windows)."""
    exe_path = Path(str(exe or ""))
    image = exe_path.name
    if not image or sys.platform != "win32":
        return []
    want = ""
    try:
        want = str(exe_path.resolve()).lower()
    except OSError:
        want = str(exe_path).lower()
    # Prefer WMI path match so python.exe / shared names don't false-positive
    try:
        r = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"Get-CimInstance Win32_Process -Filter \"Name='{image}'\" | "
                "Select-Object ProcessId,ExecutablePath | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            timeout=12,
        )
        import json

        raw = (r.stdout or "").strip()
        if raw:
            data = json.loads(raw)
            rows = data if isinstance(data, list) else [data]
            pids: list[int] = []
            for row in rows:
                path = str(row.get("ExecutablePath") or "").lower()
                pid = row.get("ProcessId")
                if pid is None:
                    continue
                if want and path and path != want:
                    continue
                if not path and want:
                    # Path unavailable — fall back to image-name match only when
                    # we already own a session for this exe
                    continue
                try:
                    pids.append(int(pid))
                except (TypeError, ValueError):
                    continue
            return pids
    except Exception:
        return []
    return []


def _processes_with_image(image: str) -> list[int]:
    """Legacy helper: PIDs for an image name (used by stop cleanup)."""
    if not image or sys.platform != "win32":
        return []
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            timeout=8,
        )
    except Exception:
        return []
    pids: list[int] = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line or line.startswith("INFO:"):
            continue
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) >= 2 and parts[0].lower() == image.lower():
            try:
                pids.append(int(parts[1]))
            except ValueError:
                continue
    return pids


def _refresh_unlocked() -> None:
    """Update running flag from Popen + leftover same-path processes."""
    global _proc
    if not _session.get("running"):
        return
    exe = str(_session.get("exe") or "")
    proc_alive = _proc is not None and _proc.poll() is None
    leftovers = _processes_for_exe(exe) if exe else []
    if proc_alive or leftovers:
        if not proc_alive and leftovers and _session.get("pid") not in leftovers:
            # Parent stub exited; keep session locked on a child with same path
            _session["pid"] = leftovers[0]
        return
    code = None
    if _proc is not None:
        code = _proc.poll()
    _finalize_unlocked(code, kill_image=False)


def _finalize_unlocked(exit_code: int | None, *, kill_image: bool = False) -> None:
    global _proc, _log_handle
    image = _image_name(_session.get("exe") or "")
    _session["running"] = False
    _session["exitCode"] = exit_code
    _session["finishedAt"] = datetime.now(timezone.utc).isoformat()
    if _log_handle is not None:
        try:
            _log_handle.close()
        except Exception:
            pass
        _log_handle = None
    if kill_image and image and sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", image, "/T"],
                capture_output=True,
                creationflags=CREATE_NO_WINDOW,
                timeout=8,
            )
        except Exception:
            pass
    _proc = None


def _monitor(proc: subprocess.Popen) -> None:
    """Wait for the root process, then until no same-named processes remain."""
    try:
        code = proc.wait()
    except Exception:
        code = -1
    # Parent may be a short-lived stub — wait until that exact exe path is gone
    exe = ""
    with _lock:
        exe = str(_session.get("exe") or "")
    deadline = time.time() + 86400  # safety: 24h
    while time.time() < deadline and not _monitor_stop.is_set():
        leftovers = _processes_for_exe(exe) if exe else []
        if not leftovers:
            break
        time.sleep(1.0)
    with _lock:
        if _proc is proc or (_session.get("running") and str(_session.get("exe") or "") == exe):
            _finalize_unlocked(code, kill_image=False)
            try:
                import applog

                applog.info(
                    "Game process exited",
                    f"pid={proc.pid} code={code} title={_session.get('title')!r}",
                )
            except Exception:
                pass


def start_game(
    exe: Path,
    *,
    title: str = "",
    env: dict | None = None,
    log_dir: Path | None = None,
    hide_console: bool = False,
    args: list[str] | None = None,
) -> dict[str, Any]:
    """Start exe once. Raises RuntimeError if a game is already running."""
    global _proc, _log_handle
    exe = Path(exe)
    if not exe.is_file():
        raise FileNotFoundError(f"Executable not found: {exe}")

    with _lock:
        _refresh_unlocked()
        if _session.get("running"):
            raise RuntimeError(
                f"Already running: {_session.get('title') or Path(_session.get('exe') or '').name} "
                f"(PID {_session.get('pid')})"
            )
        # Block orphaned instances of this exact game exe (not shared runtimes)
        import os

        alive = [p for p in _processes_for_exe(exe) if p != os.getpid()]
        if alive:
            raise RuntimeError(
                f"Already running: {title or exe.stem} (PID {alive[0]}). "
                "Close the game before starting again."
            )

        log_path = ""
        # Visible console: let the game own stdout/stderr so the user can watch init.
        # Hidden console: capture into logs\game-*.log instead.
        stdout_target = None
        stderr_target = None
        if log_dir is not None:
            log_dir = Path(log_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in (title or exe.stem))[:60]
            log_path = str(log_dir / f"game-{safe or 'run'}-{stamp}.log")
            _log_handle = open(log_path, "w", encoding="utf-8", errors="replace")
            _log_handle.write(f"Launch {exe}\nCWD {exe.parent}\nhide_console={hide_console}\n---\n")
            _log_handle.flush()
            if hide_console:
                stdout_target = _log_handle
                stderr_target = subprocess.STDOUT
            else:
                _log_handle.write(
                    "(Game console is visible — live output is in the game console window.)\n"
                )
                _log_handle.flush()

        flags = 0
        if sys.platform == "win32":
            # Never use STARTF_USESHOWWINDOW/SW_HIDE — that hides the game GUI too.
            if hide_console:
                flags = CREATE_NO_WINDOW
            else:
                flags = CREATE_NEW_CONSOLE

        _monitor_stop.clear()
        cmdline = [str(exe), *(args or [])]
        popen_kwargs: dict[str, Any] = {
            "cwd": str(exe.parent),
            "env": env,
            "creationflags": flags,
        }
        if hide_console:
            popen_kwargs["stdout"] = stdout_target
            popen_kwargs["stderr"] = stderr_target
        proc = subprocess.Popen(cmdline, **popen_kwargs)
        _proc = proc
        _session.update(
            {
                "running": True,
                "pid": proc.pid,
                "exe": str(exe),
                "title": title or exe.stem,
                "cwd": str(exe.parent),
                "logPath": log_path,
                "startedAt": datetime.now(timezone.utc).isoformat(),
                "finishedAt": "",
                "exitCode": None,
                "error": "",
            }
        )
        t = threading.Thread(target=_monitor, args=(proc,), daemon=True, name="game-monitor")
        t.start()
        return dict(_session)


def stop_game(force: bool = True) -> dict[str, Any]:
    with _lock:
        proc = _proc
        image = _image_name(_session.get("exe") or "")
        if not proc and not _session.get("running") and not (image and _processes_with_image(image)):
            return dict(_session)
        try:
            if force and sys.platform == "win32":
                if proc is not None:
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(proc.pid), "/T"],
                        capture_output=True,
                        creationflags=CREATE_NO_WINDOW,
                        timeout=10,
                    )
                if image:
                    subprocess.run(
                        ["taskkill", "/F", "/IM", image, "/T"],
                        capture_output=True,
                        creationflags=CREATE_NO_WINDOW,
                        timeout=10,
                    )
            elif proc is not None:
                proc.terminate()
        except Exception as e:
            _session["error"] = str(e)
        _monitor_stop.set()
    for _ in range(30):
        if not is_running():
            break
        time.sleep(0.1)
    with _lock:
        if _session.get("running"):
            _finalize_unlocked(-1, kill_image=True)
        return dict(_session)
