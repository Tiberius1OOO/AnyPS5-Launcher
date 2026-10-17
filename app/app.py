#!/usr/bin/env python3
"""AnyPS5 graphical launcher backend."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request

def _resolve_dirs() -> tuple[Path, Path]:
    """Return (bundle_dir for templates/static, data_dir for config)."""
    if getattr(sys, "frozen", False):
        bundle = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
        data = Path(sys.executable).resolve().parent
        return bundle, data
    here = Path(__file__).resolve().parent
    return here, here


LAUNCHER_DIR, DATA_DIR = _resolve_dirs()
if str(LAUNCHER_DIR) not in sys.path:
    sys.path.insert(0, str(LAUNCHER_DIR))

from version import LAUNCHER_GITHUB_URL, LAUNCHER_RELEASES_PAGE, LAUNCHER_VERSION  # noqa: E402

CONFIG_PATH = DATA_DIR / "launcher-config.json"
INPUT_INI_PATH = DATA_DIR / "anyps5-input.ini"
INPUT_PROFILE_PATH = DATA_DIR / "input-profile.json"
LOGS_DIR = DATA_DIR / "logs"
LAST_JOB_LOG = LOGS_DIR / "last-job.log"

import applog  # noqa: E402
import library as library_mod  # noqa: E402

applog.configure(LOGS_DIR)
library_mod.configure_cache(DATA_DIR)


def _is_anyps5_tree(path: Path) -> bool:
    try:
        return (path / ".git").exists() or (path / "CMakeLists.txt").is_file()
    except Exception:
        return False


def detect_default_anyps5_root() -> Path:
    """Always AnyPS5 next to the running launcher (local scope for GitHub users)."""
    env = str(os.environ.get("ANYPS5_ROOT") or "").strip().strip('"')
    if env:
        return Path(env)
    return (DATA_DIR / "AnyPS5").resolve()


def get_repo_root() -> Path:
    cfg = load_config()
    raw = str(cfg.get("anyps5Root") or "").strip().strip('"')
    local = detect_default_anyps5_root()
    if raw:
        chosen = Path(raw)
        # Keep installs inside the launcher folder unless ANYPS5_ROOT was set.
        # (Ignore old configs that pointed at a sibling tree elsewhere.)
        if not str(os.environ.get("ANYPS5_ROOT") or "").strip():
            try:
                chosen.resolve().relative_to(DATA_DIR.resolve())
            except ValueError:
                cfg["anyps5Root"] = str(local)
                save_config(cfg)
                return local
        return chosen
    cfg["anyps5Root"] = str(local)
    save_config(cfg)
    return local


def get_build_dir() -> Path:
    return get_repo_root() / "build"


def get_relinker() -> Path:
    return get_build_dir() / "core" / "relinker" / "relinker.exe"


def get_libs_dir() -> Path:
    return get_build_dir() / "core" / "libs" / "libs"


WINLIBS_BIN = Path(r"C:\winlibs\mingw64\bin")
CMAKE_BIN = Path(r"C:\Program Files\CMake\bin")
SSL_CERT = Path(r"C:\Program Files\Git\mingw64\etc\ssl\certs\ca-bundle.crt")
GITHUB_URL = "https://github.com/boykopovar/AnyPS5"
GITHUB_README_RAW = "https://raw.githubusercontent.com/boykopovar/AnyPS5/main/README.md"

# Required next to Windows .prx (and often next to the .exe). LoadLibraryEx uses
# LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | DEFAULT_DIRS — not PATH — so a system-wide
# MinGW install is ignored. Missing these → GetLastError 126.
MINGW_RUNTIME_DLLS = (
    "libgcc_s_seh-1.dll",
    "libstdc++-6.dll",
    "libwinpthread-1.dll",
)

# Do not copy into app0: modules are converted by the relinker; skip prior outputs too.
APP0_SKIP_DIR_NAMES = frozenset(
    {
        "sce_module",
        "sce_modules",
        "prx",
        "app0",
        "libs",
        "anyps5-out",
        ".git",
        "__pycache__",
        "_self_backup",
        "_duplex_",
    }
)
APP0_SKIP_FILE_NAMES = frozenset({"eboot.bin"})

app = Flask(
    __name__,
    template_folder=str(LAUNCHER_DIR / "templates"),
    static_folder=str(LAUNCHER_DIR / "static"),
)

_job_lock = threading.Lock()
_job: dict = {
    "running": False,
    "kind": "",
    "lines": [],
    "ok": None,
    "started": None,
    "finished": None,
    "logPath": str(LAST_JOB_LOG),
    "logHistoryPath": "",
    "logGamePath": "",
    # Live progress for UI (Convert packaging, etc.)
    "phase": "",
    "phaseLabel": "",
    "progress": None,  # 0..100 or None
    "progressDetail": "",
    "progressRate": "",  # e.g. "85.2 MB/s" — shown beside %
    "progressBusy": False,  # True → shimmer animation on the bar
    "stepIndex": 0,
    "stepTotal": 0,
}


def _default_config() -> dict:
    return {
        "anyps5Root": "",
        "lastGameRoot": "",
        "lastOutputDir": "",
        "lastOutputExe": "",
        "outputName": "app.exe",
        "gpuFilter": "",
        "toIntel": False,
        "windowsGui": False,
        "hideGameConsole": False,
        "unusedFilter": "0",
        "unconvertedGamesRoot": "",
        "convertedGamesRoot": "",
        "logLevel": "info",
        "lastBuildCommit": "",
        "lastBuildCommitShort": "",
        "lastBuildAt": "",
        "lastPullAt": "",
    }


def load_config() -> dict:
    cfg = _default_config()
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    # Keep file logger in sync with saved preference
    try:
        applog.set_level(str(cfg.get("logLevel") or "info"))
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    if "logLevel" in cfg:
        cfg["logLevel"] = applog.set_level(str(cfg.get("logLevel") or "info"))
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def path_has_spaces(path: Path | str) -> bool:
    return " " in str(path)


def build_path_needs_alias(path: Path | str) -> bool:
    """
    True when cmake/MinGW/glslang should run via a shorter space-free alias.
    Spaces break windres; very deep trees (common under Downloads) can flake
    custom commands that embed SPIR-V (.spv missing after glslang).
    """
    try:
        text = str(Path(path).resolve())
    except Exception:
        text = str(path)
    if " " in text:
        return True
    low = text.lower().replace("/", "\\")
    if "\\downloads\\" in low:
        return True
    # Repo roots under ~60 chars still grow by ~100 inside build/.../generated/...
    return len(text) >= 60


def windows_short_path(path: Path) -> Path | None:
    """Return 8.3 short path if Windows provides one without spaces (helps MinGW windres)."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        get_short = ctypes.windll.kernel32.GetShortPathNameW
        get_short.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        get_short.restype = wintypes.DWORD
        resolved = str(path.resolve())
        need = get_short(resolved, None, 0)
        if need == 0:
            return None
        buf = ctypes.create_unicode_buffer(need)
        if get_short(resolved, buf, need) == 0:
            return None
        short = Path(buf.value)
        if path_has_spaces(short):
            return None
        return short
    except Exception:
        return None


def _space_free_mount_root() -> Path:
    """Anchor directory that should not contain spaces (for junctions)."""
    for key in ("PROGRAMDATA", "SystemDrive"):
        raw = str(os.environ.get(key) or "").strip()
        if not raw:
            continue
        base = Path(raw)
        if key == "SystemDrive":
            base = Path(raw.rstrip("\\/") + "\\")
        if not path_has_spaces(base):
            return (base / "AnyPS5-Launcher" / "mounts").resolve()
    return Path(r"C:\AnyPS5-Launcher\mounts")


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve().as_posix().lower() == b.resolve().as_posix().lower()
    except Exception:
        return str(a).lower() == str(b).lower()


def ensure_space_free_alias(target: Path) -> Path | None:
    """
    Return a short, space-free path that points at target.
    Order: as-is (when already fine) → Windows short path → junction under ProgramData.
    """
    target = target.resolve()
    if not build_path_needs_alias(target):
        return target

    short = windows_short_path(target)
    if short is not None and not build_path_needs_alias(short):
        return short
    if short is not None and not path_has_spaces(short) and len(str(short)) < len(str(target)):
        # Prefer shorter even if still slightly long
        target_for_link = short
    else:
        target_for_link = target

    if os.name != "nt":
        return short if short is not None and not path_has_spaces(short) else None

    import hashlib

    mounts = _space_free_mount_root()
    try:
        mounts.mkdir(parents=True, exist_ok=True)
    except Exception:
        return short if short is not None and not path_has_spaces(short) else None

    dig = hashlib.sha1(str(target.resolve()).lower().encode("utf-8")).hexdigest()[:16]
    link = mounts / dig
    if path_has_spaces(link):
        return short if short is not None and not path_has_spaces(short) else None

    # Reuse existing junction if it already points here
    if link.exists():
        try:
            current = Path(os.readlink(link))
            if _same_path(current, target) or _same_path(current, target_for_link):
                return link
        except OSError:
            pass
        # Stale / wrong link: remove junction only (not the target tree)
        try:
            link.rmdir()
        except OSError:
            try:
                subprocess.run(
                    ["cmd", "/c", "rmdir", str(link)],
                    check=False,
                    capture_output=True,
                    text=True,
                )
            except Exception:
                return short if short is not None and not path_has_spaces(short) else None

    # mklink /J does not need admin; junction to the real folder (not the short path)
    proc = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0 or not link.exists():
        return short if short is not None and not path_has_spaces(short) else None
    return link


def space_safe_build_paths() -> tuple[Path, Path, str] | None:
    """
    Paths for cmake -S/-B that MinGW windres / deep Windows custom commands can digest.
    Returns (source, build, how) where how describes the mapping used.
    """
    root = get_repo_root().resolve()
    try:
        (root / "build").mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    if not build_path_needs_alias(root):
        return root, root / "build", "direct"

    alias = ensure_space_free_alias(root)
    if alias is None:
        return None
    short = windows_short_path(root)
    if short is not None and _same_path(short, alias):
        how = "short-path"
    elif _same_path(alias, root):
        how = "direct"
    else:
        how = "junction"
    build = alias / "build"
    if path_has_spaces(build):
        return None
    return alias, build, how


def path_spaces_error(path: Path | str) -> str:
    return (
        "Could not create a short build alias for MinGW/glslang. "
        f"Current path: {path}. "
        "Move AnyPS5-Launcher to a short path such as C:\\AnyPS5-Launcher, then Build again."
    )


def _spirv_embed_failure(lines: list[str]) -> bool:
    blob = "\n".join(lines).lower()
    if "embed_spirv.cmake" in blob:
        return True
    if "failed to open for reading" in blob and ".spv" in blob:
        return True
    if "sampledumps.spv" in blob and "no such file" in blob:
        return True
    return False


def find_python3_executable() -> Path | None:
    """Resolve python.exe via setup_engine (PATH refresh + registry + common folders)."""
    try:
        from setup_engine import find_python3_executable as _find

        return _find()
    except Exception:
        return None


