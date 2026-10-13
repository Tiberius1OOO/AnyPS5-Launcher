#!/usr/bin/env python3
"""AnyPS5 setup / installer engine — audit + install with detailed logging."""
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
        for row in _progress["steps"]:
            if row["id"] == step_id:
                row["status"] = status
                if detail:
                    row["detail"] = detail
                break
        _progress["done"] = sum(
            1 for r in _progress["steps"] if r["status"] in ("ok", "error", "skipped")
        )


def finish_progress(ok: bool) -> None:
    with _progress_lock:
        _progress["running"] = False
        _progress["ok"] = ok
        _progress["current"] = ""
        if ok:
            _progress["detail"] = "Setup finished"
        else:
            _progress["detail"] = "Setup aborted — see Console for [ABBRUCH] lines"


def get_progress() -> dict:
    with _progress_lock:
        return {
            "running": _progress["running"],
            "current": _progress["current"],
            "done": _progress["done"],
            "total": _progress["total"],
            "ok": _progress["ok"],
            "detail": _progress["detail"],
            "steps": [dict(s) for s in _progress["steps"]],
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
    status: str  # ok | missing | warn | error
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
    timestamp: str = ""

    def to_dict(self) -> dict:
        return {
            "checks": [asdict(c) for c in self.checks],
            "isAdmin": self.isAdmin,
            "repoRoot": self.repoRoot,
            "allRequiredOk": self.allRequiredOk,
            "missingRequired": self.missingRequired,
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
    except Exception as e:
        _log(log, f"  [ABBRUCH] {e}")
        return 1, str(e)


def refresh_path() -> None:
    machine = os.environ.get("Path") or ""
    # Rebuild from registry-like env
    machine = os.environ.get("PATH", "")
    try:
        import winreg

        def read(root, sub):
            try:
                with winreg.OpenKey(root, sub) as k:
                    val, _ = winreg.QueryValueEx(k, "Path")
                    return val
            except OSError:
                return ""

        m = read(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")
        u = read(winreg.HKEY_CURRENT_USER, "Environment")
        os.environ["Path"] = ";".join(p for p in (m, u, machine) if p)
        os.environ["PATH"] = os.environ["Path"]
    except Exception:
        pass


def add_user_path(directory: Path, log: LogFn | None = None) -> None:
    directory = directory.resolve()
    if not directory.is_dir():
        return
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
            try:
                current, _ = winreg.QueryValueEx(k, "Path")
            except OSError:
                current = ""
            parts = [p for p in current.split(";") if p]
            norm = str(directory)
            if any(p.rstrip("\\").lower() == norm.rstrip("\\").lower() for p in parts):
                _log(log, f"  [OK] PATH enthält bereits {norm}")
                return
            new = (current + ";" if current else "") + norm
            winreg.SetValueEx(k, "Path", 0, winreg.REG_EXPAND_SZ, new)
            _log(log, f"  [OK] Zu User-PATH hinzugefügt: {norm}")
    except Exception as e:
        _log(log, f"  [!] PATH konnte nicht gesetzt werden: {e}")
    # session
    os.environ["Path"] = str(directory) + ";" + os.environ.get("Path", "")
    os.environ["PATH"] = os.environ["Path"]


def tool_env() -> dict:
    env = os.environ.copy()
    parts = [
        str(CMAKE_BIN),
        str(WINLIBS_ROOT / "mingw64" / "bin"),
        env.get("Path", env.get("PATH", "")),
    ]
    env["Path"] = ";".join(p for p in parts if p)
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
        mark = {"ok": "OK", "missing": "FEHLT", "warn": "WARN", "error": "FEHLER"}.get(item.status, "?")
        _log(log, f"  [{mark}] {item.name}: {item.detail or item.status}")

    # Admin
    admin = is_admin()
    add(
        CheckItem(
            "admin",
            "Administrator-Rechte",
            "ok" if admin else "warn",
            "Ja" if admin else "Nein (manche Installer brauchen UAC)",
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
            winget or "Nicht gefunden — Fallback Chocolatey/manuell",
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
            choco or "Optional",
            required=False,
        )
    )

    # Git
    git = which("git")
    if git:
        code, ver = run(["git", "--version"], log=None)
        add(CheckItem("git", "Git", "ok", ver or git, required=True))
    else:
        add(CheckItem("git", "Git", "missing", "Nicht installiert (nötig für GitHub-Clone)", required=True))

    # Python
    py = which("python") or which("py")
    if py:
        code, ver = run([py if which("python") else "py", "--version"] if which("python") else ["py", "-3", "--version"], log=None)
        if which("python"):
            code, ver = run(["python", "--version"], log=None)
        else:
            code, ver = run(["py", "-3", "--version"], log=None)
        add(CheckItem("python", "Python 3", "ok", ver or py, required=True))
    else:
        add(CheckItem("python", "Python 3", "missing", "Nicht installiert (Launcher + Tools)", required=True))

    # CMake
    cmake = which("cmake")
    cmake_path = CMAKE_BIN / "cmake.exe"
    if not cmake and cmake_path.is_file():
        cmake = str(cmake_path)
    if cmake:
        code, ver = run([cmake, "--version"], log=None)
        pv = parse_cmake_version(ver)
        if pv and pv >= MIN_CMAKE:
            add(CheckItem("cmake", "CMake", "ok", ver.splitlines()[0] if ver else cmake, required=True))
        else:
            add(CheckItem("cmake", "CMake", "warn", f"Zu alt oder unbekannt: {ver}", required=True))
    else:
        add(CheckItem("cmake", "CMake", "missing", ">= 3.22.1 benötigt", required=True))

    # Ninja
    ninja = which("ninja")
    if ninja:
        code, ver = run(["ninja", "--version"], log=None)
        add(CheckItem("ninja", "Ninja", "ok", ver or ninja, required=True))
    else:
        add(CheckItem("ninja", "Ninja", "missing", "Build-Generator fehlt", required=True))

    # WinLibs GCC 15.2
    gcc = WINLIBS_ROOT / "mingw64" / "bin" / "gcc.exe"
    gxx = WINLIBS_ROOT / "mingw64" / "bin" / "g++.exe"
    if gcc.is_file() and gxx.is_file():
        code, ver = run([str(gcc), "--version"], log=None)
        line = ver.splitlines()[0] if ver else str(gcc)
        ok = "15.2.0" in line
        add(
            CheckItem(
                "winlibs",
                "WinLibs GCC 15.2.0",
                "ok" if ok else "warn",
                line,
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
                        f"Anderer GCC gefunden ({line}) — Projekt verlangt WinLibs 15.2.0",
                        required=True,
                    )
                )
        else:
            add(
                CheckItem(
                    "winlibs",
                    "WinLibs GCC 15.2.0",
                    "missing",
                    f"Erwartet unter {WINLIBS_ROOT}",
                    required=True,
                )
            )

    # Repo / git remote
    git_dir = repo_root / ".git"
    if git_dir.exists():
        code, remote = run(["git", "remote", "get-url", "origin"], cwd=repo_root, log=None)
        if code == 0:
            add(CheckItem("repo", "Git-Repository", "ok", remote, required=True, fixable=False))
        else:
            add(CheckItem("repo", "Git-Repository", "warn", "Lokal vorhanden, origin fehlt", required=True))
    else:
        add(
            CheckItem(
                "repo",
                "Git-Repository",
                "missing",
                f"Kein .git — Clone von {REPO_URL} nötig",
                required=True,
            )
        )

    # Submodules marker
    if (repo_root / ".gitmodules").is_file():
        sdl = repo_root / "3rdparty" / "SDL2" / "CMakeLists.txt"
        if sdl.is_file():
            add(CheckItem("submodules", "Submodules", "ok", "3rdparty vorhanden", required=True))
        else:
            add(
                CheckItem(
                    "submodules",
                    "Submodules",
                    "missing",
                    "gitmodules da, aber 3rdparty leer — submodule update nötig",
                    required=True,
                )
            )
    else:
        add(CheckItem("submodules", "Submodules", "missing", ".gitmodules fehlt", required=True))

    # Flask / launcher deps
    try:
        import flask  # noqa: F401

        add(CheckItem("flask", "Python Flask", "ok", "Installiert", required=True, fixable=True))
    except ImportError:
        add(CheckItem("flask", "Python Flask", "missing", "pip install flask", required=True))

    # Build artifacts (optional)
    relinker = repo_root / "build" / "core" / "relinker" / "relinker.exe"
    if relinker.is_file():
        add(CheckItem("build", "AnyPS5 Build", "ok", str(relinker), required=False, fixable=True))
    else:
        add(
            CheckItem(
                "build",
                "AnyPS5 Build",
                "warn",
                "Noch nicht gebaut — nach Setup Full Rebuild",
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
            add(CheckItem("github", "GitHub erreichbar", "ok", f"HTTP {resp.status}", required=False, fixable=False))
    except Exception as e:
        add(
            CheckItem(
                "github",
                "GitHub erreichbar",
                "error",
                f"Nicht erreichbar: {e}",
                required=False,
                fixable=False,
            )
        )

    missing = [c.name for c in checks if c.required and c.status in ("missing", "error")]
    report = AuditReport(
        checks=checks,
        isAdmin=admin,
        repoRoot=str(repo_root),
        allRequiredOk=len(missing) == 0,
        missingRequired=missing,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    _log(log, f"=== Audit fertig: {len(missing)} Pflicht-Probleme ===")
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
        _log(log, f"[!] choco fehlt — überspringe {name}")
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
    _log(log, "==> Installiere Git…")
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
    if which("python") or which("py"):
        _log(log, "[OK] Python bereits vorhanden")
        return True
    _log(log, "==> Installiere Python 3…")
    if install_winget_package("Python.Python.3.12", log):
        refresh_path()
        if which("python") or which("py"):
            return True
    if install_choco_package("python", log):
        refresh_path()
        if which("python") or which("py"):
            return True
    _log(log, "[ABBRUCH] Python-Installation fehlgeschlagen.")
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
    _log(log, "==> Installiere CMake…")
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
    _log(log, "==> Installiere Ninja…")
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
    _log(log, f"==> Lade WinLibs {WINLIBS_RELEASE}…")
    _log(log, f"  URL: {WINLIBS_URL}")
    WINLIBS_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.gettempdir()) / WINLIBS_ZIP
    try:
        _log(log, f"  Download nach {tmp}…")
        urllib.request.urlretrieve(WINLIBS_URL, tmp)  # noqa: S310
        _log(log, f"  Entpacke nach {WINLIBS_ROOT}…")
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
        _log(log, "[ABBRUCH] Git fehlt — kann nicht clonen")
        return False
    parent = repo_root.parent
    parent.mkdir(parents=True, exist_ok=True)
    if any(repo_root.iterdir()) if repo_root.exists() else False:
        # non-empty without .git
        if (repo_root / "launcher").is_dir() and (repo_root / "CMakeLists.txt").is_file():
            _log(log, "==> Initialisiere git in vorhandenem Ordner…")
            run(["git", "init"], cwd=repo_root, log=log)
            run(["git", "remote", "add", "origin", REPO_URL], cwd=repo_root, log=log)
            run(["git", "fetch", "origin"], cwd=repo_root, log=log, timeout=300)
            run(["git", "checkout", "-B", "main", "origin/main"], cwd=repo_root, log=log)
            return (repo_root / ".git").exists()
        _log(log, f"[ABBRUCH] {repo_root} ist nicht leer und kein Git-Repo")
        return False
    _log(log, f"==> Clone {REPO_URL} → {repo_root}")
    code, _ = run(["git", "clone", "--recursive", REPO_URL, str(repo_root)], log=log, timeout=600)
    return code == 0 and (repo_root / ".git").exists()


def ensure_submodules(repo_root: Path, log: LogFn) -> bool:
    if not (repo_root / ".gitmodules").is_file():
        _log(log, "[ABBRUCH] .gitmodules fehlt")
        return False
    _log(log, "==> git submodule update --init --recursive")
    env = tool_env()
    code, _ = run(
        ["git", "submodule", "update", "--init", "--recursive"],
        cwd=repo_root,
        log=log,
        timeout=600,
        env=env,
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
        _log(log, "[OK] No requirements.txt (frozen launcher?) — skip pip")
        return True
    if getattr(sys, "frozen", False):
        _log(log, "[OK] Frozen launcher — Python deps already bundled")
        return True
    py = which("python") or which("py")
    if not py:
        _log(log, "[ABBRUCH] Python fehlt für pip")
        return False
    if which("python"):
        cmd = ["python", "-m", "pip", "install", "-r", str(req)]
    else:
        cmd = ["py", "-3", "-m", "pip", "install", "-r", str(req)]
    code, _ = run(cmd, log=log, timeout=300)
    return code == 0


def _run_step(step_id: str, fn: Callable[[], bool], log: LogFn) -> bool:
    _set_step(step_id, "running", "In progress…")
    _log(log, f"==> Step: {SETUP_STEP_LABELS.get(step_id, step_id)}")
    try:
        ok = bool(fn())
    except Exception as e:
        ok = False
        _log(log, f"[ABBRUCH] {step_id}: {e}")
    if ok:
        _set_step(step_id, "ok", "Done")
    else:
        _set_step(step_id, "error", "Failed — see Console")
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
        _log(log, "# Nächster Schritt: Launcher → Full Rebuild")
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
