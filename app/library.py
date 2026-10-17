"""Game library: folder scan, portable per-game metadata/art, online metadata fetch."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any, Callable

SIDECAR_NAME = "anyps5-library.json"
ART_DIR_NAME = "anyps5-art"
COVER_NAME = "icon0.png"
BACKDROP_NAME = "backdrop.png"
LIBRARY_CACHE_NAME = "library-cache.json"

TITLE_ID_RE = re.compile(r"\b((?:PPSA|CUSA|NPXS|PSPE)[0-9]{4,6})\b", re.I)
PROSPERO_CDN_RE = re.compile(
    r"https://cdn\.prosperopatches\.com/[^\s\"'<>]+", re.I
)
ORBIS_CDN_RE = re.compile(r"https://cdn\.orbispatches\.com/[^\s\"'<>]+", re.I)
META_IMG_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]+content=["\']([^"\']+)["\']',
    re.I,
)
META_IMG_RE_ALT = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\']',
    re.I,
)
TITLE_TAG_RE = re.compile(r"<title[^>]*>\s*([^<]+?)\s*</title>", re.I)

USER_AGENT = "AnyPS5-Launcher/1.3 (+https://github.com/Tiberius1OOO/AnyPS5-Launcher)"

_progress_lock = threading.Lock()
_progress: dict[str, Any] = {
    "running": False,
    "phase": "idle",
    "phaseLabel": "",
    "percent": None,
    "detail": "",
    "rate": "",
    "gameIndex": 0,
    "gameTotal": 0,
    "ok": None,
    "error": "",
    "started": None,
    "finished": None,
}

_last_scan_lock = threading.Lock()
_last_scan: dict[str, Any] = {
    "converted": [],
    "unconverted": [],
    "scannedAt": "",
    "convertedRoot": "",
    "unconvertedRoot": "",
    "convertedTotalBytes": 0,
    "unconvertedTotalBytes": 0,
    "convertedTotalLabel": "",
    "unconvertedTotalLabel": "",
    "initialScanDone": False,
}

_scan_thread: threading.Thread | None = None
_size_fill_thread: threading.Thread | None = None
_cache_path: Path | None = None
_library_ready = False
_pending_new: list[dict[str, Any]] = []
_size_fill_lock = threading.Lock()
_size_fill_running = False


def configure_cache(data_dir: Path | str) -> None:
    """Point library cache at the launcher data directory."""
    global _cache_path, _library_ready
    _cache_path = Path(data_dir) / LIBRARY_CACHE_NAME
    _library_ready = False


def get_progress() -> dict[str, Any]:
    with _progress_lock:
        return dict(_progress)


def get_last_scan() -> dict[str, Any]:
    with _last_scan_lock:
        return {
            "converted": list(_last_scan.get("converted") or []),
            "unconverted": list(_last_scan.get("unconverted") or []),
            "scannedAt": _last_scan.get("scannedAt") or "",
            "convertedRoot": _last_scan.get("convertedRoot") or "",
            "unconvertedRoot": _last_scan.get("unconvertedRoot") or "",
            "convertedTotalBytes": int(_last_scan.get("convertedTotalBytes") or 0),
            "unconvertedTotalBytes": int(_last_scan.get("unconvertedTotalBytes") or 0),
            "convertedTotalLabel": _last_scan.get("convertedTotalLabel") or "",
            "unconvertedTotalLabel": _last_scan.get("unconvertedTotalLabel") or "",
            "initialScanDone": bool(_last_scan.get("initialScanDone")),
            "pendingNew": list(_pending_new),
            "fingerprint": str(_last_scan.get("fingerprint") or ""),
        }


def _set_progress(**kwargs: Any) -> None:
    with _progress_lock:
        _progress.update(kwargs)


def _fmt_bytes(n: float) -> str:
    n = float(max(0, n))
    for unit, div in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{int(n)} B"


def _fmt_rate(bps: float) -> str:
    if bps <= 0:
        return ""
    return f"{_fmt_bytes(bps)}/s"


def stable_id(path: Path | str) -> str:
    try:
        key = str(Path(path).resolve()).lower().replace("/", "\\")
    except Exception:
        key = str(path).lower()
    return hashlib.sha1(key.encode("utf-8", errors="replace")).hexdigest()[:16]


def safe_folder_name(name: str, fallback: str = "Game") -> str:
    raw = (name or "").strip() or fallback
    # Drop apostrophes so "Demon's Souls" → "Demons Souls" (not Demon_s)
    raw = raw.replace("'", "").replace("\u2019", "")
    out = "".join(c if (c.isalnum() or c in " -_.()[]") else "_" for c in raw)
    out = re.sub(r"_+", "_", out)
    out = re.sub(r"\s+", " ", out).strip(" ._")
    return (out or fallback)[:80]


def _read_json(path: Path) -> dict | None:
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
    except Exception:
        pass
    return None


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_sidecar(game_dir: Path) -> dict:
    data = _read_json(game_dir / SIDECAR_NAME) or {}
    return data if isinstance(data, dict) else {}


def save_sidecar(game_dir: Path, data: dict) -> None:
    cur = load_sidecar(game_dir)
    cur.update(data)
    cur["updatedAt"] = datetime.now(timezone.utc).isoformat()
    _write_json(game_dir / SIDECAR_NAME, cur)


def find_input_elf(game_root: Path) -> Path | None:
    for name in ("eboot.bin",):
        p = game_root / name
        if p.is_file():
            return p
    try:
        elfs = sorted(game_root.glob("*.elf"))
    except OSError:
        elfs = []
    if elfs:
        return elfs[0]
    try:
        children = list(game_root.iterdir())
    except OSError:
        return None
    for child in children:
        if not child.is_dir():
            continue
        for name in ("eboot.bin",):
            p = child / name
            if p.is_file():
                return p
        try:
            elfs = sorted(child.glob("*.elf"))
        except OSError:
            elfs = []
        if elfs:
            return elfs[0]
    return None


def _looks_like_dump(game_root: Path) -> bool:
    markers = (
        "sce_module",
        "sce_modules",
        "prx",
        "sce_sys",
        "app0",
        "Media",
    )
    for name in markers:
        if (game_root / name).exists():
            return True
    try:
        for child in game_root.iterdir():
            if child.is_dir() and child.name in markers:
                return True
            if child.is_dir() and (child / "sce_sys").is_dir():
                return True
    except OSError:
        pass
    return find_input_elf(game_root) is not None


def _find_exe(game_dir: Path) -> Path | None:
    preferred = ("app.exe", "eboot.exe")
    for name in preferred:
        p = game_dir / name
        if p.is_file():
            return p
    try:
        exes = sorted(p for p in game_dir.glob("*.exe") if p.is_file())
    except OSError:
        return None
    # Skip installers / obvious non-game tools
    skip = {"uninstall", "setup", "vcruntime", "vcredist"}
    for p in exes:
        if any(s in p.stem.lower() for s in skip):
            continue
        return p
    return exes[0] if exes else None


def is_converted_dir(game_dir: Path) -> bool:
    if not game_dir.is_dir():
        return False
    exe = _find_exe(game_dir)
    if not exe:
        return False
    return (game_dir / "libs").is_dir() or (game_dir / "app0").is_dir()


def is_unconverted_dir(game_dir: Path) -> bool:
    if not game_dir.is_dir():
        return False
    if is_converted_dir(game_dir):
        return False
    return find_input_elf(game_dir) is not None and _looks_like_dump(game_dir)


def _walk_files(root: Path, max_depth: int = 4) -> list[Path]:
    out: list[Path] = []
    root = Path(root)
    if not root.is_dir():
        return out

    def walk(cur: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = list(cur.iterdir())
        except OSError:
            return
        for ent in entries:
            try:
                if ent.is_file():
                    out.append(ent)
                elif ent.is_dir() and ent.name.lower() not in {
                    "libs",
                    "anyps5-art",
                    ".git",
                    "__pycache__",
                }:
                    walk(ent, depth + 1)
            except OSError:
                continue

    walk(root, 0)
    return out


def _parse_param_json(parsed: dict) -> tuple[str, str, str]:
    title_id = str(parsed.get("titleId") or parsed.get("TITLE_ID") or "").strip()
    version = str(
        parsed.get("contentVersion")
        or parsed.get("appVersion")
        or parsed.get("VERSION")
        or ""
    ).strip()
    title = ""
    loc = parsed.get("localizedParameters") or {}
    if isinstance(loc, dict):
        def_lang = loc.get("defaultLanguage") or "en-US"
        lang_block = loc.get(def_lang) or loc.get("en-US") or {}
        if isinstance(lang_block, dict):
            title = str(lang_block.get("titleName") or lang_block.get("title") or "").strip()
        if not title:
            for v in loc.values():
                if isinstance(v, dict):
                    title = str(v.get("titleName") or v.get("title") or "").strip()
                    if title:
                        break
    if not title:
        title = str(parsed.get("titleName") or parsed.get("title") or "").strip()
    return title, title_id, version


def inspect_game_folder(game_dir: Path) -> dict[str, Any]:
    """Studio-style inspect: param.json, icon0, pic0/pic1, titleId."""
    game_dir = Path(game_dir)
    title = ""
    title_id = ""
    version = ""
    icon_path: Path | None = None
    backdrop_path: Path | None = None
    side = load_sidecar(game_dir)

    # Fast path: already have sidecar + art — skip walking multi-GB dumps
    cover_cached = game_dir / ART_DIR_NAME / COVER_NAME
    back_cached = game_dir / ART_DIR_NAME / BACKDROP_NAME
    if (
        side.get("titleId")
        and cover_cached.is_file()
        and cover_cached.stat().st_size >= 64
    ):
        return {
            "title": str(side.get("title") or game_dir.name),
            "titleId": str(side.get("titleId") or "").upper(),
            "version": str(side.get("version") or ""),
            "iconPath": str(cover_cached),
            "backdropPath": str(back_cached) if back_cached.is_file() else str(cover_cached),
        }

    # Prefer common PS5 dump layouts before a deep walk
    candidates_param = [
        game_dir / "sce_sys" / "param.json",
        game_dir / "app0" / "sce_sys" / "param.json",
        game_dir / "sce_sys" / "about" / "param.json",
    ]
    for p in candidates_param:
        if p.is_file():
            parsed = _read_json(p) or {}
            title, title_id, version = _parse_param_json(parsed)
            if title_id or title:
                break

    for rel in (
        ("sce_sys", "icon0.png"),
        ("app0", "sce_sys", "icon0.png"),
        ("sce_sys", "icon0.webp"),
    ):
        p = game_dir.joinpath(*rel)
        if p.is_file():
            icon_path = p
            break
    for rel in (
        ("sce_sys", "pic1.png"),
        ("sce_sys", "pic0.png"),
        ("app0", "sce_sys", "pic1.png"),
        ("app0", "sce_sys", "pic0.png"),
    ):
        p = game_dir.joinpath(*rel)
        if p.is_file():
            backdrop_path = p
            break

    # Deep walk only if still missing critical metadata/art
    if not title_id or not icon_path:
        files = _walk_files(game_dir, max_depth=3)
        for f in files:
            lower = f.name.lower()
            if lower == "param.json" and not title_id:
                parsed = _read_json(f) or {}
                t, tid, ver = _parse_param_json(parsed)
                if tid:
                    title_id = tid
                if t and not title:
                    title = t
                if ver and not version:
                    version = ver
            if lower in ("icon0.png", "icon0.webp") and icon_path is None:
                icon_path = f
            if lower == "pic1.png":
                backdrop_path = f
            elif lower == "pic0.png" and backdrop_path is None:
                backdrop_path = f

    if not title_id:
        title_id = str(side.get("titleId") or "").strip()
    if not title_id:
        m = TITLE_ID_RE.search(game_dir.name)
        if m:
            title_id = m.group(1).upper()
    if not title:
        title = str(side.get("title") or "").strip()
    if not title:
        title = game_dir.name.replace("_", " ").replace("-", " ").strip() or "Unknown"

    return {
        "title": title,
        "titleId": title_id.upper() if title_id else "",
        "version": version or str(side.get("version") or ""),
        "iconPath": str(icon_path) if icon_path else "",
        "backdropPath": str(backdrop_path) if backdrop_path else "",
    }


def _http_get(url: str, timeout: float = 12.0, max_bytes: int = 2_000_000) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        chunks: list[bytes] = []
        total = 0
        while True:
            block = resp.read(64 * 1024)
            if not block:
                break
            total += len(block)
            if total > max_bytes:
                raise ValueError("response too large")
            chunks.append(block)
        return b"".join(chunks)


def _download_file(
    url: str,
    dest: Path,
    on_progress: Callable[[int, int, float], None] | None = None,
    timeout: float = 30.0,
) -> int:
    """Download url to dest. Returns bytes written. on_progress(received, total_or_-1, bps)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    received = 0
    t0 = time.monotonic()
    last_t = t0
    last_n = 0
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        total = -1
        try:
            total = int(resp.headers.get("Content-Length") or -1)
        except Exception:
            total = -1
        with open(tmp, "wb") as out:
            while True:
                block = resp.read(64 * 1024)
                if not block:
                    break
                out.write(block)
                received += len(block)
                now = time.monotonic()
                if on_progress and (now - last_t >= 0.15 or received == total):
                    dt = max(0.001, now - last_t)
                    bps = (received - last_n) / dt
                    on_progress(received, total, bps)
                    last_t = now
                    last_n = received
    tmp.replace(dest)
    if on_progress:
        elapsed = max(0.001, time.monotonic() - t0)
        on_progress(received, received, received / elapsed)
    return received


