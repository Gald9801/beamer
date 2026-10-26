# Installing this fork

Paste the matching prompt into Claude Code on that machine.

## Windows PC

```
Install my fork of Beamer (a keyboard/mouse sharing app) on this PC, replacing the stock version.
My fork adds copy-paste file transfer between paired machines.

Repo: https://github.com/Gald9801/beamer (branch main, commit 26a1c1a or newer)

1. Clone it to Documents\GitHub\_forks\beamer. If it already exists, git pull instead.
2. In win_app: create a venv with Python 3.11 (`py -3.11 -m venv .venv`) and
   `.venv\Scripts\python.exe -m pip install -r requirements-win.txt`.
3. Do NOT run build_win_app.ps1 or Install-Beamer.ps1: they run the full test suite, which takes
   over 30 minutes and has failures that have nothing to do with the fork. Run only the fork's
   tests, from the repo root:
   `win_app\.venv\Scripts\python.exe -m unittest core.tests.test_file_transfer core.tests.test_protocol_v6`
4. Build, from win_app:
   `.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm --distpath dist --workpath build build.spec`
5. Beamer lives at %LOCALAPPDATA%\Beamer\Beamer.exe. Stop only the Beamer.exe processes (by name
   or PID, never all python.exe), wait until they have exited, copy the existing exe to
   Beamer-1.5.13-original.exe in the same folder, then copy dist\Beamer.exe over it. If Beamer is
   not installed there, tell me and stop.
6. Start it, then confirm %LOCALAPPDATA%\Beamer\Beamer.log shows "Listening for version 6 links"
   and a link to my other PC. Leave settings.json untouched.
7. Do not commit or push anything. Report what you did and anything that failed.
```

## Mac

```
Install my fork of Beamer (a keyboard/mouse sharing app) on this Mac, replacing the stock version.
My fork adds copy-paste file transfer between paired machines. The Mac code in the fork has never
been built or run, so watch for errors.

Repo: https://github.com/Gald9801/beamer (branch main, commit 26a1c1a or newer)

1. Clone it to ~/Documents/GitHub/_forks/beamer. If it already exists, git pull instead.
2. Quit Beamer if it is running (menu bar icon, Quit), and copy /Applications/Beamer.app to
   ~/Beamer-1.5.13-original.app as a backup.
3. Run mac_app/Install-Beamer-Mac.command from Terminal. It builds with build_dmg.sh and installs
   /Applications/Beamer.app. Do not run the full test suite; if you want a check, run only:
   `cd mac_app && .venv/bin/python3 -m unittest core.tests.test_file_transfer` (with the repo root
   on the path).
4. The fork's Mac-only changes are in mac_app/clipboard_mac.py (read_files, set_files) and
   mac_app/bridge.py (the files hand-off). If the build or app start fails there, fix the bug,
   show me the diff, and do not push without asking.
5. Open Beamer. macOS may ask for Accessibility/Input Monitoring again because the build is new;
   tell me which prompts appear rather than clicking through them.
6. Confirm ~/Library/Logs/Beamer/Beamer.log shows the link to my PC coming up.
7. Report what you did and anything that failed.
```
