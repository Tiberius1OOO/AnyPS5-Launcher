"""Check GitHub releases and self-update the frozen AnyPS5 Launcher."""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from version import (
    LAUNCHER_RELEASES_API,
    LAUNCHER_VERSION,
    LAUNCHER_ZIP_NAME,
)

# User data that must survive a launcher folder replace
PRESERVE_DIR_NAMES = frozenset({"AnyPS5", "logs"})
PRESERVE_FILE_NAMES = frozenset(
    {
        "launcher-config.json",
        "setup-state.json",
        "input-profile.json",
        "anyps5-input.ini",
    }
)

# RLock: check_for_update / get_state nest under the same lock (threading.Lock deadlocks).
_lock = threading.RLock()
_state: dict[str, Any] = {
    "checked": False,
    "updateAvailable": False,
    "currentVersion": LAUNCHER_VERSION,
    "latestVersion": None,
    "tagName": None,
    "releaseName": None,
    "releaseNotes": None,
    "htmlUrl": None,
    "downloadUrl": None,
    "assetName": None,
    "error": None,
    "canAutoUpdate": False,
    "updating": False,
    "phase": None,  # checking | downloading | extracting | restarting | failed
    "downloadPercent": None,
}


def _log(msg: str) -> None:
    """Always visible in the launcher console window."""
    line = f"[update] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass


def parse_version(text: str | None) -> tuple[int, ...]:
    if not text:
        return (0,)
    m = re.search(r"(\d+(?:\.\d+)*)", str(text).strip())
    if not m:
        return (0,)
    return tuple(int(p) for p in m.group(1).split("."))


def is_newer(latest: str | None, current: str | None) -> bool:
    return parse_version(latest) > parse_version(current)


def install_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def can_auto_update() -> bool:
    return bool(getattr(sys, "frozen", False)) and (install_root() / "AnyPS5Launcher.exe").is_file()


def _snapshot_unlocked() -> dict[str, Any]:
    out = dict(_state)
    out["currentVersion"] = LAUNCHER_VERSION
    out["canAutoUpdate"] = can_auto_update()
    return out


def get_state() -> dict[str, Any]:
    with _lock:
        return _snapshot_unlocked()


def _set_phase(phase: str | None, *, percent: float | None = None, error: str | None = None) -> None:
    with _lock:
        _state["phase"] = phase
        if percent is not None or phase is None:
            _state["downloadPercent"] = percent
        if error is not None:
            _state["error"] = error


