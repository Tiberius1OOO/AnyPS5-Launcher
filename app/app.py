#!/usr/bin/env python3
"""AnyPS5 graphical launcher backend."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
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
CONFIG_PATH = DATA_DIR / "launcher-config.json"
INPUT_INI_PATH = DATA_DIR / "anyps5-input.ini"
INPUT_PROFILE_PATH = DATA_DIR / "input-profile.json"


def get_repo_root() -> Path:
    cfg = load_config()
    raw = str(cfg.get("anyps5Root") or "").strip().strip('"')
    if raw:
        return Path(raw)
    return Path(os.environ.get("USERPROFILE") or str(Path.home())) / "AnyPS5"


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
}


def _default_config() -> dict:
    return {
        "anyps5Root": str(Path(os.environ.get("USERPROFILE") or str(Path.home())) / "AnyPS5"),
        "lastGameRoot": "",
        "lastOutputDir": "",
        "lastOutputExe": "",
        "outputName": "app.exe",
        "gpuFilter": "",
        "toIntel": False,
        "windowsGui": False,
        "unusedFilter": "0",
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
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def tool_env() -> dict:
    env = os.environ.copy()
    parts = [
        str(CMAKE_BIN),
        str(WINLIBS_BIN),
        env.get("Path", env.get("PATH", "")),
    ]
    env["Path"] = ";".join(p for p in parts if p)
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


def append_job(line: str) -> None:
    with _job_lock:
        _job["lines"].append(line)
        if len(_job["lines"]) > 4000:
            _job["lines"] = _job["lines"][-3000:]


def set_job(running: bool, kind: str = "", ok: bool | None = None) -> None:
    with _job_lock:
        _job["running"] = running
        if kind:
            _job["kind"] = kind
        _job["ok"] = ok
        if running:
            _job["lines"] = []
            _job["started"] = datetime.now(timezone.utc).isoformat()
            _job["finished"] = None
        else:
            _job["finished"] = datetime.now(timezone.utc).isoformat()


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


def run_job(kind: str, commands: list[list[str]], on_success=None) -> None:
    if _job["running"]:
        return

    def worker() -> None:
        set_job(True, kind=kind, ok=None)
        append_job(f"=== {kind} started ===")
        ok = True
        for cmd in commands:
            append_job("> " + " ".join(cmd))
            try:
                proc = subprocess.Popen(
                    cmd,
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
                    append_job(line.rstrip())
                code = proc.wait()
                if code != 0:
                    append_job(f"ERROR: exit code {code}")
                    ok = False
                    break
            except Exception as e:
                append_job(f"ERROR: {e}")
                ok = False
                break
        if ok and on_success:
            try:
                on_success()
            except Exception as e:
                append_job(f"Post-step failed: {e}")
                ok = False
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

    if not ready:
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
    return jsonify(
        {
            "repoRoot": str(get_repo_root()),
            "githubUrl": GITHUB_URL,
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


@app.post("/api/open-github")
def api_open_github():
    try:
        webbrowser.open(GITHUB_URL)
        return jsonify({"ok": True, "url": GITHUB_URL})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "url": GITHUB_URL}), 500


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
        return jsonify(dict(_job))


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
    return jsonify({"ok": True, "config": cfg})


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
    cores = str(os.cpu_count() or 4)
    cmds = [
        [
            "cmake",
            "-S",
            str(get_repo_root()),
            "-B",
            str(get_build_dir()),
            "-G",
            "Ninja",
            "-DCMAKE_BUILD_TYPE=Release",
            "-DCMAKE_C_COMPILER=gcc",
            "-DCMAKE_CXX_COMPILER=g++",
        ],
        ["cmake", "--build", str(get_build_dir()), "--parallel", cores],
        ["cmake", "--build", str(get_build_dir()), "--target", "libs", "--parallel", cores],
    ]
    run_job("Build", cmds, on_success=mark_build_current)
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


@app.post("/api/convert")
def api_convert():
    if _job["running"]:
        return jsonify({"ok": False, "error": "A job is already running."}), 409

    data = request.get_json(silent=True) or {}
    game_root = Path(data.get("gameRoot", "").strip().strip('"'))
    output_dir = Path(data.get("outputDir", "").strip().strip('"'))
    cfg = load_config()

    if not game_root.is_dir():
        return jsonify({"ok": False, "error": f"Game folder not found: {game_root}"}), 400
    if not str(output_dir):
        output_dir = game_root / "anyps5-out"
    output_dir.mkdir(parents=True, exist_ok=True)

    if not get_relinker().is_file():
        return jsonify({"ok": False, "error": "relinker.exe missing. Rebuild first."}), 400
    libs = get_libs_dir()
    if not libs.is_dir() or not list(libs.glob("*.prx")):
        return jsonify({"ok": False, "error": "No .prx libs. Rebuild first."}), 400

    elf = find_input_elf(game_root)
    if not elf:
        return jsonify({"ok": False, "error": "No eboot.bin / *.elf in game folder."}), 400
    mod = find_module_dir(game_root)
    if not mod:
        return jsonify(
            {"ok": False, "error": "No sce_module / sce_modules / prx beside the ELF."}
        ), 400

    out_name = data.get("outputName") or cfg.get("outputName") or "app.exe"
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
    args.extend(["--rpath", "$ORIGIN/libs", str(elf), str(out_exe)])

    def worker() -> None:
        set_job(True, kind="Convert", ok=None)
        append_job(f"Input:  {elf}")
        append_job(f"Module: {mod}")
        append_job(f"Output: {out_exe}")
        append_job("> " + " ".join(args))
        ok = True
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
                append_job(line.rstrip())
            if proc.wait() != 0:
                ok = False
                append_job("Relinker failed.")
            else:
                libs_dest = output_dir / "libs"
                libs_dest.mkdir(parents=True, exist_ok=True)
                count = 0
                for prx in get_libs_dir().glob("*.prx"):
                    dest = libs_dest / prx.name
                    dest.write_bytes(prx.read_bytes())
                    count += 1
                (output_dir / "app0").mkdir(parents=True, exist_ok=True)
                append_job(f"Copied {count} system libs to {libs_dest}")
                # Ship keyboard/mouse config next to the exe
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
                    append_job(f"Input config ready: {dest_ini}")
                except Exception as e:
                    append_job(f"[WARN] Input config not copied: {e}")
                cfg2 = load_config()
                cfg2["lastGameRoot"] = str(game_root)
                cfg2["lastOutputDir"] = str(output_dir)
                cfg2["lastOutputExe"] = str(out_exe)
                cfg2["outputName"] = out_name
                save_config(cfg2)
                append_job(f"Done: {out_exe}")
        except Exception as e:
            ok = False
            append_job(f"ERROR: {e}")
        append_job("=== done ===" if ok else "=== aborted ===")
        set_job(False, ok=ok)

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "elf": str(elf), "moduleDir": mod, "output": str(out_exe)})


@app.post("/api/start")
def api_start():
    cfg = load_config()
    exe = Path(cfg.get("lastOutputExe") or "")
    if not exe.is_file():
        return jsonify({"ok": False, "error": "No converted .exe yet. Convert first."}), 400
    env = tool_env()
    if cfg.get("gpuFilter"):
        env["ANYPS5_GPU"] = str(cfg["gpuFilter"])
    try:
        subprocess.Popen([str(exe)], cwd=str(exe.parent), env=env)
        return jsonify({"ok": True, "exe": str(exe)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.get("/api/setup/audit")
def api_setup_audit():
    from setup_engine import audit, get_progress

    lines: list[str] = []
    report = audit(get_repo_root(), log=lines.append)
    return jsonify(
        {
            "ok": True,
            "audit": report.to_dict(),
            "log": lines,
            "progress": get_progress(),
            "adminWhy": {
                "title": "Why administrator access may be needed",
                "body": (
                    "Installing Git, Python, or CMake usually writes into Program Files. "
                    "WinLibs is extracted under C:\\winlibs. winget package installs often "
                    "require elevation. The launcher itself does not need admin to run."
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
        # Relaunch elevated installer in a new console; user confirms UAC
        ps1 = LAUNCHER_DIR / "bootstrap.ps1"
        if not ps1.is_file():
            return jsonify({"ok": False, "error": "bootstrap.ps1 missing"}), 500
        try:
            root = get_repo_root()
            root.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    f"Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \"{ps1}\" -RepoRoot \"{root}\" -Elevated -NoLaunch -LauncherOnly'",
                ],
                cwd=str(DATA_DIR),
            )
            return jsonify(
                {
                    "ok": True,
                    "elevated": True,
                    "message": "UAC prompt opened. After admin install, click Re-check.",
                }
            )
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    def worker() -> None:
        from setup_engine import finish_progress, full_install

        set_job(True, kind="Setup / Installation", ok=None)
        append_job("=== Setup / Installation started ===")
        try:
            ok = full_install(get_repo_root(), append_job, steps=steps)
        except Exception as e:
            ok = False
            append_job(f"ERROR: {e}")
            try:
                finish_progress(False)
            except Exception:
                pass
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
    print(f"AnyPS5 Launcher: http://127.0.0.1:{port}/")
    print("Leave this window open. Press Ctrl+C to quit.")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    # Waitress: local UI server without Flask's "development server" warning
    import logging

    from waitress import serve

    logging.getLogger("waitress").setLevel(logging.ERROR)
    serve(app, host="127.0.0.1", port=port, threads=8)


if __name__ == "__main__":
    main()
