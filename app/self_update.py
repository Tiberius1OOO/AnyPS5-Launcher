"""Check GitHub releases and self-update the frozen AnyPS5 Launcher."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
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

_lock = threading.Lock()
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
}


def parse_version(text: str | None) -> tuple[int, ...]:
    """Parse 'v1.1.4' / '1.1.4' into a comparable tuple."""
    if not text:
        return (0,)
    m = re.search(r"(\d+(?:\.\d+)*)", str(text).strip())
    if not m:
        return (0,)
    return tuple(int(p) for p in m.group(1).split("."))


def is_newer(latest: str | None, current: str | None) -> bool:
    return parse_version(latest) > parse_version(current)


def install_root() -> Path:
    """Folder that contains AnyPS5Launcher.exe (or the app/ folder in dev)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def can_auto_update() -> bool:
    return bool(getattr(sys, "frozen", False)) and (install_root() / "AnyPS5Launcher.exe").is_file()


def get_state() -> dict[str, Any]:
    with _lock:
        out = dict(_state)
        out["currentVersion"] = LAUNCHER_VERSION
        out["canAutoUpdate"] = can_auto_update()
        return out


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
        if _state["checked"] and not force:
            return get_state()
        if _state["updating"]:
            return get_state()

    error: str | None = None
    latest = None
    try:
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
            if not download_url and is_newer(latest, LAUNCHER_VERSION):
                _state["updateAvailable"] = True
                _state["error"] = "Update found but zip asset is missing on GitHub"
                _state["canAutoUpdate"] = False
    except Exception as e:
        error = str(e)
        with _lock:
            _state.update(
                {
                    "checked": True,
                    "updateAvailable": False,
                    "currentVersion": LAUNCHER_VERSION,
                    "latestVersion": latest,
                    "error": error,
                    "canAutoUpdate": can_auto_update(),
                }
            )
    return get_state()


def _find_payload_dir(extract_root: Path) -> Path:
    """Locate the folder that contains AnyPS5Launcher.exe inside the unzipped release."""
    direct = extract_root / "AnyPS5Launcher.exe"
    if direct.is_file():
        return extract_root
    nested = extract_root / "AnyPS5-Launcher" / "AnyPS5Launcher.exe"
    if nested.is_file():
        return nested.parent
    for exe in extract_root.rglob("AnyPS5Launcher.exe"):
        return exe.parent
    raise FileNotFoundError("AnyPS5Launcher.exe not found inside the update zip")


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": f"AnyPS5-Launcher/{LAUNCHER_VERSION}"},
    )
    with urllib.request.urlopen(req, timeout=180) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out, length=1024 * 256)


def _write_updater_script(target: Path, source: Path, exe_name: str = "AnyPS5Launcher.exe") -> Path:
    """Write a .cmd that waits for this process to exit, copies files, restarts."""
    script = Path(tempfile.gettempdir()) / "anyps5-launcher-apply-update.cmd"
    # Use delayed expansion carefully; keep paths quoted.
    preserve_dirs = " ".join(PRESERVE_DIR_NAMES)
    preserve_files = " ".join(PRESERVE_FILE_NAMES)
    content = f"""@echo off
setlocal EnableExtensions
set "TARGET={target}"
set "SOURCE={source}"
set "EXE={exe_name}"
echo AnyPS5 Launcher updater
echo Waiting for launcher to exit...
:wait
timeout /t 1 /nobreak >nul
tasklist /FI "IMAGENAME eq %EXE%" 2>nul | find /I "%EXE%" >nul
if not errorlevel 1 goto wait

echo Applying update files...
robocopy "%SOURCE%" "%TARGET%" /E /NFL /NDL /NJH /NJS /nc /ns /np /XD {preserve_dirs} /XF {preserve_files}
set "RC=%ERRORLEVEL%"
if %RC% GEQ 8 (
  echo [ERROR] robocopy failed with code %RC%
  pause
  exit /b %RC%
)

echo Starting new launcher...
start "" "%TARGET%\\%EXE%"
timeout /t 2 /nobreak >nul
rmdir /s /q "%SOURCE%" 2>nul
del /f /q "%~f0" 2>nul
exit /b 0
"""
    script.write_text(content, encoding="utf-8")
    return script


def apply_update() -> dict[str, Any]:
    """Download latest zip, stage files, spawn updater, then caller should exit."""
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

    work = Path(tempfile.mkdtemp(prefix="anyps5-launcher-upd-"))
    zip_path = work / LAUNCHER_ZIP_NAME
    extract_dir = work / "extract"
    try:
        _download(str(url), zip_path)
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)
        payload = _find_payload_dir(extract_dir)
        # Stage a clean copy so the updater can delete the temp tree afterward
        staged = work / "staged"
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)
        shutil.copytree(
            payload,
            staged,
            ignore=shutil.ignore_patterns(*PRESERVE_DIR_NAMES, *PRESERVE_FILE_NAMES),
        )
        target = install_root()
        script = _write_updater_script(target, staged)
        # Detached so this process can exit while the script keeps running
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
        subprocess.Popen(
            ["cmd.exe", "/c", str(script)],
            cwd=str(Path(tempfile.gettempdir())),
            creationflags=creationflags,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {
            "ok": True,
            "restarting": True,
            "fromVersion": LAUNCHER_VERSION,
            "toVersion": state.get("latestVersion"),
            "target": str(target),
        }
    except Exception:
        with _lock:
            _state["updating"] = False
        shutil.rmtree(work, ignore_errors=True)
        raise