def _http_json(url: str, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"AnyPS5-Launcher/{LAUNCHER_VERSION}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def check_for_update(*, force: bool = False) -> dict[str, Any]:
    """Query GitHub latest release. Runs at most once per process unless force=True."""
    with _lock:
        # Never nest get_state() under a non-reentrant lock — that hung Update Now on 1.2.2/1.2.3.
        if _state["updating"]:
            return _snapshot_unlocked()
        if _state["checked"] and not force:
            return _snapshot_unlocked()
        # Reserve "checking" so concurrent polls do not stampede the API
        _state["phase"] = "checking"

    latest = None
    try:
        _log(f"checking GitHub releases API for updates (current v{LAUNCHER_VERSION})")
        data = _http_json(LAUNCHER_RELEASES_API)
        tag = str(data.get("tag_name") or "")
        latest = tag.lstrip("vV") or tag
        notes = str(data.get("body") or "").strip()
        if len(notes) > 1200:
            notes = notes[:1200].rstrip() + "…"
        download_url = None
        asset_name = None
        for asset in data.get("assets") or []:
            name = str(asset.get("name") or "")
            if name.lower() == LAUNCHER_ZIP_NAME.lower() or name.lower().endswith(".zip"):
                download_url = asset.get("browser_download_url")
                asset_name = name
                if name.lower() == LAUNCHER_ZIP_NAME.lower():
                    break
        available = is_newer(latest, LAUNCHER_VERSION) and bool(download_url)
        with _lock:
            # Do not clear an in-progress update phase if another thread started apply_update
            keep_phase = _state.get("updating")
            _state.update(
                {
                    "checked": True,
                    "updateAvailable": available,
                    "currentVersion": LAUNCHER_VERSION,
                    "latestVersion": latest,
                    "tagName": tag,
                    "releaseName": data.get("name") or tag,
                    "releaseNotes": notes,
                    "htmlUrl": data.get("html_url"),
                    "downloadUrl": download_url,
                    "assetName": asset_name,
                    "error": None if download_url or not available else "No zip asset on latest release",
                    "canAutoUpdate": can_auto_update(),
                }
            )
            if not keep_phase:
                _state["phase"] = None
                _state["downloadPercent"] = None
            if not download_url and is_newer(latest, LAUNCHER_VERSION):
                _state["updateAvailable"] = True
                _state["error"] = "Update found but zip asset is missing on GitHub"
                _state["canAutoUpdate"] = False
        if available:
            _log(f"update available: v{LAUNCHER_VERSION} -> v{latest} ({asset_name})")
        else:
            _log(f"no update (latest={latest or '?'})")
    except Exception as e:
        _log(f"update check failed: {e}")
        with _lock:
            keep_phase = _state.get("updating")
            _state.update(
                {
                    "checked": True,
                    "updateAvailable": False,
                    "currentVersion": LAUNCHER_VERSION,
                    "latestVersion": latest,
                    "error": str(e),
                    "canAutoUpdate": can_auto_update(),
                }
            )
            if not keep_phase:
                _state["phase"] = None
                _state["downloadPercent"] = None
    return get_state()


def _find_payload_dir(extract_root: Path) -> Path:
    direct = extract_root / "AnyPS5Launcher.exe"
    if direct.is_file():
        return extract_root
    nested = extract_root / "AnyPS5-Launcher" / "AnyPS5Launcher.exe"
    if nested.is_file():
        return nested.parent
    for exe in extract_root.rglob("AnyPS5Launcher.exe"):
        return exe.parent
    raise FileNotFoundError("AnyPS5Launcher.exe not found inside the update zip")


def _download(url: str, dest: Path, *, attempts: int = 3) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_err: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            _log(f"download attempt {attempt}/{attempts}: {url}")
            _set_phase("downloading", percent=0.0)
            req = urllib.request.Request(
                url,
                headers={"User-Agent": f"AnyPS5-Launcher/{LAUNCHER_VERSION}"},
            )
            # Per-operation timeout so a dead socket cannot hang forever
            with urllib.request.urlopen(req, timeout=45) as resp, open(dest, "wb") as out:
                total = int(resp.headers.get("Content-Length") or 0)
                got = 0
                last_pct = -1
                last_print = -1
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    got += len(chunk)
                    if total > 0:
                        pct = int(100 * got / total)
                        if pct != last_pct:
                            last_pct = pct
                            _set_phase("downloading", percent=float(pct))
                        if pct >= last_print + 5 or pct == 100:
                            last_print = pct
                            mb = got / (1024 * 1024)
                            total_mb = total / (1024 * 1024)
                            _log(f"downloaded {mb:.1f}/{total_mb:.1f} MB ({pct}%)")
                    else:
                        _set_phase("downloading", percent=None)
                        if got and got % (2 * 1024 * 1024) < 256 * 1024:
                            _log(f"downloaded {got / (1024 * 1024):.1f} MB …")
            if total > 0 and dest.stat().st_size < total:
                raise RuntimeError(
                    f"download incomplete: got {dest.stat().st_size} of {total} bytes"
                )
            if dest.stat().st_size < 1_000_000:
                raise RuntimeError(
                    f"download looks too small ({dest.stat().st_size} bytes) — not a release zip"
                )
            _set_phase("downloading", percent=100.0)
            _log(f"download complete: {dest} ({dest.stat().st_size} bytes)")
            return
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError, RuntimeError) as e:
            last_err = e
            _log(f"download attempt {attempt} failed: {e}")
            try:
                if dest.is_file():
                    dest.unlink()
            except OSError:
                pass
            if attempt < attempts:
                time.sleep(1.5 * attempt)
    raise RuntimeError(f"download failed after {attempts} attempts: {last_err}")