def _parse_patches_html(html: str, title_id: str) -> dict[str, str]:
    title = ""
    cover = ""
    m = TITLE_TAG_RE.search(html)
    if m:
        raw = unescape(m.group(1)).strip()
        # "PPSA01285: Returnal" or "Title | ORBISPatches.com"
        if ":" in raw:
            title = raw.split(":", 1)[1].strip()
        else:
            title = raw
        title = re.split(r"\s*[|\u2013\u2014\-]\s*(?:ORBIS|PROSPERO|Patches)", title, maxsplit=1, flags=re.I)[0]
        title = re.sub(r"\s*[|].*$", "", title).strip()
    for rx in (META_IMG_RE, META_IMG_RE_ALT):
        im = rx.search(html)
        if im:
            cover = unescape(im.group(1)).strip()
            break
    if not cover:
        tid = title_id.upper()
        if tid.startswith("PPSA"):
            cm = PROSPERO_CDN_RE.search(html)
        else:
            cm = ORBIS_CDN_RE.search(html)
        if cm:
            cover = cm.group(0)
    # Allowlist CDN hosts only
    if cover and not re.match(
        r"^https://cdn\.(?:prosperopatches|orbispatches)\.com/", cover, re.I
    ):
        cover = ""
    return {"title": title, "coverUrl": cover}


