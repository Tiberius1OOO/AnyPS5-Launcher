#!/usr/bin/env python3
"""AnyPS5 setup / installer engine - audit + install with detailed logging."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

LogFn = Callable[[str], None]

SETUP_STEP_ORDER = [
    "git",
    "python",
    "cmake",
    "ninja",
    "winlibs",
    "repo",
    "submodules",
    "flask",
]
SETUP_STEP_LABELS = {
    "git": "Git",
    "python": "Python",
    "cmake": "CMake",
    "ninja": "Ninja",
    "winlibs": "WinLibs GCC",
    "repo": "Clone AnyPS5",
    "submodules": "Git submodules",
    "flask": "Launcher Python deps",
}

_progress_lock = __import__("threading").Lock()
_progress: dict = {
    "running": False,
    "current": "",
    "steps": [],
    "done": 0,
    "total": 0,
    "ok": None,
    "detail": "",
    "transfer": None,  # {percent, speed, received, total, phase, label}
}


def reset_progress(steps: list[str] | None = None) -> None:
    todo = list(steps) if steps else list(SETUP_STEP_ORDER)
    with _progress_lock:
        _progress["running"] = True
        _progress["current"] = ""
        _progress["done"] = 0
        _progress["total"] = len(todo)
        _progress["ok"] = None
        _progress["detail"] = ""
        _progress["transfer"] = None
        _progress["steps"] = [
            {
                "id": sid,
                "name": SETUP_STEP_LABELS.get(sid, sid),
                "status": "pending",
                "detail": "",
            }
            for sid in todo
        ]


def _set_step(step_id: str, status: str, detail: str = "") -> None:
    with _progress_lock:
        _progress["current"] = step_id if status == "running" else _progress.get("current", "")
        _progress["detail"] = detail
        if status != "running":
            _progress["transfer"] = None
        for row in _progress["steps"]:
            if row["id"] == step_id:
                row["status"] = status
                if detail:
                    row["detail"] = detail
                break
        _progress["done"] = sum(
            1 for r in _progress["steps"] if r["status"] in ("ok", "error", "skipped")
        )


def set_transfer(
    *,
    percent: float | None = None,
    speed: str = "",
    received: str = "",
    total: str = "",
    phase: str = "",
    label: str = "",
    step_id: str | None = None,
) -> None:
    """Update live download/clone transfer stats for the UI."""
    detail_parts = []
    if phase:
        detail_parts.append(phase)
    if percent is not None:
        detail_parts.append(f"{percent:.0f}%")
    if received and total:
        detail_parts.append(f"{received} / {total}")
    elif received:
        detail_parts.append(received)
    if speed:
        detail_parts.append(speed)
    detail = " · ".join(detail_parts) if detail_parts else (label or "Working…")
    with _progress_lock:
        _progress["transfer"] = {
            "percent": percent,
            "speed": speed,
            "received": received,
            "total": total,
            "phase": phase,
            "label": label or detail,
        }
        _progress["detail"] = detail
        sid = step_id or _progress.get("current") or ""
        if sid:
            for row in _progress["steps"]:
                if row["id"] == sid:
                    row["detail"] = detail
                    break


def finish_progress(ok: bool) -> None:
    with _progress_lock:
        _progress["running"] = False
        _progress["ok"] = ok
        _progress["current"] = ""
        _progress["transfer"] = None
        if ok:
            _progress["detail"] = "Setup finished"
        else:
            _progress["detail"] = "Setup aborted - see Console for [ABBRUCH] lines"


def sync_progress_from_audit(report: "AuditReport") -> dict:
    """When idle, show real detection state - not fake 'waiting to start' rows."""
    with _progress_lock:
        if _progress["running"]:
            return {
                "running": True,
                "current": _progress["current"],
                "done": _progress["done"],
                "total": _progress["total"],
                "ok": _progress["ok"],
                "detail": _progress["detail"],
                "steps": [dict(s) for s in _progress["steps"]],
            }

        by_id = {c.id: c for c in report.checks}
        steps: list[dict] = []
        for sid in SETUP_STEP_ORDER:
            check = by_id.get(sid)
            label = SETUP_STEP_LABELS.get(sid, sid)
            if check is None:
                steps.append(
                    {"id": sid, "name": label, "status": "pending", "detail": "Not checked"}
                )
                continue
            st = check.status
            if st == "ok":
                status, detail = "ok", check.detail or "Detected on this PC"
            elif st == "todo":
                status, detail = "todo", check.detail or "Will run on Install"
            elif st == "missing":
                status, detail = "missing", check.detail or "Missing - Install will try to fix"
            elif st == "warn":
                # Present but imperfect (e.g. wrong GCC) - still detected
                status, detail = "warn", check.detail or "Detected with warnings"
            elif st == "error":
                status, detail = "error", check.detail or "Error"
            else:
                status, detail = st, check.detail or ""
            steps.append({"id": sid, "name": label, "status": status, "detail": detail})

        done = sum(1 for s in steps if s["status"] == "ok")
        total = len(steps)
        remaining = total - done
        if remaining == 0:
            detail = "Everything detected on this PC - Install is optional (repair)"
            ok: bool | None = True
        else:
            detail = f"{done}/{total} ready - Install handles the rest"
            ok = None

        _progress["running"] = False
        _progress["current"] = ""
        _progress["done"] = done
        _progress["total"] = total
        _progress["ok"] = ok
        _progress["detail"] = detail
        _progress["transfer"] = None
        _progress["steps"] = steps
        return {
            "running": False,
            "current": "",
            "done": done,
            "total": total,
            "ok": ok,
            "detail": detail,
            "transfer": None,
            "steps": [dict(s) for s in steps],
        }


def get_progress() -> dict:
    with _progress_lock:
        steps = [dict(s) for s in _progress["steps"]]
        if not steps:
            # Before first audit: neutral placeholders (audit will replace immediately)
            steps = [
                {
                    "id": sid,
                    "name": SETUP_STEP_LABELS.get(sid, sid),
                    "status": "pending",
                    "detail": "Checking...",
                }
                for sid in SETUP_STEP_ORDER
            ]
            total = len(steps)
            done = 0
            detail = "Checking this PC..."
            transfer = None
        else:
            total = _progress["total"]
            done = _progress["done"]
            detail = _progress["detail"] or ""
            transfer = dict(_progress["transfer"]) if _progress.get("transfer") else None
        return {
            "running": _progress["running"],
            "current": _progress["current"],
            "done": done,
            "total": total,
            "ok": _progress["ok"],
            "detail": detail,
            "transfer": transfer,
            "steps": steps,
        }


REPO_URL = "https://github.com/boykopovar/AnyPS5.git"
GITHUB_PAGE = "https://github.com/boykopovar/AnyPS5"
WINLIBS_RELEASE = "15.2.0posix-14.0.0-ucrt-r7"
WINLIBS_ZIP = "winlibs-x86_64-posix-seh-gcc-15.2.0-mingw-w64ucrt-14.0.0-r7.zip"
WINLIBS_URL = (
    f"https://github.com/brechtsanders/winlibs_mingw/releases/download/"
    f"{WINLIBS_RELEASE}/{WINLIBS_ZIP}"
)
WINLIBS_ROOT = Path(r"C:\winlibs")
CMAKE_BIN = Path(r"C:\Program Files\CMake\bin")
MIN_CMAKE = (3, 22, 1)
SSL_CERT = Path(r"C:\Program Files\Git\mingw64\etc\ssl\certs\ca-bundle.crt")


@dataclass
class CheckItem:
    id: str
    name: str
    status: str  # ok | missing | warn | error | todo
    detail: str = ""
    required: bool = True
    fixable: bool = True


@dataclass
class AuditReport:
    checks: list[CheckItem] = field(default_factory=list)
    isAdmin: bool = False
    repoRoot: str = ""
    allRequiredOk: bool = False
    missingRequired: list[str] = field(default_factory=list)
    nextSteps: list[str] = field(default_factory=list)
    toolsOk: bool = False
    timestamp: str = ""

    def to_dict(self) -> dict:
        return {
            "checks": [asdict(c) for c in self.checks],
            "isAdmin": self.isAdmin,
            "repoRoot": self.repoRoot,
            "allRequiredOk": self.allRequiredOk,
            "missingRequired": self.missingRequired,
            "nextSteps": self.nextSteps,
            "toolsOk": self.toolsOk,
            "timestamp": self.timestamp,
        }


def _log(log: LogFn | None, msg: str) -> None:
    if log:
        log(msg)
    else:
        print(msg)


def is_admin() -> bool:
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def which(name: str) -> str | None:
    return shutil.which(name)


def _python_looks_usable(exe: Path) -> bool:
    """Reject WindowsApps stubs / broken installs; require a working Python 3."""
    try:
        resolved = exe.resolve()
    except Exception:
        resolved = exe
    if not resolved.is_file():
        return False
    low = str(resolved).lower()
    if "\\windowsapps\\" in low or low.endswith("\\py.exe"):
        return False
    try:
        p = subprocess.run(
            [str(resolved), "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            shell=False,
        )
        out = (p.stdout or "").strip().splitlines()
        if p.returncode != 0 or not out:
            return False
        # Accept 3.x
        return out[0].startswith("3.")
    except Exception:
        return False


def _registry_python_executables() -> list[Path]:
    out: list[Path] = []
    if os.name != "nt":
        return out
    try:
        import winreg
    except ImportError:
        return out

    roots = (
        (winreg.HKEY_CURRENT_USER, r"Software\Python\PythonCore"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Python\PythonCore"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Python\PythonCore"),
    )
    for hive, base in roots:
        try:
            with winreg.OpenKey(hive, base) as core:
                i = 0
                while True:
                    try:
                        ver = winreg.EnumKey(core, i)
                        i += 1
                    except OSError:
                        break
                    try:
                        with winreg.OpenKey(core, rf"{ver}\InstallPath") as ip:
                            for value_name in ("ExecutablePath", None):
                                try:
                                    if value_name is None:
                                        val, _ = winreg.QueryValueEx(ip, None)
                                    else:
                                        val, _ = winreg.QueryValueEx(ip, value_name)
                                except OSError:
                                    continue
                                raw = str(val or "").strip().strip('"')
                                if not raw:
                                    continue
                                p = Path(raw)
                                if p.is_dir():
                                    p = p / "python.exe"
                                if p.name.lower() == "python.exe" or p.suffix.lower() == ".exe":
                                    out.append(p)
                    except OSError:
                        continue
        except OSError:
            continue
    return out


def _common_python_executables() -> list[Path]:
    out: list[Path] = []
    local = Path(os.environ.get("LOCALAPPDATA") or "") / "Programs" / "Python"
    if local.is_dir():
        out.extend(sorted(local.glob("Python3*/python.exe"), reverse=True))
        out.extend(sorted(local.glob("Python*/python.exe"), reverse=True))
    for base in (
        Path(r"C:\Program Files\Python312"),
        Path(r"C:\Program Files\Python311"),
        Path(r"C:\Program Files\Python310"),
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Python312",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Python311",
        Path(r"C:\Python312"),
        Path(r"C:\Python311"),
    ):
        cand = base / "python.exe" if base.suffix.lower() != ".exe" else base
        if cand.name.lower() != "python.exe":
            cand = base / "python.exe"
        out.append(cand)
    return out


def find_python3_executable() -> Path | None:
    """
    Find a real python.exe for CMake FindPython3 / glslang.
    Reloads PATH from the registry first (important for the frozen .exe).
    """
    refresh_path()
    candidates: list[Path] = []

    for name in ("python", "python3"):
        hit = which(name)
        if hit:
            candidates.append(Path(hit))

    py = which("py")
    if py:
        try:
            p = subprocess.run(
                [py, "-3", "-c", "import sys; print(sys.executable)"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                shell=False,
            )
            line = (p.stdout or "").strip().splitlines()
            if p.returncode == 0 and line:
                candidates.append(Path(line[0].strip()))
        except Exception:
            pass

    candidates.extend(_registry_python_executables())
    candidates.extend(_common_python_executables())

    seen: set[str] = set()
    for cand in candidates:
        try:
            key = str(cand.resolve()).lower()
        except Exception:
            key = str(cand).lower()
        if key in seen:
            continue
        seen.add(key)
        if _python_looks_usable(cand):
            try:
                return cand.resolve()
            except Exception:
                return cand
    return None


def run(
    args: list[str],
    log: LogFn | None = None,
    cwd: str | Path | None = None,
    timeout: int | None = None,
    env: dict | None = None,
) -> tuple[int, str]:
    _log(log, "> " + " ".join(args))
    try:
        p = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            env=env or os.environ.copy(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        if out:
            for line in out.splitlines()[-40:]:
                _log(log, "  " + line)
        if p.returncode != 0:
            _log(log, f"  [ABBRUCH] Exit-Code {p.returncode}")
        return p.returncode, out
    except subprocess.TimeoutExpired:
        _log(log, "  [ABBRUCH] Timeout")
        return 1, "timeout"
    except OSError as e:
        msg = str(e)
        _log(log, f"  [ABBRUCH] {msg}")
        # WinError 8 = ERROR_NOT_ENOUGH_MEMORY; often a huge PATH / env block, not empty RAM
        if getattr(e, "winerror", None) == 8 or "nicht genügend Speicher" in msg.lower() or "not enough memory" in msg.lower():
            _log(
                log,
                "  [HINT] Windows could not start this process (WinError 8). "
                "Close heavy apps, free RAM / grow the page file, reboot, then retry. "
                "If PATH is very long, trim unused entries in System Environment Variables.",
            )
        return 1, msg
    except Exception as e:
        _log(log, f"  [ABBRUCH] {e}")
        return 1, str(e)


def _parse_git_progress_line(line: str) -> dict | None:
    """Parse git --progress stderr into transfer fields."""
    import re

    s = (line or "").strip().rstrip("\r")
    if not s:
        return None
    # Receiving objects:  45% (1234/2742), 12.34 MiB | 2.10 MiB/s
    m = re.search(
        r"(Receiving objects|Resolving deltas|Remote:\s*Compressing objects|"
        r"Updating files|Checking out files|Filtering content)\s*:\s*(\d+)%"
        r"(?:\s*\(([^)]+)\))?(?:,\s*([^|]+))?(?:\|\s*(.+))?",
        s,
        re.IGNORECASE,
    )
    if m:
        phase, pct, counts, size, speed = m.groups()
        received = ""
        total = ""
        if counts and "/" in counts:
            received, total = [x.strip() for x in counts.split("/", 1)]
        size = (size or "").strip().rstrip(",")
        return {
            "percent": float(pct),
            "phase": phase.replace("Remote:", "").strip(),
            "received": size or received,
            "total": total,
            "speed": (speed or "").strip(),
        }
    if "Cloning into" in s or "Submodule" in s:
        return {"percent": None, "phase": s[:120], "received": "", "total": "", "speed": ""}
    return None


def run_streaming(
    args: list[str],
    log: LogFn | None = None,
    cwd: str | Path | None = None,
    timeout: int | None = None,
    env: dict | None = None,
    on_line: Callable[[str], None] | None = None,
    step_id: str | None = None,
) -> tuple[int, str]:
    """Run a process and stream output. Handles git \\r progress updates."""
    _log(log, "> " + " ".join(args))
    merged: list[str] = []
    last_logged = ""
    try:
        run_env = dict(env or os.environ.copy())
        # Help git emit progress more promptly on Windows pipes
        run_env.setdefault("GIT_FLUSH", "1")
        p = subprocess.Popen(
            args,
            cwd=str(cwd) if cwd else None,
            env=run_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=0,
        )
        assert p.stdout is not None
        deadline = (datetime.now().timestamp() + timeout) if timeout else None
        buf = ""

        def emit(line: str) -> None:
            nonlocal last_logged
            line = line.strip()
            if not line:
                return
            merged.append(line)
            # Avoid flooding console with identical \\r progress spam
            if line != last_logged:
                _log(log, "  " + line)
                last_logged = line
            if on_line:
                on_line(line)
            parsed = _parse_git_progress_line(line)
            if parsed and step_id:
                set_transfer(step_id=step_id, **parsed)

        while True:
            if deadline and datetime.now().timestamp() > deadline:
                p.kill()
                _log(log, "  [ABBRUCH] Timeout")
                return 1, "timeout"
            ch = p.stdout.read(1)
            if ch == "":
                if buf:
                    emit(buf)
                    buf = ""
                break
            if ch in ("\r", "\n"):
                emit(buf)
                buf = ""
            else:
                buf += ch
        code = p.wait(timeout=30)
        if code != 0:
            _log(log, f"  [ABBRUCH] Exit-Code {code}")
        return code, "\n".join(merged[-80:])
    except OSError as e:
        msg = str(e)
        _log(log, f"  [ABBRUCH] {msg}")
        if getattr(e, "winerror", None) == 8 or "nicht genügend Speicher" in msg.lower() or "not enough memory" in msg.lower():
            _log(
                log,
                "  [HINT] Windows could not start this process (WinError 8). "
                "Close heavy apps, free RAM / grow the page file, reboot, then retry. "
                "If PATH is very long, trim unused entries in System Environment Variables.",
            )
        return 1, msg
    except Exception as e:
        _log(log, f"  [ABBRUCH] {e}")
        return 1, str(e)


def _fmt_bytes(n: float) -> str:
    units = ["B", "KB", "MB", "GB"]
    v = float(n)
    for u in units:
        if v < 1024 or u == units[-1]:
            return f"{v:.1f} {u}" if u != "B" else f"{int(v)} B"
        v /= 1024
    return f"{n:.0f} B"


def download_with_progress(url: str, dest: Path, log: LogFn, step_id: str) -> bool:
    """HTTP download with live percent + speed in Setup progress."""
    import time

    dest.parent.mkdir(parents=True, exist_ok=True)
    _log(log, f"  Download {url}")
    start = time.time()
    last_t = start
    last_b = 0

    def hook(block_num: int, block_size: int, total_size: int) -> None:
        nonlocal last_t, last_b
        got = block_num * block_size
        if total_size > 0:
            got = min(got, total_size)
        now = time.time()
        dt = max(now - last_t, 1e-3)
        speed = (got - last_b) / dt if got >= last_b else 0
        if now - last_t >= 0.25 or (total_size > 0 and got >= total_size):
            last_t = now
            last_b = got
            pct = (100.0 * got / total_size) if total_size > 0 else None
            set_transfer(
                step_id=step_id,
                percent=pct,
                phase="Downloading",
                received=_fmt_bytes(got),
                total=_fmt_bytes(total_size) if total_size > 0 else "",
                speed=f"{_fmt_bytes(speed)}/s",
            )

    try:
        urllib.request.urlretrieve(url, dest, reporthook=hook)  # noqa: S310
        elapsed = max(time.time() - start, 1e-3)
        size = dest.stat().st_size if dest.is_file() else 0
        set_transfer(
            step_id=step_id,
            percent=100,
            phase="Download complete",
            received=_fmt_bytes(size),
            total=_fmt_bytes(size),
            speed=f"avg {_fmt_bytes(size / elapsed)}/s",
        )
        return True
    except Exception as e:
        _log(log, f"[ABBRUCH] Download failed: {e}")
        return False


def _join_path_unique(*segments: str) -> str:
    """Join PATH pieces once each (case-insensitive). Avoids WinError 8 from a bloated env block."""
    seen: set[str] = set()
    out: list[str] = []
    for seg in segments:
        if not seg:
            continue
        for part in seg.split(";"):
            p = part.strip().strip('"')
            if not p:
                continue
            key = p.rstrip("\\/").lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
    return ";".join(out)


def refresh_path() -> None:
    """Rebuild process PATH from the registry only (never append the current PATH again).

    Older code did machine+user+current_PATH on every call, which duplicated entries until
    CreateProcess failed with WinError 8 (ERROR_NOT_ENOUGH_MEMORY) - often not real OOM.
    """
    try:
        import winreg

        def read(root, sub):
            try:
                with winreg.OpenKey(root, sub) as k:
                    val, _ = winreg.QueryValueEx(k, "Path")
                    return str(val or "")
            except OSError:
                return ""

        m = read(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")
        u = read(winreg.HKEY_CURRENT_USER, "Environment")
        # Keep entries this process already added that are not in the registry yet
        # (e.g. mid-install session prepends), but never re-stack the whole old PATH.
        current = os.environ.get("Path") or os.environ.get("PATH") or ""
        merged = _join_path_unique(m, u)
        # Preserve only prefixes that are not already covered (session-only dirs)
        merged_keys = {p.rstrip("\\/").lower() for p in merged.split(";") if p}
        extras: list[str] = []
        for part in current.split(";"):
            p = part.strip().strip('"')
            if not p:
                continue
            key = p.rstrip("\\/").lower()
            if key in merged_keys:
                continue
            # Skip obvious duplicates from a previously ballooned PATH
            if key in {e.rstrip("\\/").lower() for e in extras}:
                continue
            extras.append(p)
        # Cap extras so a already-corrupted process env cannot stay huge
        if len(extras) > 40:
            extras = extras[:40]
        path = _join_path_unique(";".join(extras), merged) if extras else merged
        os.environ["Path"] = path
        os.environ["PATH"] = path
    except Exception:
        # Still collapse duplicates in the live env if registry read fails
        cur = os.environ.get("Path") or os.environ.get("PATH") or ""
        if cur:
            collapsed = _join_path_unique(cur)
            os.environ["Path"] = collapsed
            os.environ["PATH"] = collapsed


def add_user_path(directory: Path, log: LogFn | None = None) -> None:
    directory = directory.resolve()
    if not directory.is_dir():
        return
    norm = str(directory)
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
            try:
                current, _ = winreg.QueryValueEx(k, "Path")
            except OSError:
                current = ""
            parts = [p for p in str(current).split(";") if p]
            if any(p.rstrip("\\").lower() == norm.rstrip("\\").lower() for p in parts):
                _log(log, f"  [OK] PATH enthaelt bereits {norm}")
            else:
                new = (str(current) + ";" if current else "") + norm
                winreg.SetValueEx(k, "Path", 0, winreg.REG_EXPAND_SZ, new)
                _log(log, f"  [OK] Zu User-PATH hinzugefuegt: {norm}")
    except Exception as e:
        _log(log, f"  [!] PATH konnte nicht gesetzt werden: {e}")
    # session (deduped)
    os.environ["Path"] = _join_path_unique(norm, os.environ.get("Path") or os.environ.get("PATH") or "")
    os.environ["PATH"] = os.environ["Path"]


def tool_env() -> dict:
    env = os.environ.copy()
    env["Path"] = _join_path_unique(
        str(CMAKE_BIN),
        str(WINLIBS_ROOT / "mingw64" / "bin"),
        env.get("Path", env.get("PATH", "")),
    )
    env["PATH"] = env["Path"]
    if SSL_CERT.exists():
        env["SSL_CERT_FILE"] = str(SSL_CERT)
        env["CURL_CA_BUNDLE"] = str(SSL_CERT)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def parse_cmake_version(text: str) -> tuple[int, int, int] | None:
    import re

    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def audit(repo_root: Path, log: LogFn | None = None) -> AuditReport:
    refresh_path()
    checks: list[CheckItem] = []

    def add(item: CheckItem) -> None:
        checks.append(item)
        mark = {
            "ok": "OK",
            "missing": "MISSING",
            "warn": "WARN",
            "error": "ERROR",
            "todo": "NEXT",
        }.get(item.status, "?")
        _log(log, f"  [{mark}] {item.name}: {item.detail or item.status}")

    # Admin
    admin = is_admin()
    add(
        CheckItem(
            "admin",
            "Administrator rights",
            "ok" if admin else "warn",
            "Yes" if admin else "Non-admin window (normal).",
            required=False,
            fixable=True,
        )
    )

    # winget
    winget = which("winget")
    add(
        CheckItem(
            "winget",
            "winget",
            "ok" if winget else "warn",
            "Found" if winget else "Not found - Chocolatey/manual fallback",
            required=False,
        )
    )

    # choco
    choco = which("choco")
    add(
        CheckItem(
            "choco",
            "Chocolatey",
            "ok" if choco else "warn",
            "Found" if choco else "Optional",
            required=False,
        )
    )

    def _short_ver(raw: str | None, fallback: str = "Found") -> str:
        line = (raw or "").splitlines()[0].strip() if raw else ""
        if not line:
            return fallback
        # Avoid dumping long absolute paths into the checklist UI
        if len(line) > 72 or (":\\" in line and "version" not in line.lower()):
            return fallback
        return line

    # Git
    git = which("git")
    if git:
        code, ver = run(["git", "--version"], log=None)
        add(CheckItem("git", "Git", "ok", _short_ver(ver, "Found"), required=True))
    else:
        add(CheckItem("git", "Git", "missing", "Not installed (needed to clone AnyPS5)", required=True))

    # Python (must be a real python.exe CMake/glslang can use - not only py.exe / Store stub)
    py_exe = find_python3_executable()
    if py_exe is not None:
        code, ver = run([str(py_exe), "--version"], log=None)
        add(
            CheckItem(
                "python",
                "Python 3",
                "ok",
                _short_ver(ver, str(py_exe)),
                required=True,
            )
        )
    else:
        add(
            CheckItem(
                "python",
                "Python 3",
                "missing",
                "No usable python.exe (PATH/registry). Needed for Build/glslang. Run Install / repair.",
                required=True,
            )
        )

    # CMake
    cmake = which("cmake")
    cmake_path = CMAKE_BIN / "cmake.exe"
    if not cmake and cmake_path.is_file():
        cmake = str(cmake_path)
    if cmake:
        code, ver = run([cmake, "--version"], log=None)
        pv = parse_cmake_version(ver)
        if pv and pv >= MIN_CMAKE:
            add(CheckItem("cmake", "CMake", "ok", _short_ver(ver, "Found"), required=True))
        else:
            add(CheckItem("cmake", "CMake", "warn", f"Too old or unknown: {_short_ver(ver, 'unknown')}", required=True))
    else:
        add(CheckItem("cmake", "CMake", "missing", ">= 3.22.1 required", required=True))

    # Ninja
    ninja = which("ninja")
    if ninja:
        code, ver = run(["ninja", "--version"], log=None)
        add(CheckItem("ninja", "Ninja", "ok", _short_ver(ver and f"ninja {ver}", "Found"), required=True))
    else:
        add(CheckItem("ninja", "Ninja", "missing", "Build generator missing", required=True))

    # WinLibs GCC 15.2
    gcc = WINLIBS_ROOT / "mingw64" / "bin" / "gcc.exe"
    gxx = WINLIBS_ROOT / "mingw64" / "bin" / "g++.exe"
    if gcc.is_file() and gxx.is_file():
        code, ver = run([str(gcc), "--version"], log=None)
        line = ver.splitlines()[0] if ver else ""
        ok = "15.2.0" in line
        add(
            CheckItem(
                "winlibs",
                "WinLibs GCC 15.2.0",
                "ok" if ok else "warn",
                "gcc 15.2.0" if ok else (line or "Found"),
                required=True,
            )
        )
    else:
        path_gcc = which("gcc")
        if path_gcc:
            code, ver = run(["gcc", "--version"], log=None)
            line = ver.splitlines()[0] if ver else path_gcc
            if "15.2.0" in line:
                add(CheckItem("winlibs", "WinLibs GCC 15.2.0", "ok", line, required=True))
            else:
                add(
                    CheckItem(
                        "winlibs",
                        "WinLibs GCC 15.2.0",
                        "warn",
                        f"Other GCC found ({line}) - project expects WinLibs 15.2.0",
                        required=True,
                    )
                )
        else:
            add(
                CheckItem(
                    "winlibs",
                    "WinLibs GCC 15.2.0",
                    "missing",
                    f"Expected under {WINLIBS_ROOT}",
                    required=True,
                )
            )

    # Paths with spaces are fine for users; Build remaps via short path / junction for windres
    if " " in str(repo_root):
        add(
            CheckItem(
                "path_spaces",
                "Install path",
                "ok",
                "Contains spaces (supported). Build uses a space-free alias for MinGW windres.",
                required=False,
            )
        )

    # Repo / git remote - "todo" = expected next Setup step, not a broken install
    git_dir = repo_root / ".git"
    if git_dir.exists():
        code, remote = run(["git", "remote", "get-url", "origin"], cwd=repo_root, log=None)
        if code == 0:
            add(CheckItem("repo", "AnyPS5 source", "ok", remote, required=True, fixable=False))
        else:
            add(
                CheckItem(
                    "repo",
                    "AnyPS5 source",
                    "warn",
                    "Folder exists but git origin is missing",
                    required=True,
                )
            )
    else:
        add(
            CheckItem(
                "repo",
                "AnyPS5 source",
                "todo",
                f"Not cloned yet - Install / repair will clone into {repo_root}",
                required=True,
            )
        )

    # Submodules marker
    if (repo_root / ".gitmodules").is_file():
        sdl = repo_root / "3rdparty" / "SDL2" / "CMakeLists.txt"
        if sdl.is_file():
            add(CheckItem("submodules", "Submodules", "ok", "3rdparty ready", required=True))
        else:
            add(
                CheckItem(
                    "submodules",
                    "Submodules",
                    "todo",
                    "gitmodules present, 3rdparty empty - Install runs submodule update",
                    required=True,
                )
            )
    elif git_dir.exists():
        add(
            CheckItem(
                "submodules",
                "Submodules",
                "todo",
                "Source present but submodules not initialized yet",
                required=True,
            )
        )
    else:
        add(
            CheckItem(
                "submodules",
                "Submodules",
                "todo",
                "After clone, Install initializes submodules automatically",
                required=True,
            )
        )

    # Flask / launcher deps (frozen build already bundles these)
    if getattr(sys, "frozen", False):
        add(CheckItem("flask", "Launcher Python deps", "ok", "Bundled in exe", required=True, fixable=False))
    else:
        try:
            import flask  # noqa: F401

            add(CheckItem("flask", "Launcher Python deps", "ok", "Installed", required=True, fixable=True))
        except ImportError:
            add(CheckItem("flask", "Launcher Python deps", "missing", "pip install flask", required=True))

    # Build artifacts (optional) - never a Setup install failure
    relinker = repo_root / "build" / "core" / "relinker" / "relinker.exe"
    if relinker.is_file():
        add(CheckItem("build", "AnyPS5 Build", "ok", "relinker.exe present", required=False, fixable=True))
    elif git_dir.exists():
        add(
            CheckItem(
                "build",
                "AnyPS5 Build",
                "todo",
                "Source ready - next step is Build -> Full Rebuild (not Setup)",
                required=False,
                fixable=True,
            )
        )
    else:
        add(
            CheckItem(
                "build",
                "AnyPS5 Build",
                "todo",
                "After Setup clones the source, use Build -> Full Rebuild",
                required=False,
                fixable=True,
            )
        )

    # Network / GitHub
    try:
        req = urllib.request.Request(
            "https://github.com/boykopovar/AnyPS5",
            method="HEAD",
            headers={"User-Agent": "AnyPS5-Setup"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            add(CheckItem("github", "GitHub reachable", "ok", f"HTTP {resp.status}", required=False, fixable=False))
    except Exception as e:
        add(
            CheckItem(
                "github",
                "GitHub reachable",
                "error",
                f"Unreachable: {e}",
                required=False,
                fixable=False,
            )
        )

    tool_ids = {"git", "python", "cmake", "ninja", "winlibs", "flask"}
    tool_missing = [
        c.name for c in checks if c.id in tool_ids and c.required and c.status in ("missing", "error")
    ]
    next_steps = [c.name for c in checks if c.status == "todo"]
    # Required setup complete when tools + repo + submodules are ok (build is optional)
    setup_blocking = [
        c.name
        for c in checks
        if c.required and c.status in ("missing", "error", "todo")
    ]
    report = AuditReport(
        checks=checks,
        isAdmin=admin,
        repoRoot=str(repo_root),
        allRequiredOk=len(setup_blocking) == 0,
        missingRequired=tool_missing,
        nextSteps=next_steps,
        toolsOk=len(tool_missing) == 0,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    sync_progress_from_audit(report)
    _log(log, f"=== Audit done: {len(tool_missing)} tool issue(s), {len(next_steps)} next step(s) ===")
    return report


def install_winget_package(pkg_id: str, log: LogFn, extra: list[str] | None = None) -> bool:
    if not which("winget"):
        _log(log, "[ABBRUCH] winget fehlt")
        return False
    args = [
        "winget",
        "install",
        "--id",
        pkg_id,
        "-e",
        "--accept-package-agreements",
        "--accept-source-agreements",
        "--disable-interactivity",
    ]
    if extra:
        args.extend(extra)
    code, _ = run(args, log=log, timeout=600)
    refresh_path()
    return code == 0


def install_choco_package(name: str, log: LogFn, params: str = "") -> bool:
    if not which("choco"):
        _log(log, f"[!] choco fehlt - ueberspringe {name}")
        return False
    # Auto-confirm non-elevated prompt
    cmd = f'echo Y| choco install {name} -y --no-progress --confirm'
    if params:
        cmd += f" {params}"
    _log(log, "> " + cmd)
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.stdout:
        for line in p.stdout.splitlines()[-30:]:
            _log(log, "  " + line)
    if p.stderr:
        for line in p.stderr.splitlines()[-15:]:
            _log(log, "  " + line)
    refresh_path()
    ok = p.returncode == 0
    if not ok:
        _log(log, f"  [ABBRUCH] choco {name} Exit {p.returncode}")
    return ok


def ensure_git(log: LogFn) -> bool:
    if which("git"):
        _log(log, "[OK] Git bereits vorhanden")
        return True
    _log(log, "==> Installiere Git...")
    if install_winget_package("Git.Git", log):
        refresh_path()
        if which("git"):
            return True
    if install_choco_package("git", log):
        refresh_path()
        if which("git"):
            return True
    _log(log, "[ABBRUCH] Git-Installation fehlgeschlagen. Installiere manuell: https://git-scm.com/download/win")
    return False


def ensure_python(log: LogFn) -> bool:
    refresh_path()
    exe = find_python3_executable()
    if exe is not None:
        _log(log, f"[OK] Python: {exe}")
        add_user_path(exe.parent, log)
        return True
    _log(log, "==> Installiere Python 3 (winget Python.Python.3.12)...")
    if install_winget_package("Python.Python.3.12", log):
        refresh_path()
        exe = find_python3_executable()
        if exe is not None:
            add_user_path(exe.parent, log)
            _log(log, f"[OK] Python after install: {exe}")
            return True
    if install_choco_package("python", log):
        refresh_path()
        exe = find_python3_executable()
        if exe is not None:
            add_user_path(exe.parent, log)
            _log(log, f"[OK] Python after install: {exe}")
            return True
    _log(
        log,
        "[ABBRUCH] Python 3 not found after install. "
        "Install from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), "
        "then close and reopen the launcher.",
    )
    return False


def ensure_cmake(log: LogFn) -> bool:
    refresh_path()
    cmake = which("cmake") or (str(CMAKE_BIN / "cmake.exe") if (CMAKE_BIN / "cmake.exe").is_file() else None)
    if cmake:
        code, ver = run([cmake, "--version"], log=None)
        pv = parse_cmake_version(ver)
        if pv and pv >= MIN_CMAKE:
            _log(log, f"[OK] CMake {ver.splitlines()[0]}")
            return True
    _log(log, "==> Installiere CMake...")
    if install_winget_package("Kitware.CMake", log):
        refresh_path()
        if which("cmake") or (CMAKE_BIN / "cmake.exe").is_file():
            return True
    if install_choco_package("cmake", log, '--installargs "ADD_CMAKE_TO_PATH=System"'):
        refresh_path()
        if which("cmake"):
            return True
    _log(log, "[ABBRUCH] CMake-Installation fehlgeschlagen.")
    return False


def ensure_ninja(log: LogFn) -> bool:
    if which("ninja"):
        _log(log, "[OK] Ninja vorhanden")
        return True
    _log(log, "==> Installiere Ninja...")
    if install_winget_package("Ninja-build.Ninja", log):
        refresh_path()
        if which("ninja"):
            return True
    if install_choco_package("ninja", log):
        refresh_path()
        if which("ninja"):
            return True
    _log(log, "[ABBRUCH] Ninja-Installation fehlgeschlagen.")
    return False


def ensure_winlibs(log: LogFn) -> bool:
    gcc = WINLIBS_ROOT / "mingw64" / "bin" / "gcc.exe"
    if gcc.is_file():
        code, ver = run([str(gcc), "--version"], log=None)
        line = ver.splitlines()[0] if ver else ""
        if "15.2.0" in line:
            _log(log, f"[OK] {line}")
            add_user_path(WINLIBS_ROOT / "mingw64" / "bin", log)
            return True
    _log(log, f"==> Lade WinLibs {WINLIBS_RELEASE}...")
    _log(log, f"  URL: {WINLIBS_URL}")
    WINLIBS_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.gettempdir()) / WINLIBS_ZIP
    try:
        _log(log, f"  Download nach {tmp}...")
        if not download_with_progress(WINLIBS_URL, tmp, log, "winlibs"):
            return False
        set_transfer(step_id="winlibs", phase="Extracting WinLibs", percent=None)
        _log(log, f"  Entpacke nach {WINLIBS_ROOT}...")
        with zipfile.ZipFile(tmp, "r") as zf:
            zf.extractall(WINLIBS_ROOT)
    except Exception as e:
        _log(log, f"[ABBRUCH] WinLibs Download/Extract: {e}")
        return False
    if not gcc.is_file():
        # search
        found = list(WINLIBS_ROOT.rglob("gcc.exe"))
        if found:
            _log(log, f"  gcc gefunden: {found[0]}")
        else:
            _log(log, "[ABBRUCH] gcc.exe nach Extract nicht gefunden")
            return False
    add_user_path(WINLIBS_ROOT / "mingw64" / "bin", log)
    code, ver = run([str(WINLIBS_ROOT / "mingw64" / "bin" / "gcc.exe"), "--version"], log=log)
    return code == 0


def ensure_repo(repo_root: Path, log: LogFn) -> bool:
    if (repo_root / ".git").exists():
        _log(log, f"[OK] Repo vorhanden: {repo_root}")
        code, _ = run(["git", "remote", "get-url", "origin"], cwd=repo_root, log=log)
        if code != 0:
            run(["git", "remote", "add", "origin", REPO_URL], cwd=repo_root, log=log)
        return True
    if not which("git"):
        _log(log, "[ABBRUCH] Git fehlt - kann nicht clonen")
        return False
    parent = repo_root.parent
    parent.mkdir(parents=True, exist_ok=True)
    env = tool_env()
    # Shallow-ish feedback: clone WITHOUT --recursive so this step finishes visibly;
    # submodules are a separate step with their own live progress.
    if any(repo_root.iterdir()) if repo_root.exists() else False:
        # non-empty without .git
        if (repo_root / "launcher").is_dir() and (repo_root / "CMakeLists.txt").is_file():
            _log(log, "==> Initialisiere git in vorhandenem Ordner...")
            run(["git", "init"], cwd=repo_root, log=log)
            run(["git", "remote", "add", "origin", REPO_URL], cwd=repo_root, log=log)
            set_transfer(step_id="repo", phase="Fetching origin", percent=0)
            code, _ = run_streaming(
                ["git", "fetch", "--progress", "origin"],
                cwd=repo_root,
                log=log,
                timeout=600,
                env=env,
                step_id="repo",
            )
            if code != 0:
                return False
            run(["git", "checkout", "-B", "main", "origin/main"], cwd=repo_root, log=log)
            return (repo_root / ".git").exists()
        _log(log, f"[ABBRUCH] {repo_root} ist nicht leer und kein Git-Repo")
        return False
    _log(log, f"==> Clone {REPO_URL} -> {repo_root}")
    set_transfer(step_id="repo", phase="Cloning AnyPS5", percent=0)
    code, _ = run_streaming(
        ["git", "clone", "--progress", REPO_URL, str(repo_root)],
        log=log,
        timeout=900,
        env=env,
        step_id="repo",
    )
    return code == 0 and (repo_root / ".git").exists()


def ensure_submodules(repo_root: Path, log: LogFn) -> bool:
    if not (repo_root / ".gitmodules").is_file():
        _log(log, "[ABBRUCH] .gitmodules fehlt")
        return False
    _log(log, "==> git submodule update --init --recursive")
    env = tool_env()
    set_transfer(step_id="submodules", phase="Updating submodules", percent=0)
    code, _ = run_streaming(
        ["git", "submodule", "update", "--init", "--recursive", "--progress"],
        cwd=repo_root,
        log=log,
        timeout=1800,
        env=env,
        step_id="submodules",
    )
    return code == 0


def ensure_flask(repo_root: Path, log: LogFn) -> bool:
    # Prefer standalone launcher requirements next to this module, then cloned repo.
    candidates = [
        Path(__file__).resolve().parent / "requirements.txt",
        Path(__file__).resolve().parent.parent / "requirements.txt",
        repo_root / "launcher" / "requirements.txt",
    ]
    req = next((p for p in candidates if p.is_file()), None)
    if req is None:
        _log(log, "[OK] No requirements.txt (frozen launcher?) - skip pip")
        return True
    if getattr(sys, "frozen", False):
        _log(log, "[OK] Frozen launcher - Python deps already bundled")
        return True
    py = which("python") or which("py")
    if not py:
        _log(log, "[ABBRUCH] Python fehlt fuer pip")
        return False
    if which("python"):
        cmd = ["python", "-m", "pip", "install", "-r", str(req)]
    else:
        cmd = ["py", "-3", "-m", "pip", "install", "-r", str(req)]
    code, _ = run(cmd, log=log, timeout=300)
    return code == 0


def _run_step(step_id: str, fn: Callable[[], bool], log: LogFn) -> bool:
    _set_step(step_id, "running", "In progress...")
    _log(log, f"==> Step: {SETUP_STEP_LABELS.get(step_id, step_id)}")
    try:
        ok = bool(fn())
    except Exception as e:
        ok = False
        _log(log, f"[ABBRUCH] {step_id}: {e}")
    if ok:
        _set_step(step_id, "ok", "Done")
    else:
        _set_step(step_id, "error", "Failed - see Console")
    return ok


def full_install(repo_root: Path, log: LogFn, steps: list[str] | None = None) -> bool:
    """Run full install pipeline. steps=None means all."""
    todo = list(steps) if steps else list(SETUP_STEP_ORDER)
    reset_progress(todo)
    _log(log, "########################################")
    _log(log, "# AnyPS5 Vollinstallation gestartet")
    _log(log, f"# Ziel: {repo_root}")
    _log(log, f"# Admin: {is_admin()}")
    _log(log, f"# Zeit: {datetime.now().isoformat(timespec='seconds')}")
    _log(log, "########################################")

    runners: dict[str, Callable[[], bool]] = {
        "git": lambda: ensure_git(log),
        "python": lambda: ensure_python(log),
        "cmake": lambda: ensure_cmake(log),
        "ninja": lambda: ensure_ninja(log),
        "winlibs": lambda: ensure_winlibs(log),
        "repo": lambda: ensure_repo(repo_root, log),
        "submodules": lambda: ensure_submodules(repo_root, log),
        "flask": lambda: ensure_flask(repo_root, log),
    }

    ok = True
    for step_id in todo:
        runner = runners.get(step_id)
        if not runner:
            _set_step(step_id, "skipped", "Unknown step")
            continue
        step_ok = _run_step(step_id, runner, log)
        ok = step_ok and ok
        if not step_ok:
            # Continue other independent steps? Plan: no silent skips; mark error and keep going
            # so later checklist rows still update, but overall fails.
            pass

    _log(log, "########################################")
    if ok:
        _log(log, "# ERGEBNIS: Installation OK")
        _log(log, "# Naechster Schritt: Launcher -> Full Rebuild")
    else:
        _log(log, "# ERGEBNIS: Installation mit FEHLERN abgebrochen")
        _log(log, "# Scroll nach oben zu [ABBRUCH]-Zeilen")
    _log(log, "########################################")

    report = audit(repo_root, log)
    final_ok = ok and report.allRequiredOk
    finish_progress(final_ok)

    # Prefer writing state beside the launcher (standalone) when possible
    state_candidates = [
        Path(__file__).resolve().parent / "setup-state.json",
        repo_root / "launcher" / "setup-state.json",
    ]
    for state_path in state_candidates:
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps(
                    {"ok": final_ok, "audit": report.to_dict(), "progress": get_progress()},
                    indent=2,
                ),
                encoding="utf-8",
            )
            break
        except Exception:
            continue
    return final_ok


if __name__ == "__main__":
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent

    def _print(m: str) -> None:
        print(m, flush=True)

    if len(sys.argv) > 2 and sys.argv[2] == "audit":
        audit(root, _print)
    else:
        sys.exit(0 if full_install(root, _print) else 1)
