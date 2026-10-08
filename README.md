# Token Panel

A small system tray app for Linux, Windows and macOS that shows how many tokens Claude Code, Codex and
OpenCode use, and what they would cost at API prices. It never talks to an API; it reads the session logs
these tools write to your computer.

## What it shows

The panel opens layer by layer:

1. **Overview** — total tokens for the selected range (Today / 7d / 30d / All) with its input / output split
   and estimated API cost, the Claude Code / Codex / OpenCode share, a daily chart of the last 30 days, and
   Codex's 5-hour and weekly usage limits. Hover a column for its numbers; click the chart for the same days as a
   table. Days outside the selected range are drawn faded.
2. **Details** — per-tool breakdown by client: CLI, Desktop, VS Code extension, ACP (Zed);
   the models used, the number of threads and the estimated cost for each.
3. **Threads** — the threads of the selected client: title, project, models, last activity.
4. **Thread details** — breakdown by model, by token type (cache / new input / output / thinking) and
   "spent on" (approximate, by tool: Bash, Read, exec_command, apply_patch…).

Hover the tray icon for today's total; hover any row for the exact numbers.

### Which tokens are counted?

Agents resend the whole conversation on every model call, and most of it is read from the cache, so counting
cache reads inflates totals 30–150×. Claude Code's `/stats` and the Codex app now count it anyway, which is why
they show billions. The default metric, **Same as the official apps**, follows them; the other metrics leave
the cache out. It can be changed with the **Metric** button on the overview:

| Metric | What is counted |
|---|---|
| Same as the official apps (default) | Claude: the same method as "Total tokens" in Claude Code's `/stats` — input, output, cache reads and cache writes, and every transcript line of a response that Claude Code split across lines is counted. Codex: the same as Codex's own counter, cached input included — conversation compaction summary calls are not counted. OpenCode: input, output and cache |
| Official apps, cache excluded | the same, without cached input: how the official apps counted before they included the cache |
| Input + output | every model call counted once: non-cached input + output (thinking included), compaction calls included — actual spend |
| Input + output + cache writes | the above plus context written to the cache for the first time |
| Raw | everything, including context re-read from the cache on every call |

The choice is remembered. Input and output are shown under the same metric.

### API cost estimate