def _fetch_title_from_serialstation(title_id: str) -> dict[str, str]:
    """Best-effort JSON/HTML lookup on serialstation (public title catalog)."""
    tid = title_id.upper()
    # Search API-style pages; tolerate failures.
    candidates = [
        f"https://serialstation.com/titles/{tid}",
        f"https://serialstation.com/games/{tid}",
    ]
    for url in candidates:
        try:
            raw = _http_get(url, timeout=8.0, max_bytes=800_000)
            html = raw.decode("utf-8", errors="replace")
            m = TITLE_TAG_RE.search(html)
            if not m:
                continue
            title = unescape(m.group(1)).strip()
            title = re.sub(r"\s*[|\-–].*$", "", title).strip()
            if title and tid.lower() not in title.lower():
                return {"title": title, "coverUrl": "", "source": url}
            if title:
                return {"title": title, "coverUrl": "", "source": url}
        except Exception:
            continue
    return {}


def fetch_online_metadata(title_id: str) -> dict[str, str]:
    """Try multiple public DBs: ProsperoPatches, OrbisPatches, SerialStation."""
    tid = (title_id or "").strip().upper()
    if not tid or not TITLE_ID_RE.fullmatch(tid):
        return {}
    urls: list[str] = []
    if tid.startswith("PPSA"):
        urls = [
            f"https://prosperopatches.com/{tid}",
            f"https://orbispatches.com/{tid}",
        ]
    elif tid.startswith("CUSA"):
        urls = [
            f"https://orbispatches.com/{tid}",
            f"https://prosperopatches.com/{tid}",
        ]
    else:
        urls = [
            f"https://prosperopatches.com/{tid}",
            f"https://orbispatches.com/{tid}",
        ]

    best: dict[str, str] = {}
    for url in urls:
        try:
            raw = _http_get(url, timeout=10.0, max_bytes=1_500_000)
            html = raw.decode("utf-8", errors="replace")
            if "404" in html[:200] and tid not in html[:400]:
                continue
            parsed = _parse_patches_html(html, tid)
            if parsed.get("title") or parsed.get("coverUrl"):
                parsed["source"] = url
                if parsed.get("coverUrl"):
                    return parsed
                if not best:
                    best = parsed
        except Exception:
            continue
    if best.get("title") and not best.get("coverUrl"):
        # Title from patches site; still try serialstation for a name refine only
        return best
    extra = _fetch_title_from_serialstation(tid)
    if extra.get("title") and not best.get("title"):
        best.update(extra)
    return best


def ensure_local_art_copied(game_dir: Path, inspected: dict[str, Any]) -> dict[str, str]:
    """Copy dump icon/backdrop into anyps5-art/ if present."""
    art = game_dir / ART_DIR_NAME
    art.mkdir(parents=True, exist_ok=True)
    cover_rel = ""
    backdrop_rel = ""
    icon_src = Path(inspected["iconPath"]) if inspected.get("iconPath") else None
    back_src = Path(inspected["backdropPath"]) if inspected.get("backdropPath") else None
    cover_dest = art / COVER_NAME
    back_dest = art / BACKDROP_NAME
    try:
        if icon_src and icon_src.is_file():
            if not cover_dest.is_file() or icon_src.stat().st_mtime > cover_dest.stat().st_mtime:
                shutil.copy2(icon_src, cover_dest)
            cover_rel = f"{ART_DIR_NAME}/{COVER_NAME}"
        elif cover_dest.is_file():
            cover_rel = f"{ART_DIR_NAME}/{COVER_NAME}"
    except OSError:
        pass
    try:
        if back_src and back_src.is_file():
            if not back_dest.is_file() or back_src.stat().st_mtime > back_dest.stat().st_mtime:
                shutil.copy2(back_src, back_dest)
            backdrop_rel = f"{ART_DIR_NAME}/{BACKDROP_NAME}"
        elif back_dest.is_file():
            backdrop_rel = f"{ART_DIR_NAME}/{BACKDROP_NAME}"
    except OSError:
        pass
    # Fallback: use cover as backdrop later in UI
    return {"cover": cover_rel, "backdrop": backdrop_rel}


def resolve_artwork_for_game(
    game_dir: Path,
    inspected: dict[str, Any],
    *,
    fetch_remote: bool = True,
    game_index: int = 0,
    game_total: int = 0,
) -> dict[str, str]:
    """Ensure anyps5-art exists; optionally download cover from online DBs."""
    paths = ensure_local_art_copied(game_dir, inspected)
    cover_file = game_dir / ART_DIR_NAME / COVER_NAME
    title_id = str(inspected.get("titleId") or "").strip().upper()
    title = str(inspected.get("title") or "").strip()

    need_remote = fetch_remote and title_id and (
        not cover_file.is_file() or cover_file.stat().st_size < 64
    )
    if need_remote:
        _set_progress(
            phase="artwork",
            phaseLabel=f"Downloading cover art — game {game_index} of {game_total}",
            detail=title or title_id,
            percent=(
                int(40 + (50 * (game_index - 1) / max(1, game_total)))
                if game_total
                else None
            ),
            rate="",
            gameIndex=game_index,
            gameTotal=game_total,
        )
        meta = fetch_online_metadata(title_id)
        if meta.get("title") and (
            not title or title.lower() == game_dir.name.lower().replace("_", " ")
        ):
            title = meta["title"]
            inspected["title"] = title
        cover_url = meta.get("coverUrl") or ""
        if cover_url:

            def on_prog(recv: int, total: int, bps: float) -> None:
                pct_dl = None
                if total > 0:
                    base = 40 + (50 * (game_index - 1) / max(1, game_total))
                    span = 50 / max(1, game_total)
                    pct_dl = int(base + span * (recv / total))
                _set_progress(
                    detail=(
                        f"{title or title_id} — {_fmt_bytes(recv)}"
                        + (f" / {_fmt_bytes(total)}" if total > 0 else "")
                    ),
                    rate=_fmt_rate(bps),
                    percent=pct_dl,
                    phaseLabel=f"Downloading cover art — game {game_index} of {game_total}",
                    gameIndex=game_index,
                    gameTotal=game_total,
                )

            try:
                _download_file(cover_url, cover_file, on_progress=on_prog)
                paths["cover"] = f"{ART_DIR_NAME}/{COVER_NAME}"
                # Use cover as backdrop if we still lack one
                back = game_dir / ART_DIR_NAME / BACKDROP_NAME
                if not back.is_file():
                    try:
                        shutil.copy2(cover_file, back)
                        paths["backdrop"] = f"{ART_DIR_NAME}/{BACKDROP_NAME}"
                    except OSError:
                        pass
            except Exception:
                pass

    if not paths.get("cover") and cover_file.is_file():
        paths["cover"] = f"{ART_DIR_NAME}/{COVER_NAME}"
    if not paths.get("backdrop"):
        back = game_dir / ART_DIR_NAME / BACKDROP_NAME
        if back.is_file():
            paths["backdrop"] = f"{ART_DIR_NAME}/{BACKDROP_NAME}"
        elif paths.get("cover"):
            paths["backdrop"] = paths["cover"]

    save_sidecar(
        game_dir,
        {
            "title": inspected.get("title") or title,
            "titleId": title_id,
            "version": inspected.get("version") or "",
            "cover": paths.get("cover") or "",
            "backdrop": paths.get("backdrop") or "",
        },
    )
    return paths


