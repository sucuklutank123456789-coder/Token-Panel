# Token Panel

A small system tray app for Linux and Windows that shows how many tokens Claude Code and Codex use.
It never talks to an API; it reads the session logs both tools write to your computer.

## What it shows

The panel opens layer by layer:

1. **Overview** — total tokens for the selected range (Today / 7d / 30d / All), the Claude Code vs Codex
   share, and Codex's 5-hour and weekly rate limits.
2. **Details** — per-tool breakdown by client: CLI, Desktop, VS Code extension, ACP (Zed);
   the models used and the number of threads for each.
3. **Threads** — the threads of the selected client: title, project, models, last activity.
4. **Thread details** — breakdown by model, by token type (cache / new input / output / thinking) and
   "spent on" (approximate, by tool: Bash, Read, exec_command, apply_patch…).

Hover the tray icon for today's total; hover any row for the exact numbers.

### Which tokens are counted?

Agents resend the whole conversation on every model call, and most of it is read from the cache.
Counting cache reads inflates totals 30–150×, so the default metric is **Same as the official apps**.
It can be changed with the **Metric** button on the overview:

| Metric | What is counted |
|---|---|
| Same as the official apps (default) | Claude: the same method as "Total tokens" in the Claude app — every transcript line of a response that Claude Code split across lines is counted (≈1.5–2.5× higher for Claude). Codex: the same as Codex's own counter — conversation compaction summary calls are not counted |
| Input + output | every model call counted once: non-cached input + output (thinking included), compaction calls included — actual spend |
| Input + output + cache writes | the above plus context written to the cache for the first time |
| Raw | everything, including context re-read from the cache on every call |

The choice is remembered.

## Install

### Windows

Download `TokenPanel.exe` from the [Releases](https://github.com/sucuklutank123456789-coder/token-panel/releases) page
(or, for the latest build, from the newest successful run under **Actions → Build → Artifacts**) and run it.
It is a single file; nothing is installed and Python is not needed.

- Opening it shows the panel next to the tray. Windows hides new tray icons behind the **^** arrow; drag the
  icon onto the taskbar to keep it visible.
- Right click the icon → **Start with Windows** to start it on login.
- Using Claude Code or Codex inside WSL? Right click → **Include WSL logs** (shown when WSL is installed).
  Reading those logs starts the WSL distribution, so it is off by default.
- Running it again while it is open just opens the panel.
- The .exe is not code-signed, so SmartScreen may warn on first launch: **More info → Run anyway**.

Logs are read from `%USERPROFILE%\.claude` and `%USERPROFILE%\.codex`, the folders Claude Code and Codex use on
Windows. Terminal output works too (in PowerShell): `.\TokenPanel.exe --dump | Out-Host`.

### Linux, any distribution (recommended)

```bash
git clone https://github.com/sucuklutank123456789-coder/token-panel.git
cd token-panel
./install.sh             # add --autostart to start it on login
```

The installer detects the distribution, takes PySide6 from its packages when available (otherwise from
PyPI into a private virtual environment) and installs the app for the current user into `~/.local`
(app menu entry included). It only asks for `sudo` to install distribution packages.

| Distribution | PySide6 source |
|---|---|
| Arch, Manjaro, EndeavourOS, CachyOS | `pyside6` package |
| Fedora | `python3-pyside6` package |
| openSUSE Tumbleweed | `python3-pyside6` package |
| Debian 13+, Ubuntu 25.04+ | `python3-pyside6.*` packages |
| Debian 12, Ubuntu 22.04/24.04 and derivatives (Mint, Pop!_OS, Zorin…), openSUSE Leap, RHEL/Rocky/Alma 9, others | PyPI |

If a distribution package is missing or fails, the installer falls back to PyPI. NixOS is not handled by
the script; use `nix-shell -p python3Packages.pyside6` and run `python -m tokenpanel` from the source tree.

The installer is tested end to end (install, render, uninstall) on Ubuntu 22.04, 24.04 and 25.04; the Arch package is in daily use. Other distributions are covered by the same logic but not tested yet.

Other options: `--pip` (always use PyPI), `--yes` (no confirmation prompts), `--uninstall`.
To update: `git pull && ./install.sh`.

### Arch package

```bash
cd token-panel/packaging/arch
makepkg -si
```

To start it on login with the package:

```bash
mkdir -p ~/.config/autostart
cp /usr/share/applications/tokenpanel.desktop ~/.config/autostart/
```

### Running from the source tree

```bash
python -m tokenpanel          # needs Python 3.10+ and PySide6 6.4+
```

### Desktop environment notes (Linux)

- **KDE, XFCE, Cinnamon, MATE, LXQt, Budgie**: the tray works out of the box.
- **Hyprland / Sway**: enable the `tray` module in waybar.
- **GNOME**: needs the AppIndicator extension (`gnome-shell-extension-appindicator`; built in on Ubuntu).
  Without a tray the app opens as a normal window.

## Usage

- Left click: open/close the panel. It closes when you click elsewhere or press `Esc`; `Backspace` goes back one level.
- Right click: Open panel / Refresh / Quit.
- Logs are checked every 5 seconds; only newly appended lines are read.

Terminal output (without opening the panel):

```bash
tokenpanel --dump --range 7d --metric app   # app | io | new | raw
```

## Data sources

| Tool | Log location | Client field |
|---|---|---|
| Claude Code | `~/.claude/projects/**/*.jsonl`, on Windows `%USERPROFILE%\.claude` (honours `CLAUDE_CONFIG_DIR`) | `entrypoint`: `cli`, `claude-desktop`, `claude-vscode`, `sdk-ts` (Zed ACP) |
| Codex | `~/.codex/sessions/**/*.jsonl`, `~/.codex/archived_sessions/`, on Windows `%USERPROFILE%\.codex` (honours `CODEX_HOME`) | `originator`: `codex-tui`, `Codex Desktop`, `codex_vscode`, `zed` |

Known limitations:

- Only usage on this computer is shown; cloud sessions (claude.ai/code, Codex cloud tasks) leave no local logs.
- Claude Code deletes transcripts older than 30 days by default (`cleanupPeriodDays` in `~/.claude/settings.json`).
- On the Claude side `sdk-ts` covers every tool built on the Agent SDK; it equals ACP only if Zed is the only one you use.
- Codex Desktop opens each chat in an auto-created folder, so the "project" there is that folder's name.
- "Spent on" is approximate: a model call's tokens are split evenly across the tools used in that call.

`scripts/diagnose.py` prints Claude Code token counts under each definition, which helps compare the panel
with the Claude app.

## Development

```bash
python -m unittest discover -s tests -t .
```

Windows build (on Windows, with PySide6 and PyInstaller installed):

```bash
python packaging/windows/make_ico.py packaging/windows/tokenpanel.ico
pyinstaller --noconfirm packaging/windows/tokenpanel.spec     # writes dist/TokenPanel.exe
```

The **Build** workflow runs the tests on Linux and Windows, builds `TokenPanel.exe` on every push and checks it
against sample logs with Windows paths (`scripts/sample_logs.py`). Pushing a `v*` tag attaches the .exe to a
GitHub release.