Each model call is priced at the provider's pay-as-you-go API rates for its model: new input, cache reads,
cache writes (Claude's 5-minute and 1-hour writes separately) and output (thinking included), plus Claude fast
mode and OpenAI's long-context tier. It always counts every call once, whatever the metric. It is an estimate of
what the same work would cost on the API; Claude Pro/Max and ChatGPT plans are billed differently.

- Claude prices come from Anthropic's pricing page; OpenAI prices from OpenAI's pricing and model pages
  (checked 2026-10-05; the OpenAI figures could only be read through search results, so double-check them).
- For OpenCode, a model the panel doesn't know is priced with the cost OpenCode itself logged.
- Models without a known price are left out of the cost and reported as "tokens without a known price".

Prices can be added or corrected in `prices.json`, in USD per million tokens:

```json
{
  "gpt-6": {"input": 2.0, "cache_read": 0.2, "output": 10.0},
  "my-local-model": {"input": 0, "output": 0}
}
```

`cache_read` and `cache_write` default to the input price; `cache_write_1h` is optional. The file lives at
`~/.config/tokenpanel/prices.json` on Linux, `%APPDATA%\tokenpanel\prices.json` on Windows and
`~/Library/Application Support/tokenpanel/prices.json` on macOS (or set `TOKENPANEL_PRICES`). Restart the panel
after editing it.

## Install

### Windows

Download `TokenPanel.exe` from the [Releases](https://github.com/sucuklutank123456789-coder/Token-Panel/releases) page
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

### macOS

> Built and tested automatically on GitHub's macOS machines, but not yet tried on a real Mac. Reports welcome.

Download `TokenPanel-macos-arm64.zip` (Apple Silicon: M1 and later) or `TokenPanel-macos-x86_64.zip` (Intel)
from the [Releases](https://github.com/sucuklutank123456789-coder/Token-Panel/releases) page (or from the newest
successful run under **Actions → Build → Artifacts**). Unzip it and move **TokenPanel.app** to Applications.

The app is not signed with an Apple Developer ID, so macOS blocks the first launch:

1. Open TokenPanel.app; macOS says it can't be opened. Click **Done**.
2. **System Settings → Privacy & Security**, scroll down, and click **Open Anyway** next to TokenPanel.
3. Open it again and confirm with **Open**. Later launches work normally.

Or, in Terminal: `xattr -dr com.apple.quarantine /Applications/TokenPanel.app`.

- It lives in the menu bar (no Dock icon). Opening the app shows the panel, which helps on MacBooks where the
  notch can hide menu bar icons.
- Click the icon to open the panel; right click or control-click for Refresh, **Start at login** and Quit.
- Logs are read from `~/.claude` and `~/.codex`, the same folders as on Linux.
- Terminal output: `/Applications/TokenPanel.app/Contents/MacOS/TokenPanel --dump`.

### Linux, any distribution (recommended)

```bash
git clone https://github.com/sucuklutank123456789-coder/Token-Panel.git
cd Token-Panel
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
cd Token-Panel/packaging/arch
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
- Right click (on macOS also control-click): Open panel / Refresh / Quit, plus start on login on Windows and macOS.
- Logs are checked every 5 seconds and only newly appended lines are read. New log files are picked up within a minute; **Refresh** looks for them right away.

Terminal output (without opening the panel):

```bash
tokenpanel --dump --range 7d --metric app   # app | app_nc | io | new | raw
```

## Data sources

| Tool | Log location | Client field |
|---|---|---|
| Claude Code | `~/.claude/projects/**/*.jsonl`, on Windows `%USERPROFILE%\.claude` (honours `CLAUDE_CONFIG_DIR`) | `entrypoint`: `cli`, `claude-desktop`, `claude-vscode`, `sdk-ts` (Zed ACP) |
| Codex | `~/.codex/sessions/**/*.jsonl`, `~/.codex/archived_sessions/`, on Windows `%USERPROFILE%\.codex` (honours `CODEX_HOME`) | `originator`: `codex-tui`, `Codex Desktop`, `codex_vscode`, `zed` |
| OpenCode (1.2+) | `~/.local/share/opencode/opencode.db` on every OS, on Windows `%USERPROFILE%\.local\share\opencode` (honours `XDG_DATA_HOME`, `OPENCODE_DB`); read-only | not recorded — shown as "All clients" |

Known limitations:

- Only usage on this computer is shown; cloud sessions (claude.ai/code, Codex cloud tasks) leave no local logs.
- Claude's usage limits are not shown: they are shared with the Claude apps and claude.ai chat, whose usage
  leaves no logs on this computer, so any percentage computed here would be wrong. `/usage` in Claude Code
  shows them.
- On the Claude side `sdk-ts` covers every tool built on the Agent SDK; it equals ACP only if Zed is the only one you use.
- Codex Desktop opens each chat in an auto-created folder, so the "project" there is that folder's name.
- "Spent on" is approximate: a model call's tokens are split evenly across the tools used in that call.
- OpenCode doesn't record whether a session ran in the terminal, the desktop app or over ACP, so it has one
  client row. Its subagent sessions are counted in the conversation that started them. Versions before 1.2
  stored sessions as JSON files, which are not read (upgrading imports them into the database).

`scripts/diagnose.py` prints Claude Code token counts under each definition, which helps compare the panel
with the Claude app.

## Development

```bash
python -m unittest discover -s tests -t .
ruff check .        # lint; the Build workflow runs it too
```

Windows build (on Windows, with PySide6 and PyInstaller installed):

```bash
python packaging/make_icons.py ico packaging/windows/tokenpanel.ico
pyinstaller --noconfirm packaging/windows/tokenpanel.spec     # writes dist/TokenPanel.exe
```

macOS build (on a Mac, with PySide6 and PyInstaller installed):

```bash
python packaging/make_icons.py iconset tokenpanel.iconset
iconutil -c icns tokenpanel.iconset -o packaging/macos/tokenpanel.icns
pyinstaller --noconfirm packaging/macos/tokenpanel.spec       # writes dist/TokenPanel.app
codesign --force --deep --sign - dist/TokenPanel.app            # ad-hoc signature
```

The **Build** workflow runs the tests on Linux, Windows and macOS on every push. It builds `TokenPanel.exe` and
`TokenPanel.app` (Apple Silicon and Intel) and checks them against sample logs (`scripts/sample_logs.py`).
Pushing a `v*` tag attaches the builds to a GitHub release.