def tool_env() -> dict:
    from setup_engine import _join_path_unique

    env = os.environ.copy()
    parts = [
        str(CMAKE_BIN),
        str(WINLIBS_BIN),
    ]
    py = find_python3_executable()
    if py is not None:
        parts.append(str(py.parent))
        # Scripts/ often holds useful tools next to python.exe
        scripts = py.parent / "Scripts"
        if scripts.is_dir():
            parts.append(str(scripts))
    parts.append(env.get("Path", env.get("PATH", "")))
    env["Path"] = _join_path_unique(*parts)
    env["PATH"] = env["Path"]
    if SSL_CERT.exists():
        env["SSL_CERT_FILE"] = str(SSL_CERT)
        env["CURL_CA_BUNDLE"] = str(SSL_CERT)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def run_capture(args: list[str], cwd: Path | None = None, timeout: int = 120) -> tuple[int, str]:
    try:
        p = subprocess.run(
            args,
            cwd=str(cwd or get_repo_root()),
            env=tool_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
        out = (p.stdout or "") + (p.stderr or "")
        return p.returncode, out.strip()
    except Exception as e:
        return 1, str(e)


def run_git_one_line(args: list[str]) -> str:
    """Return first stdout line only (ignore stderr noise that broke build stamps)."""
    try:
        p = subprocess.run(
            args,
            cwd=str(get_repo_root()),
            env=tool_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            shell=False,
        )
        if p.returncode != 0:
            return ""
        for line in (p.stdout or "").splitlines():
            s = line.strip()
            if s:
                return s
        return ""
    except Exception:
        return ""


def _safe_log_name(kind: str) -> str:
    raw = (kind or "job").strip().lower() or "job"
    out = "".join(c if (c.isalnum() or c in "-_") else "-" for c in raw)
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-") or "job"


def _game_log_label(game_root: Path) -> str:
    """Short filesystem-safe game name for convert logs (folder name preferred)."""
    generic = {
        "app0",
        "app",
        "decrypted",
        "anyps5-out",
        "sce_module",
        "sce_modules",
        "prx",
        "game",
        "roms",
        "ps5",
    }
    try:
        name = (game_root.name or "").strip()
        if name.lower() in generic and game_root.parent:
            name = game_root.parent.name or name
        return _safe_log_name(name)[:80]
    except Exception:
        return "game"


def _write_job_log_line(line: str) -> None:
    """Append one line to last-job.log and the dated/game copy (caller holds lock)."""
    payload = line + "\n"
    for key in ("logPath", "logHistoryPath", "logGamePath"):
        raw = str(_job.get(key) or "").strip()
        if not raw:
            continue
        try:
            path = Path(raw)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", errors="replace") as fh:
                fh.write(payload)
        except Exception:
            pass


def _reset_job_log(kind: str, *, name_hint: str = "") -> None:
    """Start fresh log files for a new job (caller holds lock)."""
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    kind_slug = _safe_log_name(kind)
    hint = _safe_log_name(name_hint) if name_hint else ""
    if hint and kind_slug == "convert":
        hist_name = f"convert-{hint}-{stamp}.log"
    elif hint:
        hist_name = f"{kind_slug}-{hint}-{stamp}.log"
    else:
        hist_name = f"{kind_slug}-{stamp}.log"
    hist = LOGS_DIR / hist_name
    header = (
        f"AnyPS5 Launcher job: {kind or 'job'}\n"
        f"Started (UTC): {datetime.now(timezone.utc).isoformat()}\n"
        f"Repo: {get_repo_root()}\n"
    )
    if hint:
        header += f"Game label: {hint}\n"
    header += f"{'=' * 60}\n"
    _job["logPath"] = str(LAST_JOB_LOG)
    _job["logHistoryPath"] = str(hist)
    _job["logGamePath"] = ""
    for path in (LAST_JOB_LOG, hist):
        try:
            path.write_text(header, encoding="utf-8")
        except Exception:
            pass


def _attach_game_convert_log(output_dir: Path, game_label: str) -> Path | None:
    """
    Also write the convert log next to the game output (easy for users to find/send).
    Call after output_dir exists; subsequent append_job lines go there too.
    """
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        label = _safe_log_name(game_label) or "game"
        dest = output_dir / f"convert-{label}.log"
        # Seed with whatever was already written to the history log
        hist = Path(str(_job.get("logHistoryPath") or ""))
        if hist.is_file():
            dest.write_bytes(hist.read_bytes())
        else:
            dest.write_text("", encoding="utf-8")
        with _job_lock:
            _job["logGamePath"] = str(dest)
        return dest
    except Exception:
        return None


def _failure_snippets(lines: list[str], limit: int = 12) -> list[str]:
    """Pick real failure lines; ignore normal CMake feature-probe noise."""
    noise_prefixes = (
        "-- looking for ",
        "-- performing test ",
        "-- check for working ",
        "-- detecting ",
        "-- found ",
    )
    strong_keys = (
        "cmake error",
        "could not find",
        "could NOT find",
        "fatal error",
        "ninja: build stopped",
        "undefined reference",
        "permission denied",
        "windres",
        "error: exit code",
    )
    weak_keys = (
        "error:",
        "fatal:",
        "failed:",
        "no such file",
        "cannot find",
    )
    strong: list[str] = []
    weak: list[str] = []
    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        low = s.lower()
        if low.startswith(noise_prefixes) and "cmake error" not in low:
            continue
        if "looking for " in low and " - not found" in low:
            continue
        if "performing test " in low and " - failed" in low:
            continue
        if any(k.lower() in low or k in s for k in strong_keys):
            strong.append(s)
            # Include the next non-empty line (often "Could NOT find …")
            for nxt in lines[i + 1 : i + 4]:
                t = nxt.strip()
                if not t:
                    continue
                if t.lower().startswith("-- ") and "error" not in t.lower():
                    break
                strong.append(t)
                break
            continue
        if any(k in low for k in weak_keys):
            weak.append(s)
    hits = strong or weak
    if hits:
        # Dedupe while keeping order
        out: list[str] = []
        seen: set[str] = set()
        for h in hits:
            if h in seen:
                continue
            seen.add(h)
            out.append(h)
        return out[-limit:]
    nonempty = [ln.strip() for ln in lines if ln.strip()]
    return nonempty[-min(limit, 8) :]


def note_job_failure_hints() -> None:
    """Append a short summary + log path after a failed job."""
    with _job_lock:
        lines = list(_job.get("lines") or [])
        log_path = str(_job.get("logPath") or LAST_JOB_LOG)
        hist = str(_job.get("logHistoryPath") or "").strip()
        game_log = str(_job.get("logGamePath") or "").strip()
    snippets = _failure_snippets(lines)
    append_job("")
    append_job("The line 'ERROR: exit code …' only means the command failed.")
    if snippets:
        append_job("Likely cause (from earlier output):")
        for s in snippets:
            append_job(f"  {s}")
    send = hist or log_path
    append_job(f"Full log file (send this if you need help): {send}")
    if game_log:
        append_job(f"Same log beside game output: {game_log}")
    append_job(f"Log folder: {LOGS_DIR}")


def append_job(line: str) -> None:
    with _job_lock:
        _job["lines"].append(line)
        if len(_job["lines"]) > 4000:
            _job["lines"] = _job["lines"][-3000:]
        _write_job_log_line(line)


def set_job(
    running: bool,
    kind: str = "",
    ok: bool | None = None,
    *,
    log_label: str = "",
) -> None:
    with _job_lock:
        _job["running"] = running
        if kind:
            _job["kind"] = kind
        _job["ok"] = ok
        if running:
            _job["lines"] = []
            _job["started"] = datetime.now(timezone.utc).isoformat()
            _job["finished"] = None
            _job["phase"] = ""
            _job["phaseLabel"] = ""
            _job["progress"] = None
            _job["progressDetail"] = ""
            _job["progressRate"] = ""
            _job["progressBusy"] = False
            _job["stepIndex"] = 0
            _job["stepTotal"] = 0
            _reset_job_log(kind or _job.get("kind") or "job", name_hint=log_label)
        else:
            _job["finished"] = datetime.now(timezone.utc).isoformat()
            if not _job.get("logPath"):
                _job["logPath"] = str(LAST_JOB_LOG)
            if ok is True:
                _job["phase"] = "done"
                _job["phaseLabel"] = "Finished"
                _job["progress"] = 100
            elif ok is False:
                _job["phase"] = "failed"
                _job["phaseLabel"] = "Failed"
            else:
                _job["phase"] = ""
                _job["phaseLabel"] = ""
                _job["progress"] = None
            _job["progressDetail"] = ""
            _job["progressRate"] = ""
            _job["progressBusy"] = False


def set_job_progress(
    *,
    phase: str = "",
    label: str = "",
    percent: float | None = None,
    detail: str = "",
    rate: str | None = None,
    busy: bool | None = None,
    step: int | None = None,
    step_total: int | None = None,
) -> None:
    """Update live job progress for the Console UI (thread-safe)."""
    with _job_lock:
        if phase:
            _job["phase"] = phase
        if label:
            _job["phaseLabel"] = label
        if percent is not None:
            _job["progress"] = max(0.0, min(100.0, float(percent)))
        if detail is not None:
            _job["progressDetail"] = detail
        if rate is not None:
            _job["progressRate"] = rate
        if busy is not None:
            _job["progressBusy"] = bool(busy)
        if step is not None:
            _job["stepIndex"] = int(step)
        if step_total is not None:
            _job["stepTotal"] = int(step_total)


_NINJA_PROG_RE = re.compile(r"\[(\d+)\s*/\s*(\d+)\]")


def parse_ninja_progress(line: str) -> tuple[int, int] | None:
    """Parse Ninja/CMake build lines like '[373/431] Building …'."""
    m = _NINJA_PROG_RE.search(line or "")
    if not m:
        return None
    cur, total = int(m.group(1)), int(m.group(2))
    if total <= 0 or cur < 0:
        return None
    return min(cur, total), total


def _command_progress_label(cmd: list[str]) -> str:
    """Short human label for a build/update command (UI only)."""
    if not cmd:
        return "Command"
    low = [str(c).lower() for c in cmd]
    joined = " ".join(low)
    if "cmake" in low and "-s" in low:
        return "CMake configure"
    if "cmake" in low and "--build" in low:
        if "--target" in low:
            try:
                t = cmd[low.index("--target") + 1]
            except (ValueError, IndexError):
                t = "target"
            return f"Build target: {t}"
        return "Build (compile)"
    if "git" in low and "pull" in low:
        return "git pull"
    if "git" in low and "fetch" in low:
        return "git fetch"
    if "git" in low and "submodule" in joined:
        return "git submodule"
    # Truncate long paths in the display
    parts = []
    for c in cmd[:6]:
        s = str(c)
        parts.append(s if len(s) < 40 else ("…" + s[-36:]))
    more = " …" if len(cmd) > 6 else ""
    return " ".join(parts) + more


def _path_is_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def current_head() -> tuple[str, str]:
    full = run_git_one_line(["git", "rev-parse", "HEAD"])
    short = run_git_one_line(["git", "rev-parse", "--short", "HEAD"])
    # Validate hex-ish hashes only
    if full and not all(c in "0123456789abcdef" for c in full.lower()):
        full = ""
    if short and not all(c in "0123456789abcdef" for c in short.lower()):
        short = full[:7] if full else ""
    if full and not short:
        short = full[:7]
    return full, short


def get_build_stamp() -> Path:
    return get_build_dir() / "anyps5-build-stamp.json"



def mark_build_current() -> None:
    full, short = current_head()
    if not full:
        append_job("[WARN] Could not read git HEAD; build stamp not updated.")
        return
    now = datetime.now(timezone.utc).isoformat()
    cfg = load_config()
    cfg["lastBuildCommit"] = full
    cfg["lastBuildCommitShort"] = short
    cfg["lastBuildAt"] = now
    save_config(cfg)
    try:
        get_build_dir().mkdir(parents=True, exist_ok=True)
        get_build_stamp().write_text(
            json.dumps({"commit": full, "short": short, "at": now}, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        append_job(f"[WARN] Could not write build stamp file: {e}")
    append_job(f"Build marked current: {short} ({full[:12]}...)")


def mark_repo_pulled() -> None:
    cfg = load_config()
    cfg["lastPullAt"] = datetime.now(timezone.utc).isoformat()
    save_config(cfg)
    full, short = current_head()
    build_short = cfg.get("lastBuildCommitShort") or "?"
    if cfg.get("lastBuildCommit") and full and cfg["lastBuildCommit"] != full:
        append_job(
            f"NOTE: Repo is now {short}, build still {build_short}. Open Build and run Build now."
        )
    elif not cfg.get("lastBuildCommit"):
        append_job("NOTE: Open Build and run Build now after updating.")


def run_job(
    kind: str,
    commands: list[list[str]],
    on_success=None,
    *,
    cwd: Path | None = None,
    preamble: list[str] | None = None,
    on_failure=None,
    retries_per_command: int = 0,
    should_retry=None,
) -> None:
    if _job["running"]:
        return

    def worker() -> None:
        set_job(True, kind=kind, ok=None)
        append_job(f"=== {kind} started ===")
        for line in preamble or []:
            append_job(line)
        ok = True
        workdir = str(cwd or get_repo_root())
        n_cmds = max(len(commands), 1)
        set_job_progress(
            phase="job",
            label=f"{kind}: starting…",
            percent=0,
            detail=f"{n_cmds} command(s)",
            step=0,
            step_total=n_cmds,
        )
        for cmd_i, cmd in enumerate(commands):
            slice_lo = 100.0 * cmd_i / n_cmds
            slice_hi = 100.0 * (cmd_i + 1) / n_cmds
            cmd_label = _command_progress_label(cmd)
            attempts = 1 + max(0, int(retries_per_command or 0))
            for attempt in range(1, attempts + 1):
                if attempt > 1:
                    append_job(f"Retrying command ({attempt}/{attempts})…")
                append_job("> " + " ".join(cmd))
                set_job_progress(
                    phase="job",
                    label=f"Step {cmd_i + 1}/{n_cmds}: {cmd_label}",
                    percent=slice_lo,
                    detail="starting…",
                    step=cmd_i + 1,
                    step_total=n_cmds,
                )
                mark = 0
                with _job_lock:
                    mark = len(_job["lines"])
                last_sent_pct = -1
                cmake_ticks = 0
                try:
                    proc = subprocess.Popen(
                        cmd,
                        cwd=workdir,
                        env=tool_env(),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        bufsize=1,
                    )
                    assert proc.stdout is not None
                    for line in proc.stdout:
                        text = line.rstrip()
                        append_job(text)
                        # Drive the Console progress bar from Ninja [n/m] when present.
                        # Throttle UI updates to whole percent points — keeps Waitress stable.
                        try:
                            parsed = parse_ninja_progress(text)
                            if parsed:
                                cur, tot = parsed
                                within = cur / tot
                                overall = slice_lo + (slice_hi - slice_lo) * within
                                ipct = int(overall)
                                if ipct != last_sent_pct:
                                    last_sent_pct = ipct
                                    after = text.split("]", 1)[-1].strip()
                                    set_job_progress(
                                        phase="job",
                                        label=f"Step {cmd_i + 1}/{n_cmds}: {cmd_label}",
                                        percent=overall,
                                        detail=f"[{cur}/{tot}] {after[:100]}",
                                        step=cmd_i + 1,
                                        step_total=n_cmds,
                                    )
                            elif text.startswith("--"):
                                # CMake configure has no [n/m]; ease within this step only.
                                cmake_ticks += 1
                                within = min(0.92, 0.08 + 0.84 * (1.0 - (0.97 ** cmake_ticks)))
                                overall = slice_lo + (slice_hi - slice_lo) * within
                                ipct = int(overall)
                                if ipct != last_sent_pct:
                                    last_sent_pct = ipct
                                    set_job_progress(
                                        phase="job",
                                        label=f"Step {cmd_i + 1}/{n_cmds}: {cmd_label}",
                                        percent=overall,
                                        detail=text[:120],
                                        step=cmd_i + 1,
                                        step_total=n_cmds,
                                    )
                        except Exception:
                            pass  # progress is cosmetic — never fail the build on UI updates
                    code = proc.wait()
                    if code == 0:
                        set_job_progress(
                            phase="job",
                            label=f"Step {cmd_i + 1}/{n_cmds}: {cmd_label}",
                            percent=slice_hi,
                            detail="step done",
                            step=cmd_i + 1,
                            step_total=n_cmds,
                        )
                        break
                    append_job(f"ERROR: exit code {code} ({' '.join(cmd)})")
                    with _job_lock:
                        chunk = list(_job["lines"][mark:])
                    if attempt < attempts and should_retry and should_retry(chunk):
                        append_job(
                            "Transient build step failure detected; "
                            "retrying the same command once more."
                        )
                        continue
                    ok = False
                    break
                except Exception as e:
                    append_job(f"ERROR: {e}")
                    ok = False
                    break
            if not ok:
                break
        if ok and on_success:
            try:
                on_success()
            except Exception as e:
                append_job(f"Post-step failed: {e}")
                ok = False
        if not ok:
            if on_failure:
                try:
                    on_failure()
                except Exception:
                    pass
            note_job_failure_hints()
        append_job("=== done ===" if ok else "=== aborted ===")
        set_job(False, ok=ok)

    threading.Thread(target=worker, daemon=True).start()


def build_status() -> dict:
    prx = 0
    libs = get_libs_dir()
    if libs.is_dir():
        prx = len(list(libs.glob("*.prx")))
    ready = get_relinker().is_file() and prx > 0
    head_full, head_short = current_head()
    cfg = load_config()
    build_full = (cfg.get("lastBuildCommit") or "").strip()
    build_short = (cfg.get("lastBuildCommitShort") or "").strip()

    # Prefer stamp file written after successful Full Rebuild
    if get_build_stamp().is_file():
        try:
            stamp = json.loads(get_build_stamp().read_text(encoding="utf-8"))
            if stamp.get("commit"):
                build_full = stamp["commit"].strip()
                build_short = (stamp.get("short") or build_full[:7]).strip()
                # Keep config in sync with stamp
                if cfg.get("lastBuildCommit") != build_full:
                    cfg["lastBuildCommit"] = build_full
                    cfg["lastBuildCommitShort"] = build_short
                    cfg["lastBuildAt"] = stamp.get("at") or cfg.get("lastBuildAt") or ""
                    save_config(cfg)
        except Exception:
            pass

    if build_full and not build_short:
        build_short = build_full[:7]

    in_sync = bool(ready and build_full and head_full and build_full == head_full)
    needs_rebuild = False
    sync_state = "missing"
    sync_label = "No build"
    root = get_repo_root()
    repo_present = _is_anyps5_tree(root)

    if not repo_present:
        sync_state = "need_setup"
        sync_label = "AnyPS5 is not cloned yet. Open Setup and click Install / repair."
        needs_rebuild = False
    elif not ready:
        sync_state = "missing"
        sync_label = "Not built yet. Open Build and run Build now."
        needs_rebuild = True
    elif not build_full:
        sync_state = "unknown"
        sync_label = "Build found, but status is unclear. Build again or mark as up to date."
        needs_rebuild = True
    elif in_sync:
        sync_state = "current"
        sync_label = "Build is up to date."
        needs_rebuild = False
    else:
        sync_state = "outdated"
        sync_label = "Updates were downloaded. Run Build now."
        needs_rebuild = True

    return {
        "relinker": get_relinker().is_file(),
        "relinkerPath": str(get_relinker()),
        "prxCount": prx,
        "ready": ready,
        "repoPresent": repo_present,
        "winlibs": (WINLIBS_BIN / "gcc.exe").is_file(),
        "cmake": (CMAKE_BIN / "cmake.exe").is_file() or bool(run_capture(["where", "cmake"])[0] == 0),
        "headCommit": head_full,
        "headCommitShort": head_short,
        "buildCommit": build_full,
        "buildCommitShort": build_short,
        "buildAt": cfg.get("lastBuildAt") or "",
        "lastPullAt": cfg.get("lastPullAt") or "",
        "inSync": in_sync,
        "needsRebuild": needs_rebuild,
        "syncState": sync_state,
        "syncLabel": sync_label,
    }


def _parse_log(raw: str) -> list[dict]:
    commits = []
    for line in (raw or "").splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4:
            commits.append(
                {"hash": parts[0], "date": parts[1], "author": parts[2], "subject": parts[3]}
            )
    return commits


def git_status(fetch: bool = False) -> dict:
    code_h, head = run_capture(["git", "rev-parse", "--short", "HEAD"])
    code_b, branch = run_capture(["git", "branch", "--show-current"])

    fetch_out = ""
    remote_ok = True
    if fetch:
        code_f, fetch_out = run_capture(["git", "fetch", "origin"], timeout=90)
        remote_ok = code_f == 0

    behind = ahead = 0
    _, behind_s = run_capture(["git", "rev-list", "--count", "HEAD..origin/main"])
    _, ahead_s = run_capture(["git", "rev-list", "--count", "origin/main..HEAD"])
    try:
        behind = int(behind_s or "0")
    except ValueError:
        behind = 0
    try:
        ahead = int(ahead_s or "0")
    except ValueError:
        ahead = 0

    _, log_out = run_capture(
        ["git", "log", "-12", "--pretty=format:%h|%ad|%an|%s", "--date=short", "HEAD"],
        timeout=30,
    )
    commits = _parse_log(log_out)

    pending = []
    if behind > 0:
        _, pend = run_capture(
            [
                "git",
                "log",
                "-20",
                "--pretty=format:%h|%ad|%an|%s",
                "--date=short",
                "HEAD..origin/main",
            ]
        )
        pending = _parse_log(pend)

    dirty_code, dirty = run_capture(["git", "status", "--porcelain"])
    dirty_count = len([l for l in dirty.splitlines() if l.strip()]) if dirty_code == 0 else 0

    return {
        "head": head if code_h == 0 else "?",
        "branch": branch if code_b == 0 else "?",
        "remoteOk": remote_ok,
        "fetchMessage": fetch_out if not remote_ok else "",
        "behind": behind,
        "ahead": ahead,
        "updateAvailable": behind > 0,
        "dirtyCount": dirty_count,
        "localLog": commits,
        "pendingLog": pending,
        "fetched": fetch,
    }


def find_input_elf(game_root: Path) -> Path | None:
    for name in ("eboot.bin",):
        p = game_root / name
        if p.is_file():
            return p
    elfs = sorted(game_root.glob("*.elf"))
    if elfs:
        return elfs[0]
    # shallow search one level
    for child in game_root.iterdir():
        if child.is_dir():
            for name in ("eboot.bin",):
                p = child / name
                if p.is_file():
                    return p
            elfs = sorted(child.glob("*.elf"))
            if elfs:
                return elfs[0]
    return None


def find_module_dir(game_root: Path) -> str | None:
    for name in ("sce_module", "sce_modules", "prx"):
        if (game_root / name).is_dir():
            return name
    for child in game_root.iterdir():
        if child.is_dir():
            for name in ("sce_module", "sce_modules", "prx"):
                if (child / name).is_dir():
                    return str(child.relative_to(game_root) / name).replace("\\", "/")
    return None


def _dir_has_guest_modules(path: Path) -> bool:
    try:
        return any(path.glob("*.prx")) or any(path.glob("*.elf"))
    except OSError:
        return False


def find_extra_module_dirs(content_root: Path) -> list[str]:
    """
    Extra --module-dir paths for Unity/engine titles (Media/Modules, Media/Plugins, …).
    Relinker only auto-converts sce_module/sce_modules/prx; everything else needs --module-dir.
    """
    extras: list[str] = []
    seen: set[str] = set()

    def add(rel: Path) -> None:
        key = str(rel).replace("\\", "/").strip("/").lower()
        if not key or key in seen:
            return
        if rel.name.lower() in ("sce_module", "sce_modules", "prx"):
            return
        full = content_root / rel
        if full.is_dir() and _dir_has_guest_modules(full):
            seen.add(key)
            extras.append(str(rel).replace("\\", "/"))

    for rel in (
        Path("Media") / "Modules",
        Path("Media") / "Plugins",
        Path("modules"),
        Path("Modules"),
        Path("Plugins"),
    ):
        add(rel)

    media = content_root / "Media"
    if media.is_dir():
        try:
            for child in media.iterdir():
                if child.is_dir():
                    add(Path("Media") / child.name)
        except OSError:
            pass

    return extras


def find_mingw_runtime_sources() -> dict[str, Path]:
    """Locate each MinGW runtime DLL from WinLibs, the build libs folder, or gcc on PATH."""
    found: dict[str, Path] = {}
    search_dirs: list[Path] = []

    # Prefer the folder that actually holds the built .prx (may already have runtimes)
    try:
        libs = get_libs_dir()
        if libs.is_dir():
            search_dirs.append(libs)
    except Exception:
        pass

    search_dirs.append(WINLIBS_BIN)

    # gcc.exe directory from PATH / common WinLibs layouts
    for cand in (
        shutil.which("gcc"),
        shutil.which("g++"),
    ):
        if cand:
            search_dirs.append(Path(cand).resolve().parent)

    for extra in (
        Path(r"C:\winlibs\mingw64\bin"),
        Path(r"C:\mingw64\bin"),
        Path(os.environ.get("WINLIBS_BIN", "")),
    ):
        if extra and str(extra) not in ("", "."):
            search_dirs.append(extra)

    # Deduplicate while preserving order
    seen: set[str] = set()
    ordered: list[Path] = []
    for d in search_dirs:
        try:
            key = str(d.resolve()).lower()
        except Exception:
            key = str(d).lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(d)

    for name in MINGW_RUNTIME_DLLS:
        for d in ordered:
            src = d / name
            if src.is_file():
                found[name] = src
                break
    return found


def _copy_file_bytes(src: Path, dest: Path) -> bool:
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = src.read_bytes()
        if dest.is_file() and dest.stat().st_size == len(data):
            # Same size — still overwrite to avoid stale partial copies
            pass
        dest.write_bytes(data)
        return True
    except OSError:
        return False


def mingw_destinations_for_output(output_dir: Path) -> list[Path]:
    """
    Folders that must contain the MinGW runtimes for LoadLibraryEx to succeed.
    - libs/: system .prx and any title libs loaded via $ORIGIN/libs
    - output_dir/: application directory (DEFAULT_DIRS)
    - any app0 folder that holds converted .prx / .guest.prx (sce_module, Media/Modules, …)
    """
    dests: list[Path] = [output_dir / "libs", output_dir]
    app0 = output_dir / "app0"
    if app0.is_dir():
        try:
            for d in app0.rglob("*"):
                if not d.is_dir():
                    continue
                try:
                    if any(d.glob("*.prx")) or any(d.glob("*.guest.prx")):
                        dests.append(d)
                except OSError:
                    continue
        except OSError:
            pass
    # Dedupe
    out: list[Path] = []
    seen: set[str] = set()
    for d in dests:
        try:
            key = str(d.resolve()).lower()
        except Exception:
            key = str(d).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


def copy_mingw_runtime_dlls(
    *dest_dirs: Path,
) -> tuple[int, list[str], list[str], list[tuple[str, str, int, bool]]]:
    """
    Copy MinGW runtime DLLs into each destination directory.
    Returns (files_written, missing_names, source_dirs_used, details).
    details entries: (dll_name, dest_path, bytes, ok).
    """
    sources = find_mingw_runtime_sources()
    missing = [n for n in MINGW_RUNTIME_DLLS if n not in sources]
    written = 0
    used: list[str] = []
    details: list[tuple[str, str, int, bool]] = []
    for dest_dir in dest_dirs:
        if dest_dir is None:
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        for name, src in sources.items():
            dest = dest_dir / name
            ok = _copy_file_bytes(src, dest)
            size = 0
            if ok:
                written += 1
                try:
                    size = dest.stat().st_size
                except OSError:
                    size = 0
                src_dir = str(src.parent)
                if src_dir not in used:
                    used.append(src_dir)
            details.append((name, str(dest), size, ok))
    return written, missing, used, details


def verify_mingw_in_libs(libs_dest: Path) -> list[str]:
    """Return names of MinGW DLLs still missing from libs/."""
    return [n for n in MINGW_RUNTIME_DLLS if not (libs_dest / n).is_file()]


def ensure_mingw_runtime_for_output(output_dir: Path) -> tuple[bool, str]:
    """
    Make sure MinGW runtimes exist beside the converted game (repair/start path).
    Returns (ok, detail_message).
    """
    output_dir = Path(output_dir)
    libs = output_dir / "libs"
    dests = mingw_destinations_for_output(output_dir)
    written, missing, used, _details = copy_mingw_runtime_dlls(*dests)
    still = verify_mingw_in_libs(libs)
    if still:
        return (
            False,
            f"Missing MinGW DLL(s) in {libs}: {', '.join(still)}. "
            f"Install WinLibs (Setup) so these exist under {WINLIBS_BIN}.",
        )
    where = ", ".join(str(d) for d in dests)
    src = used[0] if used else str(WINLIBS_BIN)
    return True, f"MinGW runtimes OK in [{where}] (from {src}; wrote {written} file(s))"


def _app0_resource_ignore(directory: str, names: list[str]) -> set[str]:
    """Skip module trees / ELF/PRX so we never overwrite relinker guest modules with raw dumps."""
    ignored: set[str] = set()
    for name in names:
        low = name.lower()
        if low in APP0_SKIP_DIR_NAMES:
            ignored.add(name)
            continue
        suffix = Path(name).suffix.lower()
        if suffix in (".prx", ".elf") or low in APP0_SKIP_FILE_NAMES or suffix == ".exe":
            ignored.add(name)
            continue
        # Unity / engine module folders (converted via --module-dir)
        if low in ("modules", "plugins") and (Path(directory) / name).is_dir():
            # Only skip when they actually contain modules
            try:
                if _dir_has_guest_modules(Path(directory) / name):
                    ignored.add(name)
            except OSError:
                pass
    return ignored


def _fmt_bytes(n: float) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    v = float(max(n, 0))
    for u in units:
        if v < 1024 or u == units[-1]:
            return f"{int(v)} B" if u == "B" else f"{v:.1f} {u}"
        v /= 1024
    return f"{n:.0f} B"


def _fmt_rate(bps: float) -> str:
    return f"{_fmt_bytes(bps)}/s"


class _CopyRateMeter:
    """Track bytes copied and emit a smoothed transfer rate string."""

    def __init__(self) -> None:
        self.bytes_done = 0
        self._t0 = time.monotonic()
        self._last_t = self._t0
        self._last_b = 0
        self._rate_bps = 0.0

    def add(self, n: int) -> None:
        if n <= 0:
            return
        self.bytes_done += n
        now = time.monotonic()
        dt = now - self._last_t
        if dt >= 0.2:
            self._rate_bps = (self.bytes_done - self._last_b) / max(dt, 1e-3)
            self._last_t = now
            self._last_b = self.bytes_done

    @property
    def rate_bps(self) -> float:
        # Fallback to average if we haven't sampled yet
        if self._rate_bps > 0:
            return self._rate_bps
        elapsed = max(time.monotonic() - self._t0, 1e-3)
        return self.bytes_done / elapsed

    @property
    def rate_str(self) -> str:
        return _fmt_rate(self.rate_bps)


def _copy_file_chunked(src: Path, dest: Path, on_chunk=None, chunk_size: int = 1024 * 1024) -> int:
    """
    Copy one file; for larger files stream in chunks and call on_chunk(bytes_just_written).
    Returns bytes written.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        size = src.stat().st_size
    except OSError:
        size = 0
    # Small files: one-shot copy is fine
    if size < 8 * 1024 * 1024:
        shutil.copy2(src, dest)
        if on_chunk and size:
            on_chunk(size)
        return size
    written = 0
    with src.open("rb") as fsrc, dest.open("wb") as fdst:
        while True:
            buf = fsrc.read(chunk_size)
            if not buf:
                break
            fdst.write(buf)
            written += len(buf)
            if on_chunk:
                on_chunk(len(buf))
    try:
        shutil.copystat(src, dest)
    except OSError:
        pass
    return written


def _iter_copy_tree_files(src: Path, dest: Path, ignore_fn):
    """
    Yield (src_file, dest_file) for every file that should be copied under src,
    applying the same ignore rules as shutil.copytree(ignore=…).
    """
    dest.mkdir(parents=True, exist_ok=True)
    for root, dirnames, filenames in os.walk(src):
        root_p = Path(root)
        try:
            ignored = set(ignore_fn(str(root_p), list(dirnames) + list(filenames)) or [])
        except Exception:
            ignored = set()
        dirnames[:] = sorted(d for d in dirnames if d not in ignored)
        rel_root = root_p.relative_to(src)
        target_dir = dest / rel_root
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in sorted(filenames):
            if name in ignored:
                continue
            yield root_p / name, target_dir / name


def copy_game_resources_to_app0(
    content_root: Path,
    app0: Path,
    *,
    output_dir: Path | None = None,
    on_progress=None,
) -> tuple[int, int, list[str], list[str]]:
    """
    Copy title resources (wads, sce_sys, media, …) into app0/.
    Skips eboot/elf/prx and module dirs; the relinker already places converted modules under app0.
    Also skips any folder that contains (or is inside) the live output_dir so nested
    defaults like game\\decrypted\\anyps5-out do not re-copy the package into itself.

    on_progress receives a dict with live copy stats (bytes, rate, current file, …).

    Returns (file_count, dir_count, skipped_nested_output_dirs, manifest_lines).
    """
    app0.mkdir(parents=True, exist_ok=True)
    file_count = 0
    dir_count = 0
    skipped: list[str] = []
    manifest: list[str] = []
    if not content_root.is_dir():
        return 0, 0, skipped, manifest

    meter = _CopyRateMeter()
    last_ui = 0.0

    def _emit(
        *,
        idx: int,
        total: int,
        name: str,
        current_file: str = "",
        force: bool = False,
    ) -> None:
        nonlocal last_ui
        if not on_progress:
            return
        now = time.monotonic()
        if not force and (now - last_ui) < 0.2:
            return
        last_ui = now
        on_progress(
            {
                "idx": idx,
                "total": total,
                "name": name,
                "files": file_count,
                "dirs": dir_count,
                "bytes_done": meter.bytes_done,
                "rate_bps": meter.rate_bps,
                "rate": meter.rate_str,
                "current_file": current_file,
            }
        )

    items = sorted(content_root.iterdir(), key=lambda p: p.name.lower())
    total_items = max(len(items), 1)
    for idx, item in enumerate(items, start=1):
        low = item.name.lower()
        _emit(idx=idx, total=total_items, name=item.name, force=True)
        if item.is_dir():
            if low in APP0_SKIP_DIR_NAMES:
                manifest.append(f"SKIP dir  {item.name}/  (module/output dir — relinker owns this)")
                continue
            # Do not copy a tree that contains the output folder (or is inside it)
            if output_dir is not None and (
                _path_is_under(output_dir, item) or _path_is_under(item, output_dir)
            ):
                skipped.append(item.name)
                manifest.append(f"SKIP dir  {item.name}/  (nested convert output)")
                continue
            dest = app0 / item.name
            n_before = file_count
            bytes_before = meter.bytes_done
            for src_f, dest_f in _iter_copy_tree_files(item, dest, _app0_resource_ignore):
                try:
                    rel = str(src_f.relative_to(item)).replace("/", "\\")
                except ValueError:
                    rel = src_f.name
                cur_name = f"{item.name}\\{rel}"

                def _chunk(n: int, i=idx, nm=item.name, cur=cur_name) -> None:
                    meter.add(n)
                    _emit(idx=i, total=total_items, name=nm, current_file=cur)

                _copy_file_chunked(src_f, dest_f, on_chunk=_chunk)
                file_count += 1
            n = file_count - n_before
            dir_count += 1
            copied_bytes = meter.bytes_done - bytes_before
            manifest.append(
                f"COPY dir  {item.name}/  → app0\\{item.name}\\  "
                f"({n} file(s), {_fmt_bytes(copied_bytes)})"
            )
            _emit(idx=idx, total=total_items, name=item.name, force=True)
            continue
        if low in APP0_SKIP_FILE_NAMES or item.suffix.lower() in (".elf", ".prx", ".exe"):
            reason = "eboot/elf/prx/exe"
            if low in APP0_SKIP_FILE_NAMES:
                reason = "reserved name"
            manifest.append(f"SKIP file {item.name}  ({reason})")
            continue

        def _chunk_file(n: int, i=idx, nm=item.name) -> None:
            meter.add(n)
            _emit(idx=i, total=total_items, name=nm, current_file=nm)

        size = _copy_file_chunked(item, app0 / item.name, on_chunk=_chunk_file)
        file_count += 1
        manifest.append(f"COPY file {item.name}  → app0\\{item.name}  ({size} bytes)")
        _emit(idx=idx, total=total_items, name=item.name, force=True)
    return file_count, dir_count, skipped, manifest


def browse_dialog(kind: str) -> str | None:
    """Open a native folder/file dialog on Windows."""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        if kind == "file":
            path = filedialog.askopenfilename(
                title="ELF / eboot wählen",
                filetypes=[
                    ("PS5 executable", "*.elf;eboot.bin"),
                    ("All files", "*.*"),
                ],
            )
        else:
            path = filedialog.askdirectory(title="Ordner wählen")
        root.destroy()
        return path or None
    except Exception:
        return None


@app.after_request
def _no_cache(resp):
    # Avoid stale launcher UI after updates
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/status")
def api_status():
    from self_update import get_state as launcher_update_state

    return jsonify(
        {
            "repoRoot": str(get_repo_root()),
            "githubUrl": GITHUB_URL,
            "launcherVersion": LAUNCHER_VERSION,
            "launcherGithubUrl": LAUNCHER_GITHUB_URL,
            "launcherReleasesUrl": LAUNCHER_RELEASES_PAGE,
            "launcherUpdate": launcher_update_state(),
            "build": build_status(),
            "git": git_status(),
            "config": load_config(),
            "job": {
                "running": _job["running"],
                "kind": _job["kind"],
                "ok": _job["ok"],
                "started": _job["started"],
                "finished": _job["finished"],
                "lineCount": len(_job["lines"]),
            },
        }
    )


@app.get("/api/launcher/update-check")
def api_launcher_update_check():
    """Check GitHub for a newer launcher release (once per runtime by default)."""
    from self_update import check_for_update

    force = str(request.args.get("force") or "").strip().lower() in ("1", "true", "yes")
    try:
        state = check_for_update(force=force)
        return jsonify({"ok": True, **state})
    except Exception as e:
        return jsonify(
            {
                "ok": False,
                "checked": True,
                "updateAvailable": False,
                "currentVersion": LAUNCHER_VERSION,
                "error": str(e),
                "canAutoUpdate": False,
            }
        ), 200


@app.post("/api/launcher/update")
def api_launcher_update():
    """Start self-update in a background thread; UI polls phase until restart/fail."""
    from self_update import get_state, start_update_async

    state = get_state()
    if state.get("updating"):
        return jsonify({"ok": False, "error": "Update already in progress", **state}), 409
    try:
        print("[update] Update Now clicked — starting background download", flush=True)
        result = start_update_async()
        return jsonify(result)
    except Exception as e:
        print(f"[update] failed to start: {e}", flush=True)
        return jsonify({"ok": False, "error": str(e)}), 400


@app.post("/api/open-github")
def api_open_github():
    """Return a GitHub URL for the UI to open once (do not open a browser here — that doubles tabs)."""
    data = request.get_json(silent=True) or {}
    which = str(data.get("which") or "anyps5").strip().lower()
    if which in ("launcher", "self", "anyps5-launcher"):
        url = LAUNCHER_GITHUB_URL
    else:
        url = GITHUB_URL
    return jsonify({"ok": True, "url": url})


@app.get("/api/readme")
def api_readme():
    """Latest README from GitHub main; falls back to local file."""
    headers = {"User-Agent": "AnyPS5-Launcher"}
    try:
        req = urllib.request.Request(GITHUB_README_RAW, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        return jsonify(
            {
                "ok": True,
                "source": "github",
                "url": GITHUB_URL,
                "rawUrl": GITHUB_README_RAW,
                "markdown": text,
            }
        )
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        local = get_repo_root() / "README.md"
        if local.is_file():
            return jsonify(
                {
                    "ok": True,
                    "source": "local",
                    "url": GITHUB_URL,
                    "rawUrl": str(local),
                    "markdown": local.read_text(encoding="utf-8", errors="replace"),
                    "warning": f"GitHub nicht erreichbar ({e}); lokale README gezeigt.",
                }
            )
        return jsonify({"ok": False, "error": str(e)}), 502


@app.get("/api/job")
def api_job():
    with _job_lock:
        data = dict(_job)
    data["logPath"] = data.get("logPath") or str(LAST_JOB_LOG)
    data["logsDir"] = str(LOGS_DIR)
    return jsonify(data)


@app.post("/api/logs/open")
def api_open_logs():
    """Open the latest job log (prefer per-game convert log) in Explorer."""
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    with _job_lock:
        hist = str(_job.get("logHistoryPath") or "").strip()
        game_log = str(_job.get("logGamePath") or "").strip()
    target = LAST_JOB_LOG
    for cand in (hist, game_log, str(LAST_JOB_LOG)):
        p = Path(cand) if cand else None
        if p and p.is_file():
            target = p
            break
    if not target.is_file():
        target = LOGS_DIR
    try:
        if target.is_file():
            subprocess.Popen(["explorer", f"/select,{target}"])
        else:
            subprocess.Popen(["explorer", str(LOGS_DIR)])
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "path": str(target), "logsDir": str(LOGS_DIR)})


@app.post("/api/browse")
def api_browse():
    data = request.get_json(silent=True) or {}
    kind = data.get("kind", "folder")
    path = browse_dialog("file" if kind == "file" else "folder")
    return jsonify({"path": path})


@app.post("/api/config")
def api_config():
    cfg = load_config()
    data = request.get_json(silent=True) or {}
    for key in _default_config():
        if key in data:
            cfg[key] = data[key]
    save_config(cfg)
    applog.info("Config saved", f"keys={sorted(data.keys())}")
    return jsonify({"ok": True, "config": cfg})


@app.post("/api/log")
def api_client_log():
    """Accept UI/client log lines into launcher.log."""
    data = request.get_json(silent=True) or {}
    level = str(data.get("level") or "info").strip().lower()
    message = str(data.get("message") or data.get("msg") or "").strip()
    detail = data.get("detail")
    if not message:
        return jsonify({"ok": False, "error": "Empty message"}), 400
    if level == "debug":
        applog.debug(f"[ui] {message}", detail)
    elif level == "warning":
        applog.warning(f"[ui] {message}", detail)
    elif level == "error":
        applog.error(f"[ui] {message}", detail)
    else:
        applog.info(f"[ui] {message}", detail)
    return jsonify({"ok": True, "logPath": applog.get_log_path(), "level": applog.get_level()})


@app.post("/api/update-check")
def api_update_check():
    return jsonify({"ok": True, "git": git_status(fetch=True), "build": build_status()})


@app.post("/api/update")
def api_update():
    if _job["running"]:
        return jsonify({"ok": False, "error": "A job is already running."}), 409
    run_job(
        "GitHub Update",
        [
            ["git", "pull", "--ff-only", "origin", "main"],
            ["git", "submodule", "update", "--init", "--recursive"],
        ],
        on_success=mark_repo_pulled,
    )
    return jsonify({"ok": True})


@app.post("/api/rebuild")
def api_rebuild():
    if _job["running"]:
        return jsonify({"ok": False, "error": "A job is already running."}), 409
    if not (WINLIBS_BIN / "gcc.exe").is_file():
        return jsonify({"ok": False, "error": "WinLibs GCC missing. Run setup first."}), 400
    python_exe = find_python3_executable()
    if python_exe is None:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "No usable python.exe found (CMake/glslang need it for Build). "
                    "Checked refreshed PATH, the py launcher, registry InstallPath, and common folders. "
                    "Often this is a PATH/install issue (Microsoft Store stub, or install without "
                    "'Add python.exe to PATH'), not missing admin rights. "
                    "Open Setup → Install / repair, or install Python 3.12 from python.org with PATH enabled, "
                    "then fully quit and restart the launcher."
                ),
            }
        ), 400
    paths = space_safe_build_paths()
    if paths is None:
        return jsonify({"ok": False, "error": path_spaces_error(get_repo_root())}), 400
    src, build, how = paths
    cores = str(os.cpu_count() or 4)
    py_cmake = python_exe.resolve().as_posix()
    py_root = python_exe.resolve().parent.as_posix()
    preamble: list[str] = [f"Using Python for CMake: {py_cmake}"]
    if how != "direct":
        label = "Windows short path" if how == "short-path" else "short junction"
        preamble.append(
            f"Note: install path is long or has spaces. Building via {label} "
            "(MinGW windres / glslang embed). Your folder stays as-is:"
        )
        preamble.append(f"  real: {get_repo_root()}")
        preamble.append(f"  -S {src}")
        preamble.append(f"  -B {build}")

    def on_build_failure() -> None:
        with _job_lock:
            blob = "\n".join(_job.get("lines") or []).lower()
            lines = list(_job.get("lines") or [])
        if "windres" in blob and ("no such file" in blob or path_has_spaces(get_repo_root())):
            append_job("")
            append_job(path_spaces_error(get_repo_root()))
        if "could not find python3" in blob or "missing: python3_executable" in blob:
            append_job("")
            append_job(
                "CMake could not find Python 3 (needed by glslang). "
                "Usually PATH or a Store stub, not admin rights. "
                "Setup → Install / repair, or install Python from python.org with PATH enabled, "
                "then fully quit and restart the launcher."
            )
        if _spirv_embed_failure(lines):
            append_job("")
            append_job(
                "Shader embed failed: glslang did not leave a .spv file for CMake "
                "(often SampleDumps.spv while building libSceAgcDriver). "
                "Python/configure already succeeded; this is usually a flaky Windows step "
                "under a deep folder such as Downloads, or antivirus locking the output. "
                "Try Build again. If it keeps failing, move the launcher to a short path "
                "like C:\\AnyPS5-Launcher, then Setup/Build again."
            )

    def _retry_libs(chunk: list[str]) -> bool:
        # Only auto-retry the libs target when SPIR-V embed flakes.
        return _spirv_embed_failure(chunk)

    run_job(
        "Build",
        [
            [
                "cmake",
                "-S",
                str(src),
                "-B",
                str(build),
                "-G",
                "Ninja",
                "-DCMAKE_BUILD_TYPE=Release",
                "-DCMAKE_C_COMPILER=gcc",
                "-DCMAKE_CXX_COMPILER=g++",
                f"-DPython3_EXECUTABLE={py_cmake}",
                f"-DPython3_ROOT_DIR={py_root}",
            ],
            ["cmake", "--build", str(build), "--parallel", cores],
            ["cmake", "--build", str(build), "--target", "libs", "--parallel", cores],
        ],
        on_success=mark_build_current,
        cwd=src,
        preamble=preamble,
        on_failure=on_build_failure,
        retries_per_command=1,
        should_retry=_retry_libs,
    )
    return jsonify({"ok": True})


@app.post("/api/build/mark-current")
def api_mark_build_current():
    """Manual stamp after a successful rebuild if auto-stamp failed."""
    if not get_relinker().is_file():
        return jsonify({"ok": False, "error": "No relinker.exe. Build first."}), 400
    mark_build_current()
    return jsonify({"ok": True, "build": build_status()})


DEFAULT_INPUT_ACTIONS = [
    "Cross", "Circle", "Triangle", "Square",
    "L1", "R1", "L2", "R2", "L3", "R3", "Options",
    "Up", "Right", "Down", "Left",
    "LeftStickLeft", "LeftStickRight", "LeftStickUp", "LeftStickDown",
    "RightStickLeft", "RightStickRight", "RightStickUp", "RightStickDown",
    "TouchLeft", "TouchRight", "ToggleMouse", "ToggleFullscreen",
]


def default_keyboard_bindings() -> dict:
    return {
        "Cross": ["KEY:Space", "KEY:F"],
        "Circle": ["KEY:Escape"],
        "Triangle": ["KEY:R"],
        "Square": ["MOUSE:Left"],
        "L1": ["KEY:Q"],
        "R1": ["KEY:E"],
        "L2": ["MOUSE:Right"],
        "R2": ["MOUSE:Middle"],
        "Options": ["KEY:Return"],
        "Up": ["KEY:Up"],
        "Down": ["KEY:Down"],
        "Left": ["KEY:Left"],
        "Right": ["KEY:Right"],
        "LeftStickLeft": ["KEY:A"],
        "LeftStickRight": ["KEY:D"],
        "LeftStickUp": ["KEY:W"],
        "LeftStickDown": ["KEY:S"],
        "ToggleFullscreen": ["KEY:F11"],
    }


def bindings_to_ini(bindings: dict) -> str:
    lines = [
        "# anyps5-input.ini generated by AnyPS5 Launcher",
        "# Keyboard/mouse only. Controllers use SDL defaults automatically.",
        "",
    ]
    for action in DEFAULT_INPUT_ACTIONS:
        vals = bindings.get(action) or []
        if isinstance(vals, str):
            vals = [vals]
        for v in vals:
            v = str(v).strip()
            if v:
                lines.append(f"{action} = {v}")
    lines.append("")
    return "\n".join(lines)


def ini_to_bindings(text: str) -> dict:
    out: dict[str, list[str]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if "=" not in line:
            continue
        action, value = line.split("=", 1)
        action = action.strip()
        value = value.strip()
        if not action or not value:
            continue
        out.setdefault(action, [])
        if value not in out[action]:
            out[action].append(value)
    return out


@app.get("/api/input")
def api_input_get():
    profile = {"bindings": default_keyboard_bindings()}
    if INPUT_PROFILE_PATH.is_file():
        try:
            loaded = json.loads(INPUT_PROFILE_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded.get("bindings"), dict):
                profile["bindings"] = loaded["bindings"]
        except Exception:
            pass
    elif INPUT_INI_PATH.is_file():
        try:
            profile["bindings"] = ini_to_bindings(INPUT_INI_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return jsonify(
        {
            "ok": True,
            "actions": DEFAULT_INPUT_ACTIONS,
            "profile": profile,
            "iniPath": str(INPUT_INI_PATH),
            "note": "Keyboard/mouse bindings for anyps5-input.ini. Controllers work via SDL with no config.",
        }
    )


@app.post("/api/input")
def api_input_save():
    data = request.get_json(silent=True) or {}
    bindings = data.get("bindings")
    if not bindings:
        bindings = default_keyboard_bindings()
    profile = {
        "bindings": bindings,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    INPUT_PROFILE_PATH.write_text(json.dumps(profile, indent=2), encoding="utf-8")
    INPUT_INI_PATH.write_text(bindings_to_ini(bindings), encoding="utf-8")
    return jsonify({"ok": True, "iniPath": str(INPUT_INI_PATH), "profile": profile})


def _json_str(data: dict, key: str, default: str = "") -> str:
    """Read a JSON string field; treat null/non-str as default (avoids HTTP 500 on .strip())."""
    val = data.get(key, default)
    if val is None:
        return default
    return str(val).strip().strip('"')


@app.post("/api/convert")
def api_convert():
    if _job["running"]:
        applog.warning("Convert rejected — job already running")
        return jsonify({"ok": False, "error": "A job is already running."}), 409

    try:
        data = request.get_json(silent=True) or {}
        cfg = load_config()
        game_root = Path(_json_str(data, "gameRoot"))
        applog.info("Convert requested", str(game_root))
        out_raw = _json_str(data, "outputDir")
        # Path("") becomes "." in pathlib - treat blank as default output folder.
        if out_raw:
            output_dir = Path(out_raw)
        elif str(cfg.get("convertedGamesRoot") or "").strip():
            from library import inspect_game_folder, safe_folder_name

            meta = inspect_game_folder(game_root)
            folder = safe_folder_name(str(meta.get("title") or game_root.name))
            output_dir = Path(cfg["convertedGamesRoot"]) / folder
        else:
            output_dir = game_root / "anyps5-out"

        if not str(game_root) or str(game_root) == ".":
            return jsonify({"ok": False, "error": "Please set a game folder."}), 400
        if not game_root.is_dir():
            return jsonify({"ok": False, "error": f"Game folder not found: {game_root}"}), 400
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return jsonify(
                {
                    "ok": False,
                    "error": (
                        f"Cannot create output folder {output_dir}: {e}. "
                        "Pick a writable Output folder (or leave it empty for "
                        f"{game_root / 'anyps5-out'})."
                    ),
                }
            ), 400

        if not get_relinker().is_file():
            return jsonify({"ok": False, "error": "relinker.exe missing. Rebuild first."}), 400
        libs = get_libs_dir()
        try:
            prx_files = list(libs.glob("*.prx")) if libs.is_dir() else []
        except OSError as e:
            return jsonify({"ok": False, "error": f"Cannot read libs folder {libs}: {e}"}), 400
        if not prx_files:
            return jsonify({"ok": False, "error": "No .prx libs. Rebuild first."}), 400

        try:
            elf = find_input_elf(game_root)
            mod = find_module_dir(game_root)
        except OSError as e:
            return jsonify(
                {
                    "ok": False,
                    "error": (
                        f"Cannot read game folder {game_root}: {e}. "
                        "Check the path and folder permissions."
                    ),
                }
            ), 400
        if not elf:
            return jsonify({"ok": False, "error": "No eboot.bin / *.elf in game folder."}), 400
        if not mod:
            return jsonify(
                {"ok": False, "error": "No sce_module / sce_modules / prx beside the ELF."}
            ), 400

        out_name = _json_str(data, "outputName") or str(cfg.get("outputName") or "app.exe")
        if not out_name.lower().endswith(".exe"):
            out_name += ".exe"
        out_exe = output_dir / out_name

        args = [str(get_relinker()), "--windows"]
        if data.get("toIntel", cfg.get("toIntel")):
            args.append("--to-intel")
        if data.get("windowsGui", cfg.get("windowsGui")):
            args.append("--windows-gui")
        uf = str(data.get("unusedFilter", cfg.get("unusedFilter", "0")))
        if uf not in ("0", "1", "2"):
            uf = "0"
        args.append(f"unused-filter={uf}")
        # Unity / engine titles ship modules outside sce_module (Media/Modules, Media/Plugins, …)
        extra_module_dirs = find_extra_module_dirs(elf.parent)
        for extra in extra_module_dirs:
            args.extend(["--module-dir", extra])
        args.extend(["--rpath", "$ORIGIN/libs", str(elf), str(out_exe)])
    except Exception as e:
        return jsonify({"ok": False, "error": f"Convert setup failed: {e}"}), 500

    def worker() -> None:
        game_label = _game_log_label(game_root)
        set_job(True, kind="Convert", ok=None, log_label=game_label)
        game_log = _attach_game_convert_log(output_dir, game_label)
        packaging_steps = 4
        set_job_progress(
            phase="relinker",
            label="Step 0/4 — Relinker converting binary (not done yet)",
            percent=5,
            detail="Wait for relinker. 'Conversion complete' only means the binary step finished.",
            rate="",
            busy=True,
            step=0,
            step_total=packaging_steps,
        )
        append_job(f"Game:   {game_label} ({game_root})")
        append_job(f"Input:  {elf}")
        append_job(f"Module: {mod}")
        if extra_module_dirs:
            append_job(f"Extra modules: {', '.join(extra_module_dirs)}")
        append_job(f"Output: {out_exe}")
        with _job_lock:
            hist_path = str(_job.get("logHistoryPath") or "")
        append_job(f"Convert log (send this): {hist_path}")
        if game_log:
            append_job(f"Copy beside game:       {game_log}")
        append_job("> " + " ".join(args))
        append_job("")
        append_job(
            "NOTE: After the relinker prints 'Conversion complete', packaging still runs "
            "(system libs → game files → MinGW DLLs). Do not start the game until you see "
            "'=== done ==='."
        )
        ok = True
        self_skips: list[str] = []
        try:
            proc = subprocess.Popen(
                args,
                cwd=str(get_repo_root()),
                env=tool_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                text = line.rstrip()
                append_job(text)
                low = text.lower()
                if "skipping self container" in low:
                    self_skips.append(text.strip())
                if "conversion complete" in low or "write output" in low:
                    set_job_progress(
                        phase="relinker",
                        label="Step 0/4 — Relinker finishing…",
                        percent=48,
                        detail="Binary step almost done — packaging comes next",
                    )
            if proc.wait() != 0:
                ok = False
                append_job("ERROR: exit code from relinker (see output above)")
                append_job("Relinker failed.")
            else:
                if self_skips:
                    append_job("")
                    append_job(
                        "[WARN] Relinker skipped SELF-wrapped module(s) "
                        f"({len(self_skips)}). Unwrap them before Convert or guest "
                        "modules will be missing (Converted modules: 0)."
                    )
                    for s in self_skips:
                        append_job(f"  {s}")
                append_job("")
                append_job("=" * 60)
                append_job("Relinker finished converting the binary.")
                append_job("Packaging is NOT done yet — do not start the game.")
                append_job(
                    "Remaining: 1) system libs  2) game files  3) MinGW DLLs  4) finish"
                )
                append_job("=" * 60)
                append_job("")

                # ---- 1/4 system libs ----
                set_job_progress(
                    phase="system_libs",
                    label="Step 1/4 — Copying system libraries (.prx)",
                    percent=52,
                    detail="Copying built .prx into output\\libs",
                    step=1,
                    step_total=packaging_steps,
                )
                libs_src = get_libs_dir()
                libs_dest = output_dir / "libs"
                libs_dest.mkdir(parents=True, exist_ok=True)
                prx_list = sorted(libs_src.glob("*.prx"), key=lambda p: p.name.lower())
                # Also note non-.prx files present in the build libs folder (not copied)
                other_in_libs = sorted(
                    p.name
                    for p in libs_src.iterdir()
                    if p.is_file() and p.suffix.lower() != ".prx"
                )
                append_job(f"[1/4] System libs source: {libs_src}")
                append_job(f"[1/4] System libs dest:   {libs_dest}")
                if other_in_libs:
                    append_job(
                        "[1/4] Non-.prx files in build libs (NOT copied — AnyPS5 docs "
                        f"only require *.prx here): {', '.join(other_in_libs)}"
                    )
                append_job(f"[1/4] Packaging file list — system .prx ({len(prx_list)}):")
                count = 0
                total_prx = max(len(prx_list), 1)
                libs_meter = _CopyRateMeter()
                set_job_progress(busy=True, rate="")
                for prx in prx_list:
                    dest = libs_dest / prx.name
                    data = prx.read_bytes()
                    dest.write_bytes(data)
                    libs_meter.add(len(data))
                    count += 1
                    append_job(f"  COPY  {prx.name}  →  libs\\{prx.name}  ({len(data)} bytes)")
                    if count == 1 or count == total_prx or count % 5 == 0:
                        pct = 52 + (10 * count / total_prx)
                        set_job_progress(
                            phase="system_libs",
                            label="Step 1/4 — Copying system libraries (.prx)",
                            percent=pct,
                            detail=(
                                f"{count}/{len(prx_list)} → libs\\{prx.name} · "
                                f"{_fmt_bytes(libs_meter.bytes_done)}"
                            ),
                            rate=libs_meter.rate_str,
                            busy=True,
                            step=1,
                            step_total=packaging_steps,
                        )
                append_job(f"[1/4] Copied {count} system libs (.prx) to {libs_dest}")
                set_job_progress(rate="", busy=False)

                # ---- 2/4 game resources ----
                set_job_progress(
                    phase="game_files",
                    label="Step 2/4 — Copying game files into app0",
                    percent=65,
                    detail="This can take a while on large dumps — wait for step 3",
                    rate="",
                    busy=True,
                    step=2,
                    step_total=packaging_steps,
                )
                append_job("[2/4] Copying game resources into app0 (large dumps take time)…")
                app0 = output_dir / "app0"
                content_root = elf.parent
                _last_logged_item = {"name": "", "files": -1}

                def _res_progress(info) -> None:
                    idx = int(info.get("idx") or 0)
                    total = max(int(info.get("total") or 1), 1)
                    name = str(info.get("name") or "")
                    files_so_far = int(info.get("files") or 0)
                    dirs_so_far = int(info.get("dirs") or 0)
                    rate = str(info.get("rate") or "")
                    cur = str(info.get("current_file") or "")
                    bytes_done = int(info.get("bytes_done") or 0)
                    # Advance overall % by top-level item, with a soft nudge from bytes
                    # so a single huge folder still moves the bar a little.
                    frac = (idx - 1) / total
                    soft = min(0.9, bytes_done / (512 * 1024 * 1024)) * (1.0 / total)
                    pct = 65 + (20 * min(1.0, frac + soft))
                    detail_bits = [
                        f"Item {idx}/{total}: {name}",
                        f"{dirs_so_far} folder(s), {files_so_far} file(s)",
                        _fmt_bytes(bytes_done) + " copied",
                    ]
                    if cur:
                        detail_bits.append(cur)
                    set_job_progress(
                        phase="game_files",
                        label="Step 2/4 — Copying game files into app0",
                        percent=pct,
                        detail=" · ".join(detail_bits),
                        rate=rate,
                        busy=True,
                        step=2,
                        step_total=packaging_steps,
                    )
                    if (
                        name != _last_logged_item["name"]
                        or files_so_far - _last_logged_item["files"] >= 500
                        or idx == 1
                        or idx == total
                    ):
                        _last_logged_item["name"] = name
                        _last_logged_item["files"] = files_so_far
                        append_job(
                            f"  … game files {idx}/{total}: {name} "
                            f"({files_so_far} files, {_fmt_bytes(bytes_done)}"
                            + (f", {rate}" if rate else "")
                            + ")"
                        )

                res_files, res_dirs, skipped, res_manifest = copy_game_resources_to_app0(
                    content_root,
                    app0,
                    output_dir=output_dir,
                    on_progress=_res_progress,
                )
                set_job_progress(rate="", busy=False)
                append_job("[2/4] Packaging file list — top-level game items:")
                for line in res_manifest:
                    append_job(f"  {line}")
                append_job(
                    f"[2/4] Copied game resources into {app0} "
                    f"({res_dirs} folder(s), {res_files} file(s); "
                    "skipped eboot/elf/prx and module dirs)"
                )
                if skipped:
                    append_job(
                        f"[2/4] Skipped nested output path(s) inside game folder: "
                        f"{', '.join(skipped)} (avoids re-copying the package into itself)"
                    )

                # Drop any leftover raw .prx next to *.guest.prx (older converts / ignore misses)
                removed_raw = 0
                try:
                    for guest in app0.rglob("*.guest.prx"):
                        raw = guest.with_name(guest.name.replace(".guest.prx", ""))
                        # e.g. Il2CppUserAssemblies.prx.guest.prx -> Il2CppUserAssemblies.prx
                        if raw.name.endswith(".prx") and raw.is_file() and raw != guest:
                            raw.unlink()
                            removed_raw += 1
                    # Also strip *.prx that are not *.guest.prx under known module folders
                    for sub in ("sce_module", "sce_modules", "prx", "Modules", "Plugins"):
                        for folder in app0.rglob(sub):
                            if not folder.is_dir():
                                continue
                            for prx in folder.glob("*.prx"):
                                if prx.name.endswith(".guest.prx"):
                                    continue
                                prx.unlink()
                                removed_raw += 1
                except OSError as e:
                    append_job(f"[WARN] Could not clean raw modules under app0: {e}")
                if removed_raw:
                    append_job(f"Removed {removed_raw} raw (unconverted) .prx under app0")

                # ---- 3/4 MinGW DLLs (required — prevents GetLastError 126) ----
                set_job_progress(
                    phase="mingw",
                    label="Step 3/4 — Copying MinGW runtime DLLs (prevents error 126)",
                    percent=90,
                    detail="libgcc / libstdc++ / libwinpthread into libs and app0",
                    step=3,
                    step_total=packaging_steps,
                )
                append_job(
                    "[3/4] Copying MinGW runtime DLLs (required — without these, "
                    "launch fails with GetLastError 126)…"
                )
                mingw_sources = find_mingw_runtime_sources()
                append_job("[3/4] MinGW DLL sources resolved:")
                for name in MINGW_RUNTIME_DLLS:
                    src = mingw_sources.get(name)
                    if src:
                        try:
                            sz = src.stat().st_size
                        except OSError:
                            sz = 0
                        append_job(f"  SRC   {name}  ←  {src}  ({sz} bytes)")
                    else:
                        append_job(f"  MISSING SOURCE  {name}")
                dests = mingw_destinations_for_output(output_dir)
                append_job(
                    f"[3/4] MinGW copy destinations ({len(dests)}): "
                    + "; ".join(str(d) for d in dests)
                )
                dll_n, dll_missing, dll_srcs, dll_details = copy_mingw_runtime_dlls(*dests)
                append_job("[3/4] Packaging file list — MinGW runtime DLLs:")
                for name, dest, size, copied_ok in dll_details:
                    status = "OK" if copied_ok else "FAIL"
                    append_job(f"  {status}  {name}  →  {dest}  ({size} bytes)")
                if dll_n:
                    append_job(
                        f"[3/4] Copied MinGW runtime DLL(s) ×{dll_n} "
                        f"({', '.join(MINGW_RUNTIME_DLLS)}; "
                        f"from {dll_srcs[0] if dll_srcs else '?'})"
                    )
                still_missing = verify_mingw_in_libs(libs_dest)
                append_job("[3/4] Post-copy verify (libs\\ must have all three):")
                for name in MINGW_RUNTIME_DLLS:
                    p = libs_dest / name
                    if p.is_file():
                        try:
                            sz = p.stat().st_size
                        except OSError:
                            sz = 0
                        append_job(f"  VERIFY OK  libs\\{name}  ({sz} bytes)")
                    else:
                        append_job(f"  VERIFY MISSING  libs\\{name}")
                if still_missing or dll_missing:
                    ok = False
                    append_job(
                        "[ABBRUCH] Missing MinGW DLL(s): "
                        f"{', '.join(still_missing or dll_missing)}. "
                        f"Looked under {WINLIBS_BIN} and gcc on PATH. "
                        "Run Setup → Install / repair (WinLibs), then Convert again. "
                        "Without these, launch fails with GetLastError 126 "
                        "(e.g. Failed to load module: libs\\*.prx)."
                    )

                # ---- 4/4 finish ----
                set_job_progress(
                    phase="finish",
                    label="Step 4/4 — Finishing (input config + save paths)",
                    percent=96,
                    detail="Almost done",
                    step=4,
                    step_total=packaging_steps,
                )
                append_job("[4/4] Writing input config and saving paths…")
                dest_ini = output_dir / "anyps5-input.ini"
                try:
                    if INPUT_INI_PATH.is_file():
                        dest_ini.write_bytes(INPUT_INI_PATH.read_bytes())
                    elif INPUT_PROFILE_PATH.is_file():
                        prof = json.loads(INPUT_PROFILE_PATH.read_text(encoding="utf-8"))
                        dest_ini.write_text(
                            bindings_to_ini(prof.get("bindings") or default_keyboard_bindings()),
                            encoding="utf-8",
                        )
                    else:
                        dest_ini.write_text(
                            bindings_to_ini(default_keyboard_bindings()),
                            encoding="utf-8",
                        )
                    append_job(f"[4/4] Input config ready: {dest_ini}")
                except Exception as e:
                    append_job(f"[WARN] Input config not copied: {e}")
                cfg2 = load_config()
                cfg2["lastGameRoot"] = str(game_root)
                cfg2["lastOutputDir"] = str(output_dir)
                cfg2["lastOutputExe"] = str(out_exe)
                cfg2["outputName"] = out_name
                save_config(cfg2)
                try:
                    from library import finalize_converted_game

                    finalize_converted_game(output_dir, game_root)
                    append_job("[4/4] Library metadata + artwork written into game folder")
                except Exception as e:
                    append_job(f"[WARN] Library metadata: {e}")
                set_job_progress(
                    phase="done",
                    label="Convert finished — safe to start the game",
                    percent=100,
                    detail=str(out_exe),
                    step=4,
                    step_total=packaging_steps,
                )
                append_job("")
                append_job("=" * 60)
                append_job("PACKAGING MANIFEST SUMMARY")
                append_job(f"  Game label:     {game_label}")
                append_job(f"  System .prx:    {count} → {libs_dest}")
                append_job(f"  Game resources: {res_files} file(s), {res_dirs} folder(s) → {app0}")
                append_job(f"  MinGW DLLs:     {dll_n} file write(s) across {len(dests)} folder(s)")
                append_job(
                    f"  Required MinGW: {', '.join(MINGW_RUNTIME_DLLS)} "
                    + (
                        "ALL PRESENT in libs\\"
                        if not still_missing
                        else f"MISSING: {', '.join(still_missing)}"
                    )
                )
                with _job_lock:
                    hist_final = str(_job.get("logHistoryPath") or "")
                    game_final = str(_job.get("logGamePath") or "")
                append_job(f"  Convert log:    {hist_final}")
                if game_final:
                    append_job(f"  Beside game:    {game_final}")
                append_job("=" * 60)
                append_job(f"Done: {out_exe}")
                append_job("You can start the game now (Start game / the .exe).")
                append_job(
                    f"If launch fails: send convert log `{Path(hist_final).name}` "
                    "(or the convert-*.log next to the game exe)."
                )
        except Exception as e:
            ok = False
            append_job(f"ERROR: {e}")
        if not ok:
            note_job_failure_hints()
        append_job("=== done ===" if ok else "=== aborted ===")
        set_job(False, ok=ok)

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "elf": str(elf), "moduleDir": mod, "output": str(out_exe)})


@app.post("/api/start")
def api_start():
    import game_session
    from library import find_entry_by_id, mark_played

    cfg = load_config()
    data = request.get_json(silent=True) or {}
    exe = Path("")
    title = ""
    entry_id = str(data.get("id") or "").strip()
    exe_raw = str(data.get("exePath") or "").strip()
    if entry_id:
        entry = find_entry_by_id(entry_id)
        if not entry or entry.get("kind") != "converted":
            applog.warning("Start failed — library id not found", entry_id)
            return jsonify({"ok": False, "error": "Converted game not found. Scan library first."}), 404
        exe = Path(entry.get("exePath") or "")
        title = str(entry.get("title") or "")
    elif exe_raw:
        exe = Path(exe_raw)
        title = str(data.get("title") or exe.stem)
    else:
        exe = Path(cfg.get("lastOutputExe") or "")
        title = exe.stem
    if not exe.is_file():
        applog.warning("Start failed — exe missing", str(exe))
        return jsonify({"ok": False, "error": "No converted .exe yet. Convert first."}), 400
    if game_session.is_running():
        st = game_session.get_status()
        applog.warning("Start rejected — game already running", st.get("title"))
        return jsonify(
            {
                "ok": False,
                "error": f"Already running: {st.get('title') or 'a game'}. Close it before starting another.",
                "game": st,
            }
        ), 409
    if _job["running"]:
        return jsonify(
            {"ok": False, "error": f"Wait for {_job.get('kind') or 'the current job'} to finish."}
        ), 409
    applog.info("Starting game", str(exe))
    mingw_ok, mingw_detail = ensure_mingw_runtime_for_output(exe.parent)
    if not mingw_ok:
        return jsonify({"ok": False, "error": mingw_detail}), 400
    env = tool_env()
    gpu = str(data.get("gpuFilter") or cfg.get("gpuFilter") or "").strip()
    if gpu:
        env["ANYPS5_GPU"] = gpu
    # Default: show a separate game console so init/compile output is visible.
    if "hideGameConsole" in data:
        hide_console = bool(data.get("hideGameConsole"))
    elif "windowsGui" in data:
        # Legacy: Convert-tab "Hide console" was sometimes sent on start
        hide_console = bool(data.get("windowsGui"))
    else:
        hide_console = bool(cfg.get("hideGameConsole", False))
    try:
        session = game_session.start_game(
            exe,
            title=title or exe.stem,
            env=env,
            log_dir=LOGS_DIR,
            hide_console=hide_console,
        )
        cfg["lastOutputExe"] = str(exe)
        cfg["lastOutputDir"] = str(exe.parent)
        save_config(cfg)
        try:
            mark_played(exe.parent)
        except Exception:
            pass
        applog.info("Game started", f"pid={session.get('pid')} title={session.get('title')!r}")
        return jsonify(
            {
                "ok": True,
                "exe": str(exe),
                "mingw": mingw_detail,
                "game": session,
                "message": f"Game {session.get('title')} started and running",
            }
        )
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e), "game": game_session.get_status()}), 409
    except Exception as e:
        applog.error("Start failed", str(e))
        return jsonify({"ok": False, "error": str(e)}), 500


@app.get("/api/game/status")
def api_game_status():
    import game_session

    return jsonify({"ok": True, "game": game_session.get_status()})


@app.post("/api/game/stop")
def api_game_stop():
    import game_session

    st = game_session.stop_game(force=True)
    applog.info("Game stop requested", st.get("title"))
    return jsonify({"ok": True, "game": st})


@app.get("/api/library")
def api_library():
    from library import ensure_library_ready, get_progress, needs_first_scan

    cfg = load_config()
    conv = cfg.get("convertedGamesRoot") or ""
    unconv = cfg.get("unconvertedGamesRoot") or ""
    library = ensure_library_ready(conv, unconv)
    first = needs_first_scan(conv, unconv)
    return jsonify(
        {
            "ok": True,
            "library": library,
            "progress": get_progress(),
            "needsFirstScan": first,
        }
    )


@app.get("/api/library/progress")
def api_library_progress():
    from library import get_progress

    return jsonify({"ok": True, "progress": get_progress()})


@app.post("/api/library/scan")
def api_library_scan():
    from library import get_progress, start_scan_async

    if _job["running"]:
        applog.warning("Library scan rejected — job running", _job.get("kind"))
        return jsonify(
            {
                "ok": False,
                "error": f"Wait for {_job.get('kind') or 'the current job'} to finish before scanning.",
            }
        ), 409
    cfg = load_config()
    data = request.get_json(silent=True) or {}
    fetch_remote = data.get("fetchRemote", True)
    mode = str(data.get("mode") or "incremental").strip().lower()
    if mode not in ("first", "incremental", "full"):
        mode = "incremental"
    if not str(cfg.get("convertedGamesRoot") or "").strip() and not str(
        cfg.get("unconvertedGamesRoot") or ""
    ).strip():
        applog.warning("Library scan rejected — library folders not set")
        return jsonify(
            {
                "ok": False,
                "error": "Set Converted / Unconverted Game Folder in Settings first.",
            }
        ), 400
    applog.info(
        "Library scan requested",
        f"mode={mode} fetchRemote={bool(fetch_remote)} converted={cfg.get('convertedGamesRoot')!r} "
        f"unconverted={cfg.get('unconvertedGamesRoot')!r}",
    )
    started = start_scan_async(
        cfg.get("convertedGamesRoot") or "",
        cfg.get("unconvertedGamesRoot") or "",
        fetch_remote=bool(fetch_remote),
        mode=mode,
    )
    applog.info("Library scan " + ("started" if started else "already running"))
    msg = {
        "first": "First time scan started",
        "incremental": "Scanning for newly added games…",
        "full": "Full library scan started",
    }.get(mode, "Scan started")
    return jsonify(
        {
            "ok": True,
            "started": started,
            "mode": mode,
            "progress": get_progress(),
            "message": "Scan already running" if not started else msg,
        }
    )


@app.post("/api/library/prepare-convert")
def api_library_prepare_convert():
    from library import prepare_convert_paths

    cfg = load_config()
    data = request.get_json(silent=True) or {}
    path = str(data.get("path") or data.get("gameRoot") or "").strip()
    if not path:
        applog.warning("prepare-convert missing path")
        return jsonify({"ok": False, "error": "Missing game path."}), 400
    applog.info("prepare-convert", path)
    try:
        prepared = prepare_convert_paths(path, cfg.get("convertedGamesRoot") or "")
    except Exception as e:
        applog.error("prepare-convert failed", str(e))
        return jsonify({"ok": False, "error": str(e)}), 400
    applog.info(
        "prepare-convert ready",
        f"title={prepared.get('title')!r} output={prepared.get('outputDir')!r}",
    )
    # Settings converter defaults + game-named exe (not the global default app.exe)
    to_intel = cfg.get("toIntel")
    if isinstance(to_intel, str):
        to_intel = to_intel.strip().lower() in ("1", "true", "yes", "on")
    return jsonify(
        {
            "ok": True,
            **prepared,
            "defaults": {
                "outputName": prepared.get("suggestedOutputName")
                or cfg.get("outputName")
                or "app.exe",
                "gpuFilter": str(cfg.get("gpuFilter") or ""),
                "toIntel": bool(to_intel),
                "windowsGui": bool(cfg.get("windowsGui")),
                "unusedFilter": str(cfg.get("unusedFilter") if cfg.get("unusedFilter") is not None else "0"),
            },
        }
    )


@app.get("/api/library/file")
def api_library_file():
    from flask import send_file

    from library import path_allowed, resolve_game_file

    entry_id = str(request.args.get("id") or "").strip()
    which = str(request.args.get("which") or "cover").strip().lower()
    if which not in ("cover", "backdrop"):
        which = "cover"
    cfg = load_config()
    conv = str(cfg.get("convertedGamesRoot") or "")
    unconv = str(cfg.get("unconvertedGamesRoot") or "")
    path = resolve_game_file(entry_id, which, conv, unconv)
    if not path:
        return jsonify({"ok": False, "error": "Artwork not found."}), 404
    if not path_allowed(path, conv, unconv):
        return jsonify({"ok": False, "error": "Forbidden."}), 403
    return send_file(path)


@app.get("/api/setup/audit")
def api_setup_audit():
    from setup_engine import audit, get_progress, is_admin

    lines: list[str] = []
    report = audit(get_repo_root(), log=lines.append)
    return jsonify(
        {
            "ok": True,
            "audit": report.to_dict(),
            "log": lines,
            "progress": get_progress(),
            "launcherIsAdmin": is_admin(),
            "installTarget": str(get_repo_root()),
            "adminWhy": {
                "title": "Why administrator access may be needed",
                "body": (
                    "Installing Git, Python, or CMake usually writes into Program Files. "
                    "WinLibs is extracted under C:\\winlibs. winget package installs often "
                    "require elevation. This launcher window stays non-admin; UAC only elevates "
                    "the installer helper. If tools are already installed, you can install without admin."
                ),
            },
        }
    )


@app.get("/api/setup/progress")
def api_setup_progress():
    from setup_engine import get_progress

    return jsonify({"ok": True, "progress": get_progress()})


@app.post("/api/setup/install")
def api_setup_install():
    if _job["running"]:
        return jsonify({"ok": False, "error": "A job is already running."}), 409

    data = request.get_json(silent=True) or {}
    steps = data.get("steps")  # optional list
    elevate = bool(data.get("elevate", False))
    # allowWithoutAdmin: proceed without UAC (may fail on Program Files / C:\winlibs)

    if elevate:
        # Elevated helper installs tools into Program Files / C:\winlibs.
        # This UI process stays non-admin; user must Re-check afterwards.
        ps1 = LAUNCHER_DIR / "bootstrap.ps1"
        if not ps1.is_file():
            return jsonify({"ok": False, "error": "bootstrap.ps1 missing"}), 500
        try:
            root = get_repo_root()
            root.mkdir(parents=True, exist_ok=True)
            # Full install (not LauncherOnly) so clone + tools run elevated
            subprocess.Popen(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    f"Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \"{ps1}\" -RepoRoot \"{root}\" -Elevated -NoLaunch'",
                ],
                cwd=str(DATA_DIR),
            )
            return jsonify(
                {
                    "ok": True,
                    "elevated": True,
                    "message": (
                        "UAC opened an admin installer window. This launcher stays non-admin. "
                        "when the other window finishes, click Re-check."
                    ),
                }
            )
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    # Ensure clone target exists under the launcher folder
    get_repo_root().mkdir(parents=True, exist_ok=True)

    def worker() -> None:
        from setup_engine import finish_progress, full_install

        set_job(True, kind="Setup / Installation", ok=None)
        append_job("=== Setup / Installation started ===")
        append_job(f"Install target: {get_repo_root()}")
        try:
            ok = full_install(get_repo_root(), append_job, steps=steps)
        except Exception as e:
            ok = False
            append_job(f"ERROR: {e}")
            try:
                finish_progress(False)
            except Exception:
                pass
        if not ok:
            note_job_failure_hints()
        append_job("=== done ===" if ok else "=== aborted ===")
        set_job(False, ok=ok)

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "elevated": False})


@app.get("/api/input/export")
def api_input_export():
    """Download shareable JSON keyboard/mouse profile."""
    from flask import Response

    profile = {
        "format": "anyps5-launcher-input-v1",
        "bindings": default_keyboard_bindings(),
    }
    if INPUT_PROFILE_PATH.is_file():
        try:
            loaded = json.loads(INPUT_PROFILE_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded.get("bindings"), dict):
                profile["bindings"] = loaded["bindings"]
        except Exception:
            pass
    elif INPUT_INI_PATH.is_file():
        profile["bindings"] = ini_to_bindings(INPUT_INI_PATH.read_text(encoding="utf-8"))

    body = json.dumps(profile, indent=2)
    return Response(
        body,
        mimetype="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="anyps5-input-profile.json"'
        },
    )


@app.get("/api/input/export.ini")
def api_input_export_ini():
    from flask import Response

    if INPUT_INI_PATH.is_file():
        text = INPUT_INI_PATH.read_text(encoding="utf-8")
    elif INPUT_PROFILE_PATH.is_file():
        try:
            prof = json.loads(INPUT_PROFILE_PATH.read_text(encoding="utf-8"))
            text = bindings_to_ini(prof.get("bindings") or default_keyboard_bindings())
        except Exception:
            text = bindings_to_ini(default_keyboard_bindings())
    else:
        text = bindings_to_ini(default_keyboard_bindings())
    return Response(
        text,
        mimetype="text/plain",
        headers={"Content-Disposition": 'attachment; filename="anyps5-input.ini"'},
    )


@app.post("/api/input/import")
def api_input_import():
    """Import JSON profile or anyps5-input.ini text/file."""
    data = request.get_json(silent=True) or {}
    raw = data.get("content") or data.get("text") or ""
    name = (data.get("filename") or "").lower()

    if not raw and request.files:
        f = request.files.get("file")
        if f:
            raw = f.read().decode("utf-8", errors="replace")
            name = (f.filename or "").lower()

    if not raw.strip():
        return jsonify({"ok": False, "error": "No import content."}), 400

    bindings = {}
    try:
        if name.endswith(".json") or raw.lstrip().startswith("{"):
            obj = json.loads(raw)
            bindings = obj.get("bindings") or {}
            if not bindings and isinstance(obj, dict):
                bindings = {
                    k: ([v] if isinstance(v, str) else v)
                    for k, v in obj.items()
                    if k not in ("format", "controllerType", "controllerName", "updatedAt", "padMap")
                }
        else:
            bindings = ini_to_bindings(raw)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Import parse failed: {e}"}), 400

    if not bindings:
        return jsonify({"ok": False, "error": "No bindings found in import."}), 400

    profile = {
        "format": "anyps5-launcher-input-v1",
        "bindings": bindings,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    INPUT_PROFILE_PATH.write_text(json.dumps(profile, indent=2), encoding="utf-8")
    INPUT_INI_PATH.write_text(bindings_to_ini(bindings), encoding="utf-8")
    return jsonify({"ok": True, "profile": profile})


def main() -> None:
    port = int(os.environ.get("ANYPS5_LAUNCHER_PORT", "8765"))
    # ?boot=1 triggers the logo intro once; F5 uses a clean URL without it
    url = f"http://127.0.0.1:{port}/?boot=1"
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except Exception:
        pass
    print(f"AnyPS5 Launcher v{LAUNCHER_VERSION}  pid={os.getpid()}", flush=True)
    print(f"UI: http://127.0.0.1:{port}/", flush=True)
    print("Leave this window open. Press Ctrl+C to quit.", flush=True)
    print("Update progress is printed here when you click Update Now.", flush=True)
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    # Waitress: local UI server without Flask's "development server" warning
    import logging

    from waitress import serve

    logging.getLogger("waitress").setLevel(logging.ERROR)
    serve(app, host="127.0.0.1", port=port, threads=8)


if __name__ == "__main__":
    main()
