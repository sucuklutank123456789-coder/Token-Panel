# Token Panel

A small Linux system tray app that shows how many tokens Claude Code and Codex use.
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

## Install (Arch-based distributions)

```bash
git clone https://github.com/sucuklutank123456789-coder/Token.git
cd Token/packaging/arch
makepkg -si
```

Then open **Token Panel** from your app menu, or run `tokenpanel` in a terminal.

To start it on login:

```bash
mkdir -p ~/.config/autostart
cp /usr/share/applications/tokenpanel.desktop ~/.config/autostart/
```

To update: `git pull`, then `makepkg -sif` in `packaging/arch` and restart the app.

### Running without packaging

```bash
sudo pacman -S pyside6
python -m tokenpanel          # from the repository root
```

### Desktop environment notes

- **KDE, XFCE, Cinnamon, LXQt**: the tray works out of the box.
- **Hyprland / Sway**: enable the `tray` module in waybar.
- **GNOME**: needs `gnome-shell-extension-appindicator` for the tray. Without a tray the app opens as a normal window.

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
| Claude Code | `~/.claude/projects/**/*.jsonl` (honours `CLAUDE_CONFIG_DIR`) | `entrypoint`: `cli`, `claude-desktop`, `claude-vscode`, `sdk-ts` (Zed ACP) |
| Codex | `~/.codex/sessions/**/*.jsonl`, `~/.codex/archived_sessions/` (honours `CODEX_HOME`) | `originator`: `codex-tui`, `Codex Desktop`, `codex_vscode`, `zed` |

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