def _cover_urls(game_dir: Path, gid: str, cover_rel: str, backdrop_rel: str) -> tuple[str, str]:
    bust = ""
    try:
        art_file = game_dir / (cover_rel or f"{ART_DIR_NAME}/{COVER_NAME}")
        if art_file.is_file():
            bust = f"&t={int(art_file.stat().st_mtime)}"
    except OSError:
        bust = ""
    cover_url = f"/api/library/file?id={gid}&which=cover{bust}" if cover_rel else ""
    backdrop_url = (
        f"/api/library/file?id={gid}&which=backdrop{bust}" if backdrop_rel else ""
    )
    return cover_url, backdrop_url


def _entry_from_dir(
    game_dir: Path,
    kind: str,
    *,
    fetch_remote: bool,
    game_index: int,
    game_total: int,
    compute_size: bool = True,
    cached_size_bytes: int | None = None,
) -> dict[str, Any]:
    game_dir = Path(game_dir)
    inspected = inspect_game_folder(game_dir)
    side = load_sidecar(game_dir)
    if side.get("title") and not inspected.get("title"):
        inspected["title"] = side["title"]
    if side.get("titleId") and not inspected.get("titleId"):
        inspected["titleId"] = str(side["titleId"]).upper()

    # Skip remote download when cover art is already on disk
    cover_file = game_dir / ART_DIR_NAME / COVER_NAME
    has_cover = cover_file.is_file() and cover_file.stat().st_size >= 64
    art = resolve_artwork_for_game(
        game_dir,
        inspected,
        fetch_remote=bool(fetch_remote) and not has_cover,
        game_index=game_index,
        game_total=game_total,
    )
    side = load_sidecar(game_dir)
    exe = _find_exe(game_dir) if kind == "converted" else None
    cover_rel = art.get("cover") or side.get("cover") or ""
    backdrop_rel = art.get("backdrop") or side.get("backdrop") or cover_rel

    gid = stable_id(game_dir)
    cover_url, backdrop_url = _cover_urls(game_dir, gid, cover_rel, backdrop_rel)
    if cached_size_bytes is not None and int(cached_size_bytes) > 0:
        size_b = int(cached_size_bytes)
    elif compute_size:
        try:
            size_b = folder_size_bytes(game_dir)
        except Exception:
            size_b = 0
    else:
        size_b = 0
    if size_b > 0:
        try:
            save_sidecar(game_dir, {"sizeBytes": size_b})
        except Exception:
            pass
    return {
        "id": gid,
        "kind": kind,
        "title": str(side.get("title") or inspected.get("title") or game_dir.name),
        "titleId": str(side.get("titleId") or inspected.get("titleId") or ""),
        "version": str(side.get("version") or inspected.get("version") or ""),
        "path": str(game_dir.resolve()),
        "exePath": str(exe.resolve()) if exe else "",
        "exeName": exe.name if exe else "",
        "cover": cover_rel,
        "backdrop": backdrop_rel,
        "coverUrl": cover_url,
        "backdropUrl": backdrop_url,
        "lastPlayedAt": side.get("lastPlayedAt") or "",
        "folderName": game_dir.name,
        "sizeBytes": size_b,
        "sizeLabel": format_size_gb(size_b) if size_b else "",
    }


