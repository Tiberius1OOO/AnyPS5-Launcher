# AnyPS5 Launcher

Windows desktop launcher for **[AnyPS5](https://github.com/boykopovar/AnyPS5)** — a toolchain that converts dumped PS5 game folders into a Windows `.exe` you can run locally.

This repo is the **launcher only**. It does not fork or patch upstream AnyPS5. It installs the tools, clones the project next to the app, builds it, converts games, and starts them from a local browser UI.

**Current release:** [v1.2.4](https://github.com/Tiberius1OOO/AnyPS5-Launcher/releases/latest)

---

## What you get

| Capability | What it does |
|------------|--------------|
| **One-click setup** | Detects and installs Git, Python, CMake, Ninja, WinLibs GCC; clones AnyPS5 + submodules |
| **Build** | Full CMake / Ninja rebuild of the converter and system libs |
| **Convert** | Turns a dumped game folder into a Windows build (with GPU filter, Intel CPU mode, cleanup levels) |
| **Start game** | Launches the converted `.exe` with the right environment |
| **Controls** | Keyboard / mouse remapping (controllers work automatically) |
| **Updates** | Pull latest AnyPS5 from GitHub; optional self-update of the launcher |
| **Console** | Live install / build / convert log + saved `logs\last-job.log` |

---

## Download

1. Get **AnyPS5-Launcher-windows.zip** from [Releases](https://github.com/Tiberius1OOO/AnyPS5-Launcher/releases/latest).
2. Unzip anywhere (spaces and deep paths are fine).
3. Run **`AnyPS5Launcher.exe`** (or `Start-AnyPS5.cmd`).

A console window opens and the UI loads at `http://127.0.0.1:8765/`. **Leave that console open** while you use the launcher.

### Launcher self-update

On each start the app checks GitHub Releases once. If a newer launcher exists:

- **Update Now** — downloads the release zip, shows progress in the console, opens a visible updater window that replaces files and relaunches. Your `AnyPS5` folder, configs, and logs are kept.
- **Keep using Old version** — dismiss and continue.

> If you are still on **v1.2.2 or v1.2.3**, download the latest zip once by hand (Update Now on those builds could hang). From **v1.2.4** onward, Update Now works normally.

---

## Recommended workflow

1. **Setup** → **Install / repair everything**  
   Clones AnyPS5 into `<launcher-folder>\AnyPS5` (fixed path — no folder picker).  
   Missing tools may prompt for admin (Program Files / `C:\winlibs`). The launcher itself runs without admin.
2. **Build** → **Build now** (full rebuild after first install or after pulling updates).
3. **Convert** → pick game folder → **Convert** → **Start game**.
4. Later: **Updates** → check / download AnyPS5 changes → **Build** again if needed.

Status chips in the header show branch, commit, pending updates, and whether the build matches the current source.

---

## Tabs

| Tab | Features |
|-----|----------|
| **Setup** | Progress bar + transfer UI; install steps (Git, Python, CMake, Ninja, WinLibs, clone, submodules); PC checklist; **Re-check**; **Run as admin** when installs need elevation |
| **Controls** | Click-to-bind keys; reset defaults; save; export / import JSON or INI |
| **Convert** | Game folder + output folder; output name; GPU name filter; cleanup level (Normal / Stronger / Strict); advanced: Intel CPU compatibility, hide console window; **Convert** + **Start game** |
| **Updates** | Check AnyPS5 for new commits; download (pull); open upstream GitHub; pending + recent history lists |
| **Build** | Status for converter, system libs, compiler, build tools; **Build now**; **Mark as up to date** |
| **README** | Live upstream AnyPS5 readme from GitHub (reload / open on GitHub) |
| **Console** | Streaming log for install, update, build, convert; **Open log file** → `logs\last-job.log` |

Hero actions: **Start game** and **Check updates** are always available from the top bar.

---

## Convert details

### What the game folder needs

- An entry binary such as `eboot.bin` or another `.elf`
- Modules (typically `sce_module` / `sce_modules`)
- Title content under the usual dump layout

### What Convert does automatically

- Runs the AnyPS5 relinker with your options
- Passes extra **`--module-dir`** paths for engine layouts such as Unity **`Media/Modules`** and **`Media/Plugins`** (not only `sce_module`)
- After convert, copies system `.prx` libs and the three MinGW runtime DLLs  
  (`libgcc_s_seh-1.dll`, `libstdc++-6.dll`, `libwinpthread-1.dll`) into `libs/`, next to the `.exe`, and into `app0` module folders — Windows does not search `PATH` for these; missing DLLs show up as **GetLastError 126**
- Copies title resources into `app0/`
- **Start game** re-copies MinGW DLLs if an older convert is missing them

### Options (also in UI **?** tips)

| Option | Use when |
|--------|----------|
| **GPU name filter** | Multiple GPUs — type part of the name (e.g. `5080`) so the game picks that adapter |
| **Cleanup level** | Normal = safest; Stronger / Strict strip more unused imports (can break some titles) |
| **Intel CPU compatibility** | Intel CPUs (PS5 code may use AMD-only instructions). Leave off on AMD |
| **Hide console window** | No black console behind the game (harder to see crash text) |

---

## Paths with spaces or deep folders

Folder names with spaces (e.g. `C:\Games\My Title\...`) are supported. Deep installs under `Downloads\...` are remapped during Build to a short path or junction under ProgramData so MinGW `windres` and glslang SPIR-V steps stay reliable. Your install folder does not need to move.

---

## Tools Setup installs

| Step | Typical location / notes |
|------|--------------------------|
| Git | Needed to clone AnyPS5 |
| Python 3 | Real `python.exe` for CMake / glslang (not only the Store stub) |
| CMake ≥ 3.22 | Build system |
| Ninja | Fast build backend |
| WinLibs GCC | Default extract under `C:\winlibs` |
| Clone AnyPS5 | `<launcher>\AnyPS5` from [boykopovar/AnyPS5](https://github.com/boykopovar/AnyPS5) |
| Submodules | `git submodule update --init --recursive` |

On Setup: **ok** = present, **next** = still required. Use **Console** if something fails — useful compiler/CMake lines sit above `ERROR: exit code …`.

---

## Troubleshooting

| Symptom | What to try |
|---------|-------------|
| Setup / Build fails | **Console** tab + `logs\last-job.log` next to the exe |
| Convert / start: error **126** | Re-run Convert (or Start game) so MinGW DLLs land next to the exe and under `app0` module dirs |
| Unity / Il2Cpp titles fail to load modules | Ensure Convert sees `Media/Modules` / `Media/Plugins` (v1.2.2+ passes `--module-dir` automatically) |
| Launcher Update Now stuck (old build) | Manually install [latest zip](https://github.com/Tiberius1OOO/AnyPS5-Launcher/releases/latest) once |
| Build cannot find Python | Setup → Install / repair (installs a real `python.exe`; Build also passes `-DPython3_EXECUTABLE=…`) |

---

## Build from source (developers)

Python 3.11+ on Windows:

```bat
pip install -r requirements.txt
python app\app.py
```

Or `scripts\Start-AnyPS5.cmd`. UI: `http://127.0.0.1:8765/`

Release zip:

```bat
powershell -ExecutionPolicy Bypass -File scripts\build-release.ps1
```

Output: `dist\release\AnyPS5-Launcher\` and `AnyPS5-Launcher-windows.zip`.

---

## License

MIT — see [LICENSE](LICENSE).

AnyPS5 itself remains under its own terms: [boykopovar/AnyPS5](https://github.com/boykopovar/AnyPS5).
