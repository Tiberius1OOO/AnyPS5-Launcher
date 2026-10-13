# AnyPS5 Launcher

Standalone Windows launcher for [AnyPS5](https://github.com/boykopovar/AnyPS5).  
It installs build tools, clones the upstream AnyPS5 project, builds the relinker, converts a PS5 game folder to a Windows `.exe`, and starts it — from a simple browser UI.

This repository is **only the launcher**. It does not patch or fork AnyPS5 core.

---

## Quick start (friends / release zip)

1. Download **AnyPS5-Launcher-windows.zip** from [Releases](https://github.com/Tiberius1OOO/AnyPS5-Launcher/releases) (or use a local build under `dist/release/`).
2. Unzip anywhere (e.g. `Desktop\AnyPS5-Launcher`).
3. Double-click **`Install-AnyPS5.cmd`**.
4. Read the short admin note, then continue. The launcher UI opens in your browser.
5. In the **Setup** tab, click **Install / repair everything**.
   - A dialog explains why admin may be needed (Git / Python / CMake into Program Files, WinLibs under `C:\winlibs`).
   - Choose **Continue with admin**, **Try without admin**, or **Cancel**.
6. Wait until the Setup checklist is green (progress bar + steps update live; full log is in **Console**).
7. Open **Build** → **Full Rebuild**.
8. Open **Convert game**, pick your game folder, convert, then **Start game**.

Next time, use **`Start-AnyPS5.cmd`** (no need to reinstall).

---

## What the tabs do

| Tab | Purpose |
|-----|---------|
| **Setup** | Check and install Git, Python, CMake, Ninja, WinLibs; clone AnyPS5; init submodules |
| **Controls** | Keyboard / mouse bindings (controllers work automatically) |
| **Convert** | Point at a dumped game folder, convert to Windows `.exe` |
| **Update** | Pull latest AnyPS5 from GitHub |
| **Build** | CMake / Ninja full rebuild of relinker + libs |
| **Readme** | Upstream AnyPS5 README |
| **Console** | Live install / build / convert log |

Default AnyPS5 install folder: `%USERPROFILE%\AnyPS5`  
(configurable in launcher config as `anyps5Root`).

---

## Admin / UAC — when and why

The launcher itself does **not** need admin to run.

Elevation is only useful when installing system tools:

- **Git / Python / CMake** → typically `Program Files`
- **WinLibs GCC** → `C:\winlibs`
- **winget** installs often require UAC

You always see an explanation dialog before UAC. You can try without admin; some steps may fail if writes to those paths are denied.

---

## Run from source (developers)

Requirements: Python 3.11+ on Windows.

```bat
cd F:\PS5-Games\AnyPS5-Launcher
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app\app.py
```

Or:

```bat
scripts\Start-AnyPS5.cmd
```

Opens `http://127.0.0.1:8765/` (Waitress).  
`?boot=1` shows the logo intro once; F5 uses a clean URL and does not replay it.

---

## Build a Windows release zip

```bat
scripts\build-release.ps1
```

Produces:

- `dist\release\AnyPS5-Launcher\` — folder with `AnyPS5Launcher.exe`, start/install scripts, `LIESMICH.txt`
- `dist\release\AnyPS5-Launcher-windows.zip` — ready to send

Friends do **not** need Python to run the `.exe` UI. Build tools for AnyPS5 itself are still installed via Setup.

---

## Project layout

```
AnyPS5-Launcher/
  app/                 # Flask UI + setup engine
  scripts/             # Install / Start / build-release
  requirements.txt     # runtime + PyInstaller (dev)
  dist/                # build output (gitignored)
```

---

## Tips

- After Setup succeeds, always **Full Rebuild** before the first Convert.
- Convert needs a game folder with `eboot.bin` / `*.elf` and `sce_module` (or similar) next to it.
- Use the **?** help buttons on Convert options for Intel CPU, GPU filter, cleanup level, etc. Tips float above the UI so they do not clip or overlap fields.
- Leave the launcher window / console open while using the UI.

---

## License

MIT — see [LICENSE](LICENSE).  
AnyPS5 itself remains under its own upstream license at [boykopovar/AnyPS5](https://github.com/boykopovar/AnyPS5).