def _refresh_cached_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Keep a cached card if the folder still exists; refresh URLs/exe only."""
    path = Path(str(entry.get("path") or ""))
    if not path.is_dir():
        return None
    kind = str(entry.get("kind") or "")
    gid = str(entry.get("id") or stable_id(path))
    cover_rel = str(entry.get("cover") or "")
    backdrop_rel = str(entry.get("backdrop") or cover_rel)
    if not cover_rel:
        cover_path = path / ART_DIR_NAME / COVER_NAME
        if cover_path.is_file():
            cover_rel = f"{ART_DIR_NAME}/{COVER_NAME}"
    if not backdrop_rel:
        back_path = path / ART_DIR_NAME / BACKDROP_NAME
        if back_path.is_file():
            backdrop_rel = f"{ART_DIR_NAME}/{BACKDROP_NAME}"
        elif cover_rel:
            backdrop_rel = cover_rel
    cover_url, backdrop_url = _cover_urls(path, gid, cover_rel, backdrop_rel)
    exe = _find_exe(path) if kind == "converted" else None
    if kind == "converted" and exe is None:
        return None
    size_b = int(entry.get("sizeBytes") or 0)
    out = dict(entry)
    out.update(
        {
            "id": gid,
            "path": str(path.resolve()),
            "exePath": str(exe.resolve()) if exe else "",
            "exeName": exe.name if exe else "",
            "cover": cover_rel,
            "backdrop": backdrop_rel,
            "coverUrl": cover_url,
            "backdropUrl": backdrop_url,
            "folderName": path.name,
            "sizeBytes": size_b,
            "sizeLabel": format_size_gb(size_b) if size_b else (entry.get("sizeLabel") or ""),
            "lastPlayedAt": (load_sidecar(path).get("lastPlayedAt") or entry.get("lastPlayedAt") or ""),
        }
    )
    return out


def _norm_root(root: str | Path | None) -> str:
    if not root or not str(root).strip():
        return ""
    try:
        return str(Path(root).resolve())
    except Exception:
        return str(root).strip()


def _roots_match(cached: dict[str, Any], converted_root: str, unconverted_root: str) -> bool:
    return _norm_root(cached.get("convertedRoot")) == _norm_root(converted_root) and _norm_root(
        cached.get("unconvertedRoot")
    ) == _norm_root(unconverted_root)


def _empty_library(converted_root: str = "", unconverted_root: str = "") -> dict[str, Any]:
    return {
        "converted": [],
        "unconverted": [],
        "scannedAt": "",
        "convertedRoot": _norm_root(converted_root),
        "unconvertedRoot": _norm_root(unconverted_root),
        "convertedTotalBytes": 0,
        "unconvertedTotalBytes": 0,
        "convertedTotalLabel": "",
        "unconvertedTotalLabel": "",
        "initialScanDone": False,
    }


def _finalize_totals(result: dict[str, Any]) -> dict[str, Any]:
    converted = list(result.get("converted") or [])
    unconverted = list(result.get("unconverted") or [])
    conv_bytes = sum(int(g.get("sizeBytes") or 0) for g in converted)
    unconv_bytes = sum(int(g.get("sizeBytes") or 0) for g in unconverted)
    result["converted"] = converted
    result["unconverted"] = unconverted
    result["convertedTotalBytes"] = conv_bytes
    result["unconvertedTotalBytes"] = unconv_bytes
    result["convertedTotalLabel"] = format_size_gb(conv_bytes) if conv_bytes else ""
    result["unconvertedTotalLabel"] = format_size_gb(unconv_bytes) if unconv_bytes else ""
    return result


def _apply_scan_result(result: dict[str, Any]) -> dict[str, Any]:
    result = _finalize_totals(result)
    with _last_scan_lock:
        _last_scan.clear()
        _last_scan.update(result)
    save_library_cache(result)
    return result


def save_library_cache(result: dict[str, Any]) -> None:
    if _cache_path is None:
        return
    try:
        payload = dict(result)
        payload["savedAt"] = datetime.now(timezone.utc).isoformat()
        _cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = _cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(_cache_path)
    except Exception:
        pass


def load_library_cache() -> dict[str, Any] | None:
    if _cache_path is None or not _cache_path.is_file():
        return None
    try:
        data = json.loads(_cache_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        return None


def has_portable_metadata(game_dir: Path) -> bool:
    """True when the folder carries AnyPS5 launcher metadata (instant integrate)."""
    return (Path(game_dir) / SIDECAR_NAME).is_file()


def _entry_from_sidecar_fast(game_dir: Path, kind: str) -> dict[str, Any] | None:
    """Build a library card from sidecar/art only. No folder walk, no downloads."""
    game_dir = Path(game_dir)
    if not game_dir.is_dir():
        return None
    side = load_sidecar(game_dir)
    cover_rel = str(side.get("cover") or "")
    backdrop_rel = str(side.get("backdrop") or "")
    cover_file = game_dir / ART_DIR_NAME / COVER_NAME
    if not cover_rel and cover_file.is_file():
        cover_rel = f"{ART_DIR_NAME}/{COVER_NAME}"
    if not backdrop_rel:
        back = game_dir / ART_DIR_NAME / BACKDROP_NAME
        if back.is_file():
            backdrop_rel = f"{ART_DIR_NAME}/{BACKDROP_NAME}"
        elif cover_rel:
            backdrop_rel = cover_rel
    if not side and kind == "converted" and not _find_exe(game_dir):
        return None
    if not side and kind == "unconverted" and not (
        (game_dir / "sce_sys").is_dir() or (game_dir / "app0").is_dir()
    ):
        return None
    exe = _find_exe(game_dir) if kind == "converted" else None
    if kind == "converted" and exe is None:
        return None
    gid = stable_id(game_dir)
    cover_url, backdrop_url = _cover_urls(game_dir, gid, cover_rel, backdrop_rel)
    size_b = int(side.get("sizeBytes") or 0)
    return {
        "id": gid,
        "kind": kind,
        "title": str(side.get("title") or game_dir.name),
        "titleId": str(side.get("titleId") or "").upper(),
        "version": str(side.get("version") or ""),
        "path": str(game_dir.resolve()),
        "exePath": str(exe.resolve()) if exe else "",
        "exeName": exe.name if exe else "",
        "cover": cover_rel,
        "backdrop": backdrop_rel,
        "coverUrl": cover_url,
        "backdropUrl": backdrop_url,
        "lastPlayedAt": side.get("lastPlayedAt") or "",
        "folderName": game_dir.name,
        "sizeBytes": size_b,
        "sizeLabel": format_size_gb(size_b) if size_b else "",
    }


def _library_fingerprint(
    converted: list[dict],
    unconverted: list[dict],
    pending: list[dict] | None = None,
) -> str:
    parts: list[str] = []
    for bucket in (converted, unconverted):
        for g in bucket:
            parts.append(
                f"{g.get('id')}:{g.get('sizeBytes')}:{g.get('cover')}:{g.get('path')}"
            )
    for p in pending or []:
        parts.append(f"pending:{p.get('path')}")
    raw = "|".join(parts)
    return hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def list_game_dirs(
    converted_root: str | Path,
    unconverted_root: str | Path,
) -> tuple[list[Path], list[Path]]:
    conv_root = Path(converted_root) if str(converted_root or "").strip() else None
    unconv_root = Path(unconverted_root) if str(unconverted_root or "").strip() else None
    converted_dirs: list[Path] = []
    unconverted_dirs: list[Path] = []

    if conv_root and conv_root.is_dir():
        for child in list_child_dirs(conv_root):
            if is_converted_dir(child):
                converted_dirs.append(child)
            elif is_unconverted_dir(child):
                unconverted_dirs.append(child)

    if unconv_root and unconv_root.is_dir():
        for child in list_child_dirs(unconv_root):
            if is_converted_dir(child):
                if not any(c.resolve() == child.resolve() for c in converted_dirs):
                    converted_dirs.append(child)
            elif is_unconverted_dir(child):
                if not any(c.resolve() == child.resolve() for c in unconverted_dirs):
                    unconverted_dirs.append(child)
    return converted_dirs, unconverted_dirs


def probe_library(
    converted_root: str | Path,
    unconverted_root: str | Path,
    cached: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Instant load: keep cached cards whose folders still exist; drop missing ones."""
    cached = cached if cached is not None else load_library_cache()
    if not cached or not _roots_match(cached, str(converted_root or ""), str(unconverted_root or "")):
        return _empty_library(str(converted_root or ""), str(unconverted_root or ""))

    converted: list[dict] = []
    unconverted: list[dict] = []
    for bucket, out in (("converted", converted), ("unconverted", unconverted)):
        for entry in cached.get(bucket) or []:
            refreshed = _refresh_cached_entry(dict(entry))
            if refreshed:
                out.append(refreshed)

    result = {
        "converted": converted,
        "unconverted": unconverted,
        "scannedAt": cached.get("scannedAt") or "",
        "convertedRoot": _norm_root(converted_root),
        "unconvertedRoot": _norm_root(unconverted_root),
        "initialScanDone": bool(cached.get("initialScanDone")),
    }
    return _finalize_totals(result)


def _quick_classify_child(child: Path) -> str | None:
    """Cheap folder kind check (no deep ELF walks)."""
    if not child.is_dir():
        return None
    if _find_exe(child) and ((child / "libs").is_dir() or (child / "app0").is_dir()):
        return "converted"
    if (
        (child / SIDECAR_NAME).is_file()
        or (child / ART_DIR_NAME / COVER_NAME).is_file()
        or (child / "sce_sys").is_dir()
        or (child / "app0" / "sce_sys").is_dir()
    ):
        return "unconverted"
    return None


def quick_index_from_disk(
    converted_root: str | Path,
    unconverted_root: str | Path,
) -> dict[str, Any]:
    """Fast migration index from sidecars/art — no remote downloads, no size walks."""
    converted: list[dict] = []
    unconverted: list[dict] = []
    for root in (converted_root, unconverted_root):
        if not str(root or "").strip():
            continue
        for child in list_child_dirs(Path(root)):
            classified = _quick_classify_child(child)
            if not classified:
                continue
            e = _entry_from_sidecar_fast(child, classified)
            if not e:
                continue
            if classified == "converted":
                if not any(x.get("id") == e["id"] for x in converted):
                    converted.append(e)
            else:
                if not any(x.get("id") == e["id"] for x in unconverted):
                    unconverted.append(e)
    result = {
        "converted": converted,
        "unconverted": unconverted,
        "scannedAt": datetime.now(timezone.utc).isoformat(),
        "convertedRoot": _norm_root(converted_root),
        "unconvertedRoot": _norm_root(unconverted_root),
        "initialScanDone": True,
    }
    return _finalize_totals(result)


