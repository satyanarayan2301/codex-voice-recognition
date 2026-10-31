# voicecoder — hands-free voice for Codex CLI and Claude Code

Stop babysitting your coding agent. `voicecoder` lets you **talk** to Codex or Claude Code,
**speaks the result out loud** when the agent finishes, keeps your **laptop awake**, and then
**listens for your next command**.

## Features
- Voice loop: speak a task → read-back confirmation → agent works → result is spoken → listens again
- Works with **Codex CLI** (`codex exec --json`) and **Claude Code** (`claude -p --output-format stream-json`), keeping the same session between commands
- Keeps the computer awake while running (Windows / macOS / Linux)
- Built-in OS voices for speech — no TTS install needed
- Optional hooks so your normal interactive Codex / Claude sessions announce when they finish
- **Voice approvals**: when Codex or Claude wants to run a command or change files, it reads the request
  out loud and waits for you — say **yes**, **no**, **always** (don't ask again this session) or **details**
- Voice commands: `repeat`, `new session`, `keyboard`, `stop`

## Requirements
- Python 3.10+ (tested on 3.14, Windows)
- `pip install -r requirements.txt`
- Codex CLI and/or Claude Code installed and signed in
- Internet (speech recognition uses Google's free web speech API, default language `en-IN`)

## Quick start (Windows)
Double-click the launchers:

| File | What it does |
|---|---|
| `0 - Install Claude Code.bat` | Installs Claude Code with the official installer |
| `1 - Test speaker and mic.bat` | Installs Python deps, tests speaker + microphone |
| `2 - Start voice Codex.bat` | Voice loop with Codex in a folder you drag in |
| `3 - Start voice Claude.bat` | Voice loop with Claude Code in a folder you drag in |
| `4 - Log in to Claude.bat` | First-time Claude Code sign-in |

## Command line
```bash
python voicecoder.py test                                   # speaker + mic check
python voicecoder.py loop --agent codex  --cwd path/to/project
python voicecoder.py loop --agent claude --cwd path/to/project
python voicecoder.py loop --agent claude --lang hi-IN       # Hindi
python voicecoder.py loop --no-confirm                      # skip read-back
python voicecoder.py loop --type                            # keyboard only
python voicecoder.py loop --agent codex --codex-approval untrusted   # ask before nearly every command
python voicecoder.py loop --no-approvals                    # never ask
python voicecoder.py awake                                  # just block sleep
python voicecoder.py hooks                                  # print notify/Stop hook config
```

Without approvals (`--no-approvals`) the default flags are Codex `--sandbox workspace-write` and Claude `--permission-mode acceptEdits`.
Override with `--agent-args "..."`. Codex requires a git repo unless you add `--skip-git-repo-check`.

## Voice approvals
On by default in the voice loop.

- **Claude Code**: runs with `--permission-mode default` and `--permission-prompt-tool`, backed by a tiny
  built-in MCP helper (`voicecoder.py mcp-approver`). Every permission Claude would normally ask for on screen
  is spoken to you instead.
- **Codex**: runs through `codex app-server`, which sends real approval requests for commands and file
  changes. `--codex-approval untrusted` asks for almost every command; the default `on-request` asks when
  Codex needs more access than the sandbox allows.
- Unclear answers count as **no**. `always` is refused for risky commands (`rm`, `del`, `git push`, `curl`…),
  which are always asked individually.
- `--no-approvals` restores the old behaviour (Claude auto-accepts edits, Codex uses `codex exec`).
- In the normal interactive UI (hooks mode) approvals are only **announced**, not answered.

## Notes
- Closing the laptop lid still sleeps the machine — change that in your OS power settings.
- Env vars: `VOICECODER_MAX_CHARS` (spoken length, default 700), `VOICECODER_RATE` (voice speed).
