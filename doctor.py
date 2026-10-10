"""
AIDA self-check. Prints a PASS / WARN / FAIL checklist and exits non-zero if anything FAILED.
  .venv\\Scripts\\python.exe doctor.py              quick checks (no network, no Claude call)
  .venv\\Scripts\\python.exe doctor.py --claude     also asks Claude for one word to prove you are logged in
"""
import os
import sys
import shutil
import pathlib
import subprocess

HERE = pathlib.Path(__file__).resolve().parent
results = []


def rec(status, name, detail=""):
    results.append((status, name, detail))
    colour = {"PASS": "92", "WARN": "93", "FAIL": "91", "INFO": "96"}[status]
    print(f"  \033[{colour}m[{status}]\033[0m {name}" + (f"  -  {detail}" if detail else ""))


def run(cmd, timeout=20):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        return e


def main():
    os.system("")  # enable ANSI on Windows consoles
    want_claude = "--claude" in sys.argv
    print("\nAIDA self-check\n")

    v = sys.version_info
    rec("PASS" if (3, 10) <= v[:2] <= (3, 13) else "WARN", f"Python {v.major}.{v.minor}.{v.micro}",
        "" if (3, 10) <= v[:2] <= (3, 13) else "tested on 3.10-3.13")

    for mod in ("yaml", "numpy", "scipy", "sounddevice", "keyboard", "win32com", "faster_whisper", "kokoro_onnx"):
        try:
            __import__(mod)
            rec("PASS", f"package {mod}")
        except Exception as e:
            rec("FAIL", f"package {mod}", f"{e.__class__.__name__}: {e}")

    cfg = {}
    try:
        import yaml
        cfg = yaml.safe_load((HERE / "config.yaml").read_text(encoding="utf-8")) or {}
        rec("PASS", "config.yaml reads OK", f"user_name={cfg.get('user_name') or '(blank)'}")
    except Exception as e:
        rec("FAIL", "config.yaml", f"{e.__class__.__name__}: {e}")

    for name, size in (("kokoro-v1.0.onnx", 300_000_000), ("voices-v1.0.bin", 20_000_000)):
        f = HERE / "models" / name
        if f.exists() and f.stat().st_size >= size:
            rec("PASS", f"voice model {name}", f"{f.stat().st_size / 1e6:.0f} MB")
        else:
            rec("WARN", f"voice model {name} missing or incomplete", "AIDA will use the Windows voice. Re-run install.ps1")

    try:
        import ctranslate2
        gpus = ctranslate2.get_cuda_device_count()
        if gpus:
            rec("PASS", "NVIDIA GPU for speech recognition", f"{gpus} CUDA device(s)")
        else:
            rec("INFO", "No usable NVIDIA GPU", "speech recognition will run on the CPU (slower, still fine)")
    except Exception as e:
        rec("INFO", "GPU check skipped", str(e))

    try:
        import sounddevice as sd
        ins = [d["name"] for d in sd.query_devices() if d["max_input_channels"] > 0]
        want = (cfg.get("microphone") or "").lower()
        if not ins:
            rec("FAIL", "microphone", "no input device found")
        elif want and not any(want in n.lower() for n in ins):
            rec("WARN", "microphone", f"'{cfg.get('microphone')}' not found; will use the Windows default")
        else:
            rec("PASS", "microphone", sd.query_devices(kind="input")["name"])
    except Exception as e:
        rec("FAIL", "microphone", str(e))

    claude = cfg.get("claude_path") or shutil.which("claude") or str(pathlib.Path.home() / ".local/bin/claude.exe")
    if pathlib.Path(claude).exists() or shutil.which(claude):
        r = run([claude, "--version"])
        if isinstance(r, Exception) or r.returncode != 0:
            rec("FAIL", "Claude Code runs", "claude --version failed")
        else:
            rec("PASS", "Claude Code installed", r.stdout.strip())
            if want_claude:
                r = run([claude, "-p", "Reply with the single word: ok", "--model", cfg.get("model") or "sonnet",
                         "--strict-mcp-config"], timeout=90)
                if isinstance(r, Exception):
                    rec("WARN", "Claude login test", f"timed out or failed: {r}")
                elif r.returncode == 0 and r.stdout.strip():
                    rec("PASS", "Claude is logged in and answering")
                else:
                    rec("FAIL", "Claude login", "open a terminal, run:  claude   then complete the sign-in "
                        "with YOUR Claude account (type /login if asked)")
            else:
                rec("INFO", "Claude login not tested", "run doctor.py --claude, or just start AIDA")
    else:
        rec("FAIL", "Claude Code", "not found. Install: irm https://claude.ai/install.ps1 | iex")

    if (HERE / ".git").exists():
        def g(*a):
            r = run(["git", "-C", str(HERE), *a])
            return "" if isinstance(r, Exception) or r.returncode else r.stdout.strip()
        o, u = g("remote", "get-url", "origin"), g("remote", "get-url", "upstream")
        if o:
            rec("PASS", "git origin (your own repo)", o)
        else:
            rec("INFO", "no git origin", "optional: re-run install.ps1 to link your own GitHub copy")
        if u:
            pu = g("remote", "get-url", "--push", "upstream")
            rec("PASS" if "DISABLED" in pu.upper() else "WARN", "git upstream is read-only", pu or "(unset)")
        if cfg.get("sync_memory"):
            rec("INFO", "memory sync is ON", "topics/ is pushed to origin only")

    bad = sum(1 for s, *_ in results if s == "FAIL")
    warn = sum(1 for s, *_ in results if s == "WARN")
    print(f"\n  {len(results) - bad - warn} ok, {warn} warnings, {bad} failed.\n")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