def needs_first_scan(converted_root: str | Path, unconverted_root: str | Path) -> bool:
    """True only when folders are set and we have never completed an initial scan."""
    if not str(converted_root or "").strip() and not str(unconverted_root or "").strip():
        return False
    data = get_last_scan()
    if data.get("initialScanDone"):
        return False
    cached = load_library_cache()
    if cached and cached.get("initialScanDone") and _roots_match(
        cached, str(converted_root or ""), str(unconverted_root or "")
    ):
        return False
    # Any child folders under configured roots → first scan needed
    for root in (converted_root, unconverted_root):
        if str(root or "").strip() and list_child_dirs(Path(root)):
            return True
    return False


def ensure_library_ready(
    converted_root: str | Path,
    unconverted_root: str | Path,
) -> dict[str, Any]:
    """Load cache (or quick-index) into memory. Always cheap, never a full art scan."""
    global _library_ready
    conv = str(converted_root or "")
    unconv = str(unconverted_root or "")

    cached = load_library_cache()
    if cached and _roots_match(cached, conv, unconv) and cached.get("initialScanDone"):
        # Full live probe (drop missing, import portable, pending list)
        live_probe_library(conv, unconv, persist=True)
        _library_ready = True
        return get_last_scan()

    # Migration / first open after upgrade: index from existing sidecars instantly
    if conv.strip() or unconv.strip():
        try:
            quick = quick_index_from_disk(conv, unconv)
        except Exception:
            quick = _empty_library(conv, unconv)
        if quick.get("converted") or quick.get("unconverted"):
            quick["initialScanDone"] = True
            quick["fingerprint"] = _library_fingerprint(
                quick.get("converted") or [],
                quick.get("unconverted") or [],
                [],
            )
            _apply_scan_result(quick)
            _library_ready = True
            start_missing_size_fill_async()
            return get_last_scan()

    empty = _empty_library(conv, unconv)
    empty["fingerprint"] = _library_fingerprint([], [], [])
    with _last_scan_lock:
        _last_scan.clear()
        _last_scan.update(empty)
    _library_ready = True
    return get_last_scan()


def live_probe_library(
    converted_root: str | Path = "",
    unconverted_root: str | Path = "",
    *,
    persist: bool = True,
) -> dict[str, Any]:
    """Cheap live sync: drop missing games, import portable metadata, list pending.

    Totals are the sum of each card's cached sizeBytes (delete = remove that size).
    """
    global _pending_new
    conv = str(converted_root or "")
    unconv = str(unconverted_root or "")
    if not conv.strip() and not unconv.strip():
        data = get_last_scan()
        return {
            "library": data,
            "changed": False,
            "pendingNew": [],
            "fingerprint": data.get("fingerprint") or "",
            "imported": 0,
            "removed": 0,
        }

    prev = get_last_scan()
    prev_pending = list(_pending_new)
    cached = load_library_cache()
    if (
        not prev.get("initialScanDone")
        or (not prev.get("converted") and not prev.get("unconverted") and cached)
    ):
        if cached and _roots_match(cached, conv, unconv):
            prev = probe_library(conv, unconv, cached)
            prev["initialScanDone"] = bool(cached.get("initialScanDone"))
        elif not prev.get("initialScanDone"):
            return {
                "library": prev,
                "changed": False,
                "pendingNew": list(_pending_new),
                "fingerprint": prev.get("fingerprint") or "",
                "imported": 0,
                "removed": 0,
            }

    prev_fp = str(prev.get("fingerprint") or "")
    kept_converted: list[dict] = []
    kept_unconverted: list[dict] = []
    kept_paths: set[str] = set()
    removed = 0

    for bucket, out in (
        ("converted", kept_converted),
        ("unconverted", kept_unconverted),
    ):
        for entry in prev.get(bucket) or []:
            refreshed = _refresh_cached_entry(dict(entry))
            if refreshed:
                out.append(refreshed)
                try:
                    kept_paths.add(str(Path(refreshed["path"]).resolve()).lower())
                except Exception:
                    kept_paths.add(str(refreshed.get("path") or "").lower())
            else:
                removed += 1

    pending: list[dict[str, Any]] = []
    imported = 0

    for root in (conv, unconv):
        if not str(root or "").strip():
            continue
        for child in list_child_dirs(Path(root)):
            try:
                key = str(child.resolve()).lower()
            except Exception:
                key = str(child).lower()
            if key in kept_paths:
                continue
            classified = _quick_classify_child(child)
            if not classified:
                continue
            if has_portable_metadata(child):
                entry = _entry_from_sidecar_fast(child, classified)
                if not entry:
                    continue
                if classified == "converted":
                    kept_converted.append(entry)
                else:
                    kept_unconverted.append(entry)
                kept_paths.add(key)
                imported += 1
            else:
                try:
                    pstr = str(child.resolve())
                except Exception:
                    pstr = str(child)
                pending.append(
                    {
                        "path": pstr,
                        "folderName": child.name,
                        "kind": classified,
                        "title": child.name,
                    }
                )

    kept_converted.sort(
        key=lambda g: str(g.get("title") or g.get("folderName") or "").lower()
    )
    kept_unconverted.sort(
        key=lambda g: str(g.get("title") or g.get("folderName") or "").lower()
    )
    fp = _library_fingerprint(kept_converted, kept_unconverted, pending)
    result = _finalize_totals(
        {
            "converted": kept_converted,
            "unconverted": kept_unconverted,
            "scannedAt": prev.get("scannedAt")
            or datetime.now(timezone.utc).isoformat(),
            "convertedRoot": _norm_root(conv),
            "unconvertedRoot": _norm_root(unconv),
            "initialScanDone": True,
            "fingerprint": fp,
        }
    )
    _pending_new = pending
    changed = fp != prev_fp

    with _last_scan_lock:
        _last_scan.clear()
        _last_scan.update(result)
    if changed and persist:
        save_library_cache(result)

    if any(
        int(g.get("sizeBytes") or 0) <= 0
        for g in kept_converted + kept_unconverted
    ):
        start_missing_size_fill_async()

    lib = get_last_scan()
    return {
        "library": lib,
        "changed": changed,
        "pendingNew": list(pending),
        "fingerprint": fp,
        "imported": imported,
        "removed": removed,
        "pendingChanged": [
            str(p.get("path") or "").lower() for p in pending
        ]
        != [str(p.get("path") or "").lower() for p in prev_pending],
    }