def _write_updater_script(
    target: Path,
    source: Path,
    *,
    exe_name: str = "AnyPS5Launcher.exe",
    pid: int,
) -> Path:
    """
    Visible CMD script: wait for this PID, robocopy files, start new launcher.
    Never uses the Windows `timeout` command (hangs without a TTY).
    """
    script = Path(tempfile.gettempdir()) / "anyps5-launcher-apply-update.cmd"
    log = Path(tempfile.gettempdir()) / "anyps5-launcher-update.log"
    preserve_dirs = " ".join(PRESERVE_DIR_NAMES)
    preserve_files = " ".join(PRESERVE_FILE_NAMES)
    target_s = str(target)
    source_s = str(source)
    log_s = str(log)
    content = f"""@echo off
setlocal EnableExtensions EnableDelayedExpansion
title AnyPS5 Launcher Updater
color 0B
set "TARGET={target_s}"
set "SOURCE={source_s}"
set "EXE={exe_name}"
set "WAITPID={pid}"
set "LOG={log_s}"
echo ============================================================
echo  AnyPS5 Launcher Updater
echo ============================================================
echo.
echo Target : !TARGET!
echo Source : !SOURCE!
echo Old PID: !WAITPID!
echo Log    : !LOG!
echo.
echo [%DATE% %TIME%] updater start PID=!WAITPID! > "!LOG!"
echo TARGET=!TARGET!>> "!LOG!"
echo SOURCE=!SOURCE!>> "!LOG!"

echo Waiting for old launcher process to exit...
set /a WAITCOUNT=0
:wait
set /a WAITCOUNT+=1
if !WAITCOUNT! GEQ 90 goto apply_anyway
rem ping delay ~1s. Do NOT use the Windows timeout command.
ping -n 2 127.0.0.1 >nul
tasklist /FI "PID eq !WAITPID!" /NH 2>nul | find "!WAITPID!" >nul
if errorlevel 1 goto exited
echo   still running... !WAITCOUNT!
echo [%DATE% %TIME%] still running PID !WAITPID! n=!WAITCOUNT!>> "!LOG!"
goto wait

:apply_anyway
echo Wait expired - applying files anyway.
echo [%DATE% %TIME%] wait expired n=!WAITCOUNT! - applying anyway>> "!LOG!"
goto apply

:exited
echo Old process exited.
echo [%DATE% %TIME%] PID !WAITPID! exited>> "!LOG!"

:apply
echo.
echo Copying new files into the launcher folder...
echo [%DATE% %TIME%] robocopy start>> "!LOG!"
robocopy "!SOURCE!" "!TARGET!" /E /IS /IT /R:5 /W:1 /XD {preserve_dirs} /XF {preserve_files}
set RC=!ERRORLEVEL!
echo [%DATE% %TIME%] robocopy exit=!RC!>> "!LOG!"
if !RC! GEQ 8 goto robofail

echo.
echo Starting new launcher...
echo [%DATE% %TIME%] starting "!TARGET!\\!EXE!">> "!LOG!"
start "" "!TARGET!\\!EXE!"
ping -n 3 127.0.0.1 >nul
rmdir /s /q "!SOURCE!" 2>nul
echo [%DATE% %TIME%] done>> "!LOG!"
echo.
echo Update finished. This window will close.
ping -n 3 127.0.0.1 >nul
del /f /q "%~f0" 2>nul
exit /b 0

:robofail
echo.
echo UPDATE FAILED - robocopy exit !RC!
echo See log: !LOG!
echo [%DATE% %TIME%] robocopy FAILED rc=!RC!>> "!LOG!"
echo.
pause
exit /b !RC!
"""
    script.write_text(content, encoding="utf-8", errors="replace")
    return script


