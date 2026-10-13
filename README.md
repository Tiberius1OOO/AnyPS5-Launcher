# AnyPS5 Launcher

Windows launcher for [AnyPS5](https://github.com/boykopovar/AnyPS5).

Installs the tools you need, clones AnyPS5 next to the app, builds it, converts a game folder to a Windows `.exe`, and starts it from a browser UI.

This repository is the launcher only. It does not modify upstream AnyPS5.

## Download

1. Get **AnyPS5-Launcher-windows.zip** from [Releases](https://github.com/Tiberius1OOO/AnyPS5-Launcher/releases).
2. Unzip anywhere.
3. Run **`AnyPS5Launcher.exe`** (or `Start-AnyPS5.cmd`).

## First-time setup

1. Open **Setup** → **Install / repair everything**.
2. AnyPS5 is cloned into `<launcher-folder>\AnyPS5` (next to the exe). No path picker.
3. If Git / Python / CMake / WinLibs are missing, you may be asked for admin so they can install under Program Files or `C:\winlibs`. The launcher itself runs without admin.
4. When Setup shows ready, open **Build** → **Full Rebuild**.
5. Open **Convert**, select your game folder, convert, then **Start game**.

Leave the launcher window open while you use the UI.

If Build or Setup fails, open the **Console** tab and scroll up. The useful compiler/CMake lines are above `ERROR: exit code …`. A full copy is also written to `logs\last-job.log` next to the exe (use **Open log file** on the Console tab).

## Tabs

| Tab | Purpose |
|-----|---------|
| **Setup** | Detect / install tools, clone AnyPS5, init submodules |
| **Controls** | Keyboard and mouse bindings (controllers work automatically) |
| **Convert** | Convert a dumped game folder to a Windows `.exe` |
| **Updates** | Fetch and pull the latest AnyPS5 |
| **Build** | Full CMake / Ninja rebuild |
| **README** | Upstream AnyPS5 readme |
| **Console** | Live log for install, build, and convert |

On Setup: **ok** = already present, **next** = still required.

## Convert notes

- Game folder needs `eboot.bin` or another `.elf`, plus modules such as `sce_module`.
- Use the **?** tips on Convert options for GPU filter, Intel CPU, and cleanup level.

## Paths with spaces

Folder names with spaces (example: `C:\Games\HYDERA LUNCHER\...`) are supported.  
During Build the launcher automatically uses a space-free Windows short path or a junction under `ProgramData`, because MinGW `windres` cannot parse spaced include paths. Your install folder does not need to move.

## Python and Build

CMake needs a real `python.exe` on PATH (glslang). Setup installs Python when missing.  
Build also passes `-DPython3_EXECUTABLE=...` so CMake finds it reliably after install.

## Build from source

Python 3.11+ on Windows:

```bat
pip install -r requirements.txt
python app\app.py
```

Or `scripts\Start-AnyPS5.cmd`. UI: `http://127.0.0.1:8765/`

Release zip:

```bat
scripts\build-release.ps1
```

Output: `dist\release\AnyPS5-Launcher\` and `AnyPS5-Launcher-windows.zip`.

## License

MIT. See [LICENSE](LICENSE).  
AnyPS5 remains under its own license: [boykopovar/AnyPS5](https://github.com/boykopovar/AnyPS5).