def start_missing_size_fill_async() -> bool:
    """Background: compute sizeBytes for library cards that still have 0."""
    global _size_fill_thread, _size_fill_running
    with _size_fill_lock:
        if _size_fill_running:
            return False
        _size_fill_running = True

    def run() -> None:
        global _size_fill_running
        try:
            data = get_last_scan()
            updated = False
            for bucket in ("converted", "unconverted"):
                games = list(data.get(bucket) or [])
                for idx, g in enumerate(games):
                    if int(g.get("sizeBytes") or 0) > 0:
                        continue
                    path = Path(str(g.get("path") or ""))
                    if not path.is_dir():
                        continue
                    try:
                        size_b = folder_size_bytes(path)
                    except Exception:
                        size_b = 0
                    if size_b <= 0:
                        continue
                    g = dict(g)
                    g["sizeBytes"] = size_b
                    g["sizeLabel"] = format_size_gb(size_b)
                    games[idx] = g
                    try:
                        save_sidecar(path, {"sizeBytes": size_b})
                    except Exception:
                        pass
                    updated = True
                    # Publish incrementally so the UI can pick up sizes
                    with _last_scan_lock:
                        cur = list(_last_scan.get(bucket) or [])
                        for i, existing in enumerate(cur):
                            if existing.get("id") == g.get("id"):
                                cur[i] = g
                                break
                        _last_scan[bucket] = cur
                        _finalize_totals(_last_scan)
                        _last_scan["fingerprint"] = _library_fingerprint(
                            list(_last_scan.get("converted") or []),
                            list(_last_scan.get("unconverted") or []),
                            list(_pending_new),
                        )
                    save_library_cache(get_last_scan())
            if updated:
                try:
                    import applog

                    applog.debug("Library size fill complete")
                except Exception:
                    pass
        finally:
            with _size_fill_lock:
                _size_fill_running = False

    _size_fill_thread = threading.Thread(
        target=run, daemon=True, name="library-size-fill"
    )
    _size_fill_thread.start()
    return True


def list_child_dirs(root: Path) -> list[Path]:
    if not root or not str(root).strip() or not Path(root).is_dir():
        return []
    try:
        return sorted(
            [p for p in Path(root).iterdir() if p.is_dir()],
            key=lambda p: p.name.lower(),
        )
    except OSError:
        return []


def folder_size_bytes(root: Path) -> int:
    total = 0
    try:
        root = Path(root)
        if not root.is_dir():
            return 0
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                try:
                    total += (Path(dirpath) / name).stat().st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


def format_size_gb(num_bytes: int) -> str:
    gb = float(num_bytes) / (1024 ** 3)
    if gb >= 10:
        return f"{gb:.0f} GB"
    if gb >= 1:
        return f"{gb:.1f} GB"
    mb = float(num_bytes) / (1024 ** 2)
    if mb >= 1:
        return f"{mb:.0f} MB"
    return f"{max(0, num_bytes)} B"


def scan_library(
    converted_root: str | Path,
    unconverted_root: str | Path,
    *,
    fetch_remote: bool = True,
    mode: str = "incremental",
) -> dict[str, Any]:
    """Scan library roots.

    mode:
      - first: full first-time scan (progress labeled accordingly)
      - incremental: reuse cached games; only process newly added folders
      - full: re-process every folder (still skips remote when cover exists)
    """
    mode = (mode or "incremental").strip().lower()
    if mode not in ("first", "incremental", "full"):
        mode = "incremental"
    try:
        import applog

        applog.info(
            "Library scan begin",
            f"mode={mode} converted={converted_root!r} unconverted={unconverted_root!r} remote={fetch_remote}",
        )
    except Exception:
        pass

    phase_label = "First time scan" if mode == "first" else (
        "Scanning for newly added games" if mode == "incremental" else "Scanning game folders"
    )
    _set_progress(
        running=True,
        phase="scanning",
        phaseLabel=phase_label,
        percent=5,
        detail="",
        rate="",
        gameIndex=0,
        gameTotal=0,
        ok=None,
        error="",
        started=datetime.now(timezone.utc).isoformat(),
        finished=None,
    )

    prev = probe_library(converted_root, unconverted_root)
    prev_by_path: dict[str, dict] = {}
    for bucket in ("converted", "unconverted"):
        for g in prev.get(bucket) or []:
            try:
                prev_by_path[str(Path(g["path"]).resolve()).lower()] = g
            except Exception:
                prev_by_path[str(g.get("path") or "").lower()] = g

    converted_dirs, unconverted_dirs = list_game_dirs(converted_root, unconverted_root)
    all_dirs = [("converted", d) for d in converted_dirs] + [
        ("unconverted", d) for d in unconverted_dirs
    ]
    total = len(all_dirs)

    # Incremental: only newly added dirs need heavy work
    work: list[tuple[str, Path]] = []
    reused: dict[str, list[dict]] = {"converted": [], "unconverted": []}
    if mode == "incremental":
        for kind, d in all_dirs:
            key = str(d.resolve()).lower()
            cached = prev_by_path.get(key)
            if cached:
                refreshed = _refresh_cached_entry(dict(cached))
                if refreshed and int(refreshed.get("sizeBytes") or 0) > 0:
                    reused[kind].append(refreshed)
                    continue
                # Missing/zero size → light reprocess (cover download still skipped if present)
            work.append((kind, d))
    else:
        work = list(all_dirs)

    _set_progress(
        phase="scanning",
        phaseLabel=phase_label,
        percent=15,
        detail=(
            f"Found {len(converted_dirs)} converted, {len(unconverted_dirs)} unconverted"
            + (f" · {len(work)} new" if mode == "incremental" else "")
        ),
        gameTotal=max(len(work), 1) if work else total,
    )

    converted = list(reused["converted"])
    unconverted = list(reused["unconverted"])
    work_total = len(work)
    for i, (kind, d) in enumerate(work, start=1):
        key = str(d.resolve()).lower()
        cached = prev_by_path.get(key)
        cached_size = int(cached.get("sizeBytes") or 0) if cached else 0
        cover_exists = (d / ART_DIR_NAME / COVER_NAME).is_file()
        label = phase_label
        if fetch_remote and not cover_exists:
            label = (
                f"First time scan — cover art {i} of {work_total}"
                if mode == "first"
                else f"Downloading cover art — new game {i} of {work_total}"
            )
        elif mode == "first":
            label = f"First time scan — {i} of {work_total}"
        else:
            label = f"Scanning newly added games — {i} of {work_total}"
        _set_progress(
            phase="artwork" if (fetch_remote and not cover_exists) else "scanning",
            phaseLabel=label,
            percent=int(15 + (80 * (i - 1) / max(1, work_total))),
            detail=d.name,
            gameIndex=i,
            gameTotal=work_total,
            rate="",
        )
        try:
            entry = _entry_from_dir(
                d,
                kind,
                fetch_remote=fetch_remote,
                game_index=i,
                game_total=work_total,
                compute_size=cached_size <= 0,
                cached_size_bytes=cached_size if cached_size > 0 else None,
            )
            if kind == "converted":
                converted.append(entry)
            else:
                unconverted.append(entry)
        except Exception as e:
            _set_progress(detail=f"Skip {d.name}: {e}")

    # Stable sort by title
    converted.sort(key=lambda g: str(g.get("title") or g.get("folderName") or "").lower())
    unconverted.sort(key=lambda g: str(g.get("title") or g.get("folderName") or "").lower())

    result = {
        "converted": converted,
        "unconverted": unconverted,
        "scannedAt": datetime.now(timezone.utc).isoformat(),
        "convertedRoot": _norm_root(converted_root),
        "unconvertedRoot": _norm_root(unconverted_root),
        "initialScanDone": True,
    }
    _apply_scan_result(result)

    _set_progress(
        running=False,
        phase="done",
        phaseLabel="Library ready" if mode != "first" else "First time scan complete",
        percent=100,
        detail=(
            f"{len(converted)} converted · {len(unconverted)} unconverted"
            + (f" · added {work_total}" if mode == "incremental" and work_total else "")
        ),
        rate="",
        ok=True,
        finished=datetime.now(timezone.utc).isoformat(),
        gameIndex=work_total or total,
        gameTotal=work_total or total,
    )
    try:
        import applog

        applog.info(
            "Library scan complete",
            f"mode={mode} {len(converted)} converted, {len(unconverted)} unconverted, new={work_total}",
        )
    except Exception:
        pass
    return result


