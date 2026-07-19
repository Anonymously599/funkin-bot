# FNFBot — Python Edition

A desktop autoplay bot for Friday Night Funkin' charts, with a proper GUI, live note preview, and a chart parser that doesn't fall over the moment you feed it something other than vanilla FNF.

> Forked from a simpler original — this version rewrites the hook engine, the chart parser, and the settings system pretty much from the ground up. Feel free to fork it further.

---

## ✨ Features

- **Multi-format chart support** — vanilla FNF `.json`, Psych Engine, P-Slice, `.fnfc` archives, `.zip` chart packs, and the newer FunkinCrew multi-difficulty format. One parser, no per-engine guesswork.
- **Correct lane ownership, every time** — handles both Psych Engine's raw (unconverted) `mustHitSection` swap rule *and* the converted absolute rule, so opponent notes don't get mis-pressed regardless of which format your chart came from.
- **Global hotkeys** — start/stop and nudge timing offset from anywhere, even with FNF focused and the bot minimized. Runs on a real low-level Windows hook, with an automatic pynput fallback if the hook can't attach.
- **Live note preview** — a scrolling lane view shows upcoming notes, holds, and hit flashes in real time as the bot plays.
- **Smart note filtering** — harmful note types (mines, void notes, hurt notes, etc.) are skipped by default, opponent-tagged notes are always skipped, and cosmetic types are clicked normally. Every note type gets its own override checkbox once a chart is loaded.
- **Fast mode** — tightens reaction timing for spam-heavy charts.
- **Configurable everything** — per-lane keybinds, hotkey bindings, timing offset, all saved to a local settings file and reloaded on launch.

---

## 🚀 Getting Started

### Run from source
```bash
pip install pynput pyautogui
python fnfbot.py
```
Only `pynput` is required for keypresses; `pyautogui` is an optional fallback if `pynput` isn't available. On Windows, if neither is installed, the bot falls back to raw `ctypes` calls automatically — no extra install needed for that path.

### Build a standalone executable
A PyInstaller spec is already set up in the repo:
```bash
pip install pyinstaller
pyinstaller FNFBot.spec
```
Output lands in `dist/fnfbot.exe` — no console window, single file.

---

## 🎮 Controls

| Key | Action |
|-----|--------|
| `F1` | Start / Stop the bot |
| `F2` | Increase timing offset (+5ms) |
| `F3` | Decrease timing offset (-5ms) |

All three are rebindable from the Settings tab.

---

## 📁 Project Structure

| File | What it does |
|------|---------------|
| `fnfbot.py` | The GUI — chart loading, settings, live preview, console log |
| `fnf_song.py` | Chart parser — reads and normalizes every supported chart format into one note representation |
| `bot_engine.py` | The engine — keypress backend (pynput / pyautogui / ctypes) and the global hotkey listener |

---

## ⚙️ How it decides which notes are yours

Lane ownership isn't always as simple as "lane 0–3 = player." Depending on the chart format, it's either:

- **Absolute** — lane 0–3 is always the player, 4–7 is always the opponent (converted Psych charts, P-Slice, FunkinCrew v2 format), or
- **Swap-based** — ownership flips per-section based on `mustHitSection` (raw, unconverted Psych Engine charts straight from the editor)

`fnf_song.py` detects which rule applies per file and parses accordingly, so you don't have to pick an engine manually.

---

## 🩹 Note on scope

This is a hobby/testing tool for chart accuracy and timing experimentation — not intended for use on anything with real stakes (leaderboards, competitive play, etc.). Use responsibly.

---

## 🤝 Contributing

Forks and PRs welcome. If you hit a chart format this doesn't parse correctly, open an issue with a sample file (strip any personal info first) and it'll get looked at.

---

## 📜 License

MIT — see [`LICENSE`](LICENSE). Do what you want with it, just keep the copyright notice.

## ⚠️ Disclaimer

This tool is provided **as-is**, for personal and educational use, with no warranty of any kind. The authors and contributors are not responsible for how it's used, and are not liable for any damages, bans, disputes, or other consequences arising from its use. If you use this with any game, mod, platform, or service, it's on you to check and follow that platform's own rules and terms — the license covers the code, not your usage of it.