def apply_update() -> dict[str, Any]:
    """
    Download latest zip, stage files, open a visible updater console, then
    the caller should exit this process so the updater can replace files.
    """
    if not can_auto_update():
        raise RuntimeError(
            "Auto-update only works from the released AnyPS5Launcher.exe. "
            "Download the zip from GitHub Releases instead."
        )

    state = check_for_update()
    url = state.get("downloadUrl")
    if not state.get("updateAvailable") or not url:
        raise RuntimeError(state.get("error") or "No launcher update available")

    with _lock:
        if _state["updating"]:
            raise RuntimeError("Update already in progress")
        _state["updating"] = True
        _state["error"] = None

    work = Path(tempfile.mkdtemp(prefix="anyps5-launcher-upd-"))
    zip_path = work / LAUNCHER_ZIP_NAME
    extract_dir = work / "extract"
    try:
        _log(f"install root: {install_root()}")
        _log(f"work dir: {work}")
        _download(str(url), zip_path)

        _set_phase("extracting")
        _log("extracting zip…")
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)
        payload = _find_payload_dir(extract_dir)
        _log(f"payload folder: {payload}")

        staged = work / "staged"
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)
        shutil.copytree(
            payload,
            staged,
            ignore=shutil.ignore_patterns(*PRESERVE_DIR_NAMES, *PRESERVE_FILE_NAMES),
        )
        _log(f"staged update files in {staged}")

        target = install_root()
        script = _write_updater_script(target, staged, pid=os.getpid())
        _set_phase("restarting")
        _log(f"opening visible updater console: {script}")

        # Visible new console. Do not use CREATE_NO_WINDOW / DETACHED_PROCESS.
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)

        # One visible console window running the updater script (no CREATE_NO_WINDOW).
        proc = subprocess.Popen(
            ["cmd.exe", "/c", str(script)],
            cwd=str(Path(tempfile.gettempdir())),
            creationflags=creationflags,
            close_fds=True,
        )
        _log(f"updater console opened pid={proc.pid}; this launcher will exit so files can be replaced")
        time.sleep(0.5)
        return {
            "ok": True,
            "restarting": True,
            "fromVersion": LAUNCHER_VERSION,
            "toVersion": state.get("latestVersion"),
            "target": str(target),
            "logFile": str(Path(tempfile.gettempdir()) / "anyps5-launcher-update.log"),
        }
    except Exception as e:
        _log(f"UPDATE FAILED: {e}")
        with _lock:
            _state["updating"] = False
            _state["phase"] = "failed"
            _state["downloadPercent"] = None
            _state["error"] = str(e)
        shutil.rmtree(work, ignore_errors=True)
        raise


def start_update_async() -> dict[str, Any]:
    """
    Kick off apply_update on a background thread and return immediately so the
    UI can keep polling. When successful, the process exits after spawning the updater.
    """
    with _lock:
        if _state["updating"]:
            raise RuntimeError("Update already in progress")

    def worker() -> None:
        try:
            result = apply_update()
            _log(
                f"update prepared v{result.get('fromVersion')} -> v{result.get('toVersion')}; "
                "exiting in 1s so the updater console can replace files"
            )
            time.sleep(1.0)
            os._exit(0)
        except Exception as e:
            _log(f"background update worker error: {e}")
            with _lock:
                _state["updating"] = False
                _state["phase"] = "failed"
                _state["error"] = str(e)

    threading.Thread(target=worker, daemon=True, name="launcher-self-update").start()
    _log("update worker started")
    return {
        "ok": True,
        "started": True,
        "fromVersion": LAUNCHER_VERSION,
        "currentVersion": LAUNCHER_VERSION,
        "message": "Download started — watch this console and the Updater window.",
    }