def start_scan_async(
    converted_root: str | Path,
    unconverted_root: str | Path,
    *,
    fetch_remote: bool = True,
    mode: str = "incremental",
) -> bool:
    """Start background scan if idle. Returns False if already running."""
    global _scan_thread
    with _progress_lock:
        if _progress.get("running"):
            return False

    def run() -> None:
        try:
            scan_library(
                converted_root,
                unconverted_root,
                fetch_remote=fetch_remote,
                mode=mode,
            )
        except Exception as e:
            _set_progress(
                running=False,
                phase="error",
                phaseLabel="Library scan failed",
                detail=str(e),
                ok=False,
                error=str(e),
                finished=datetime.now(timezone.utc).isoformat(),
            )

    _scan_thread = threading.Thread(target=run, daemon=True)
    _scan_thread.start()
    return True


def find_entry_by_id(entry_id: str) -> dict | None:
    data = get_last_scan()
    for bucket in ("converted", "unconverted"):
        for g in data.get(bucket) or []:
            if g.get("id") == entry_id:
                return g
    return None


def resolve_game_file(
    entry_id: str,
    which: str,
    converted_root: str = "",
    unconverted_root: str = "",
) -> Path | None:
    entry = find_entry_by_id(entry_id)
    root: Path | None = None
    if entry:
        root = Path(entry["path"])
    else:
        # Fallback: match id under library roots (covers still work after restart)
        for base in (converted_root, unconverted_root):
            if not str(base or "").strip():
                continue
            for child in list_child_dirs(Path(base)):
                if stable_id(child) == entry_id:
                    root = child
                    break
            if root:
                break
    if root is None or not root.is_dir():
        return None
    side = load_sidecar(root)
    if which == "cover":
        rel = str(
            side.get("cover")
            or (entry or {}).get("cover")
            or f"{ART_DIR_NAME}/{COVER_NAME}"
        )
    else:
        rel = str(
            side.get("backdrop")
            or (entry or {}).get("backdrop")
            or side.get("cover")
            or f"{ART_DIR_NAME}/{BACKDROP_NAME}"
        )
    if not rel:
        # Last resort: common dump art
        for candidate in (
            root / ART_DIR_NAME / COVER_NAME,
            root / ART_DIR_NAME / BACKDROP_NAME,
            root / "sce_sys" / "icon0.png",
            root / "sce_sys" / "pic1.png",
        ):
            if candidate.is_file():
                return candidate.resolve()
        return None
    path = (root / rel).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return None
    if path.is_file():
        return path
    # Fallback paths
    for candidate in (
        root / ART_DIR_NAME / COVER_NAME,
        root / "sce_sys" / "icon0.png",
    ):
        if which == "cover" and candidate.is_file():
            return candidate.resolve()
    return None


def path_allowed(path: Path, converted_root: str, unconverted_root: str) -> bool:
    try:
        resolved = path.resolve()
    except Exception:
        return False
    for root in (converted_root, unconverted_root):
        if not root or not str(root).strip():
            continue
        try:
            resolved.relative_to(Path(root).resolve())
            return True
        except ValueError:
            continue
    return False


def prepare_convert_paths(
    unconverted_path: str | Path,
    converted_root: str | Path,
) -> dict[str, str]:
    game_root = Path(unconverted_path)
    if not game_root.is_dir():
        raise FileNotFoundError(f"Game folder not found: {game_root}")
    inspected = inspect_game_folder(game_root)
    label = inspected.get("title") or game_root.name
    folder = safe_folder_name(str(label), fallback=safe_folder_name(game_root.name))
    conv = Path(converted_root)
    if not str(converted_root or "").strip():
        raise ValueError("Set Converted Game Folder in Settings first.")
    conv.mkdir(parents=True, exist_ok=True)
    output_dir = conv / folder
    output_dir.mkdir(parents=True, exist_ok=True)
    # Seed sidecar in output early so art can land there after convert
    save_sidecar(
        output_dir,
        {
            "title": inspected.get("title") or label,
            "titleId": inspected.get("titleId") or "",
            "version": inspected.get("version") or "",
            "sourceGameRoot": str(game_root.resolve()),
        },
    )
    # Copy local dump art + download cover into the unconverted game folder
    try:
        resolve_artwork_for_game(
            game_root,
            inspected,
            fetch_remote=True,
            game_index=1,
            game_total=1,
        )
    except Exception:
        ensure_local_art_copied(game_root, inspected)
        save_sidecar(
            game_root,
            {
                "title": inspected.get("title") or label,
                "titleId": inspected.get("titleId") or "",
                "version": inspected.get("version") or "",
            },
        )

    exe_base = safe_folder_name(str(inspected.get("title") or label), fallback=folder)
    exe_base = re.sub(r"[^\w\- ]+", "", exe_base).strip() or folder
    exe_base = exe_base.replace(" ", "_")
    suggested_exe = f"{exe_base}.exe"

    return {
        "gameRoot": str(game_root.resolve()),
        "outputDir": str(output_dir.resolve()),
        "title": str(inspected.get("title") or label),
        "titleId": str(inspected.get("titleId") or ""),
        "folderName": folder,
        "suggestedOutputName": suggested_exe,
    }


def finalize_converted_game(output_dir: Path, game_root: Path | None = None) -> None:
    """After convert: write sidecar + art into the converted game folder."""
    output_dir = Path(output_dir)
    inspected = inspect_game_folder(output_dir)
    if game_root and Path(game_root).is_dir():
        src = inspect_game_folder(Path(game_root))
        if not inspected.get("titleId"):
            inspected["titleId"] = src.get("titleId") or ""
        if not inspected.get("title") or inspected["title"] == output_dir.name:
            inspected["title"] = src.get("title") or inspected.get("title")
        if not inspected.get("iconPath") and src.get("iconPath"):
            inspected["iconPath"] = src["iconPath"]
        if not inspected.get("backdropPath") and src.get("backdropPath"):
            inspected["backdropPath"] = src["backdropPath"]
    resolve_artwork_for_game(output_dir, inspected, fetch_remote=True, game_index=1, game_total=1)
    exe = _find_exe(output_dir)
    save_sidecar(
        output_dir,
        {
            "title": inspected.get("title") or output_dir.name,
            "titleId": inspected.get("titleId") or "",
            "version": inspected.get("version") or "",
            "exeName": exe.name if exe else "app.exe",
            "kind": "converted",
        },
    )


def mark_played(game_dir: Path) -> None:
    save_sidecar(
        game_dir,
        {"lastPlayedAt": datetime.now(timezone.utc).isoformat()},
    )
