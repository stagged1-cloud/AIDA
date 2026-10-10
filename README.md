# AIDA: a voice assistant that actually does things on your PC

Press a key, say what you want, press it again. [Claude Code](https://claude.com/claude-code) does the job on your
Windows machine (opens apps, edits files, builds projects, searches the web) and a local neural voice tells you
how it went, in the voice of a blunt, dry, slightly smug robot. It remembers your day, learns how you talk, and
asks for a spoken "confirmed" before anything destructive.

> **DISCLAIMER.** This is a personal hobby project, provided **as is, with no warranty** (see `LICENSE`). It lets an AI
> run commands on your PC, with your permissions, from what a speech recogniser *thinks* you said. It can make mistakes.
> The safety guard is a speed bump, not a sandbox. Use it at your own risk, don't run it on machines holding data you
> can't afford to lose, and read "Safety" below before your first command.

## What it does

- **Hands-free-ish**: toggle a hotkey (the backtick key, `` ` ``), talk, toggle again. Speech recognition runs locally (Whisper).
- **Does real work** through Claude Code: open things, write and run code, search, manage files, drive helper agents in parallel (each gets its own status box).
- **Talks back** locally with Kokoro (neural TTS) plus an optional AIDA-style filter. Falls back to the Windows voice if needed.
- **Instant commands** ("open Chrome", "go to BBC News", "search for ...") skip Claude entirely.
- **Memory**: every word is logged on disk, a small recent-chat file keeps Claude in context, topic files hold facts, and an "end of day" command files everything away. It learns your phrasing and vocabulary over time (it starts blank).
- **Personality dials** by voice: humour, honesty, mirror (how much it talks like you).
- **Safety guard**: destructive, sending, paying and pushing actions are blocked until you say "confirmed".

## Requirements

- Windows 10 or 11, 64-bit, with a microphone and speakers/headset
- A **Claude account with Claude Code access** (a Pro/Max plan or API billing). You sign in with *your own* account; this project never touches your credentials
- Internet for the install and for Claude (about 600 MB of downloads, plus about 1 GB of Python packages; about 1.5 GB more if you have an NVIDIA GPU)
- Optional: an NVIDIA GPU (faster speech recognition). Without one it uses the CPU automatically
- Nothing else by hand: the installer gets Python 3.13, Claude Code, and (optionally) the GitHub CLI. No admin rights needed, except possibly for the Visual C++ runtime on a PC that doesn't already have it (Windows will ask)

## Install (one line)

Open **PowerShell** (not as admin) and paste:

```powershell
irm https://raw.githubusercontent.com/stagged1-cloud/AIDA/main/install.ps1 | iex
```

It installs to `%USERPROFILE%\AIDA`, asks a few questions (your name, spelling, voice, microphone), and ends with a
pass/fail checklist. It is safe to run again at any time; it only does what is missing.

Want to see what it will do first? Dry run (changes nothing):

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/stagged1-cloud/AIDA/main/install.ps1))) -DryRun
```

### Alternative: clone and run

```powershell
git clone https://github.com/stagged1-cloud/AIDA "$env:USERPROFILE\AIDA"; powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\AIDA\install.ps1"
```

Updating later is the same line again (or `git pull upstream main` once you have your own copy, see below).

### Options

Pass options with the scriptblock form: `& ([scriptblock]::Create((irm <url>))) -InstallDir D:\AIDA -Cpu`

| Option | Meaning |
|---|---|
| `-InstallDir PATH` | Install somewhere else (default `%USERPROFILE%\AIDA`, or the folder you cloned into) |
| `-DryRun` (or `-WhatIf`) | Show what would happen; change nothing |
| `-RepoName NAME`, `-Visibility private\|public` | Name and visibility of *your* GitHub copy (default `aida-assistant`, private) |
| `-NoGitHub` | Skip the GitHub step (AIDA works fine without it) |
| `-SyncMemory` | Opt in to backing up learned memory to your own private repo |
| `-Cpu` | Never try the GPU |
| `-SkipModels`, `-SkipClaude`, `-NoWizard`, `-NoShortcut`, `-StartWithWindows`, `-Yes` | Self-explanatory (`-Yes` = no questions) |

Environment-variable equivalents for the `irm | iex` form: `AIDA_DIR`, `AIDA_DRYRUN=1`, `AIDA_YES=1`, `AIDA_UPSTREAM=owner/repo`.

## First run

1. **Sign in to Claude (one time, by you).** Open a terminal, run `claude`, and complete the browser sign-in with your own account (type `/login` if it asks). The installer's last check tells you if this still needs doing.
2. Start **AIDA** from the Desktop shortcut (or `start.bat`). The first start loads the models (a few seconds, longer the first time).
3. A small status box appears. Press `` ` `` and talk, press `` ` `` again to send. `` Ctrl+` `` cancels the current job.
4. Try: "open Notepad", "what can you do", "make me a folder called Test under Documents".

### Your own GitHub copy (optional, recommended if you want to tinker)

The installer can create **your own private copy** of AIDA on **your** GitHub account:

1. It installs the GitHub CLI (`gh`) if missing and walks you through `gh auth login` (a code, a browser tab, one click). No GitHub account yet? It prints the sign-up link and carries on without this step; run the installer again later.
2. It creates `YOU/aida-assistant` (private by default; rename with `-RepoName`) and makes it your `origin`. If a repo of that name already exists it just links it and pushes nothing.
3. The original project becomes `upstream`, with its **push URL set to `DISABLED`**, so you can never push to it by accident.
4. Daily use: `git push` goes to your repo; `git pull upstream main` brings in updates from the original.

**Why a fresh independent repo instead of a fork?** A fork of a public repo is always public, stays attached to the original (so PRs and "behind by N commits" banners point at it) and can't be made private on a free account. A fresh repo made with `gh repo create --source . --push` is yours alone: private by default, no link back, no surprise visibility. The cost: no one-click "Sync fork" button, so updates are `git pull upstream main`. Prefer a fork anyway? Run the installer with `-NoGitHub` and use `gh repo fork --remote` yourself.

**Memory sync is off by default.** Your conversation memory is private and git-ignored. If you opt in (prompt, `-SyncMemory`, or `sync_memory: true`), only `assistant/memory/topics/` is pushed, **only to `origin`**, and only if that repo is private; the program refuses to sync if `origin` is missing, disabled, or the same as `upstream`. The raw day logs, `today.md` and `memory.db` are never pushed.

## Voice commands

Handled by the program itself (no Claude round-trip):

| Say | Does |
|---|---|
| "Initiate AIDA" | Start of day: fresh conversation, then a spoken briefing from your week summary and to-do list |
| "Initiate end of day" | Files the day into the memory topic files and week summary (uses the big model) |
| "Stop" / "shut up" | Stops talking |
| "Cancel" | Kills the current job |
| "Repeat that" | Says the last reply again |
| "Start fresh" / "new conversation" | New Claude conversation (today's memory still loads) |
| "Humour setting 80", "honesty setting 90", "mirror setting 60" | Personality dials, 0 to 100 |
| "Confirmed" | Lets one blocked delete/send/push through, for 3 minutes |
| "Open Chrome / VS Code / Notepad / my projects folder", "go to BBC News", "search for ..." | Instant, with a chime. The list lives in `config.yaml` |
| Say "think hard" anywhere | Uses the big model for that job |
| Mention email, calendar, Drive, Jira... | Loads your Claude connectors (if you set any up) for that job |

Everything else goes to Claude.

## Configuration

`config.yaml` (created from `config.example.yaml`; git-ignored; restart AIDA after editing). Main settings:

| Key | Default | What it does |
|---|---|---|
| `user_name` | (your name) | How logs and memory refer to you |
| `language_style` | `British English` | Spelling and conventions Claude uses: "American English", "Australian English"... |
| `time_label` | blank | Optional text after the time in each message header (e.g. `UK`) |
| `hotkey`, `cancel_hotkey` | `` ` ``, `` ctrl+` `` | Talk toggle and cancel |
| `microphone` | blank (Windows default) | Part of a device name |
| `tts_engine` | `kokoro` | `kokoro` (local neural) or `sapi` (Windows voice) |
| `kokoro_voice`, `kokoro_lang` | `am_onyx`, `en-us` | Voice and its language (`en-gb` for `bm_*`/`bf_*`) |
| `aida_voice` | `true` | Robot-ish filter on the voice |
| `whisper_model` | `small.en` | `medium.en` is more accurate, slower. English only |
| `whisper_device` | `auto` | `auto` tries the GPU, falls back to CPU; `cpu` never tries |
| `vocabulary` | a few words | Names and jargon the recogniser should expect |
| `model`, `model_deep` | `sonnet`, default | Claude models for everyday and "think hard" jobs |
| `humour`, `honesty`, `mirror` | 60, 90, 30 | Personality dials (also by voice) |
| `learn_every_min` | 10 | How often it updates your speech profile (0 = never) |
| `sync_memory` | `false` | Opt-in backup of memory topics to *your* repo (see above) |
| `idle_minutes`, `task_timeout_min` | 30, 30 | New conversation after idle; kill runaway jobs |
| `apps`, `sites` | common ones | The instant-command lists; add your own |

## How memory works

All under `assistant/memory/`, all local and git-ignored:

- `days/DATE.md`: every word of each day, written automatically (plus `memory.db`, searchable with `python memory\search.py "words"`)
- `today.md` and `recent.md`: today's log, and the last ~40 lines which Claude loads at the start of each conversation (keeps context small)
- `topics/`: `todo.md`, `projects.md`, `technical.md`, `decisions.md`, `people.md`, `history.md`, `week_summary.md`, and `how_you_talk.md` (your vocabulary and phrasing). They start as empty templates (copied from `memory/templates/`) and Claude fills them at end of day or when you say "remember that..."

Delete the folder contents any time to start blank. Don't tell it passwords.

## Safety

Claude Code is launched with `--dangerously-skip-permissions` so it can work hands-free. The only brake is `assistant/guard.py`, a hook that runs before every tool call and **blocks** things that look destructive (delete, uninstall, shutdown, `git push`, formatting), anything that sends/posts/pays through a connector, until you say "confirmed" (valid for 3 minutes). It is pattern matching: it will not catch every dangerous command, and it relies on Claude honouring the block. Keep backups, don't give it a PC with anything irreplaceable, and read the status box when it is working. Edit the patterns in `guard.py` to suit you.

## Privacy

Speech recognition, the voice and your memory files all run and live on your PC. The only thing sent out is your transcribed text and Claude's tool activity, to Claude (Anthropic) through Claude Code, under Anthropic's data policies and your own plan. The installer downloads models from GitHub and Hugging Face. Nothing is sent to the author. Optional GitHub features only talk to your own account.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Installer says script is disabled | `powershell -ExecutionPolicy Bypass -File .\install.ps1` |
| "Claude Code has logged out" / checklist says Claude FAIL | Terminal: `claude`, sign in. Try `claude -p "hi"` |
| Voice is the plain Windows one | Models missing: re-run the installer; check `models\` has two files |
| Won't start / DLL error | Install the "Microsoft Visual C++ 2015-2022 Redistributable (x64)"; re-run installer |
| Slow recognition | No NVIDIA GPU: normal. Use `whisper_model: base.en` for speed |
| It hears nothing / wrong mic | Re-run `.venv\Scripts\python.exe setup_wizard.py`, or set `microphone:` |
| Backtick key does nothing in some apps | Change `hotkey` in `config.yaml` (e.g. `f9`); run as normal user |
| Anything else | `.venv\Scripts\python.exe doctor.py --claude` prints a checklist; logs are in `logs\` |

## Uninstall

`powershell -File uninstall.ps1` removes the shortcuts (add `-RemoveFolder` to delete the whole folder, memory included; use `-DryRun` first). Python, Claude Code and `gh` stay (remove them in Windows Settings > Apps if you want). Your GitHub repo stays until you delete it.

## Credits and licences

MIT licensed. Voice: Kokoro (Apache-2.0) via kokoro-onnx. Speech: OpenAI Whisper via faster-whisper. Brain: Claude Code. Full list in `THIRD_PARTY.md`. Unofficial hobby project; not affiliated with Anthropic.
