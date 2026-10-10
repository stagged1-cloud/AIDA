"""
First-run wizard: creates config.yaml from config.example.yaml and asks the few things only you can answer.
Run by install.ps1; run it again any time:  .venv\\Scripts\\python.exe setup_wizard.py
Options:  --yes (no questions, keep defaults)   --name NAME   --sync on|off
"""
import re
import sys
import shutil
import pathlib
import argparse

HERE = pathlib.Path(__file__).resolve().parent
CFG = HERE / "config.yaml"
EXAMPLE = HERE / "config.example.yaml"

VOICES = [
    ("am_onyx", "en-us", "deep US male (default)"),
    ("am_adam", "en-us", "US male"),
    ("am_michael", "en-us", "US male, warmer"),
    ("bm_george", "en-gb", "British male"),
    ("bm_lewis", "en-gb", "British male, lighter"),
    ("af_heart", "en-us", "US female"),
    ("bf_emma", "en-gb", "British female"),
]


def set_key(key, value):
    """Replace the value of a top-level `key: value  # comment` line, keeping the comment."""
    text = CFG.read_text(encoding="utf-8")
    if isinstance(value, bool):
        v = "true" if value else "false"
    elif isinstance(value, (int, float)):
        v = str(value)
    else:
        v = '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'
    pat = re.compile(r"^(" + re.escape(key) + r":\s*)(\"(?:[^\"\\]|\\.)*\"|'[^']*'|[^\s#]+)", re.M)
    new, n = pat.subn(lambda m: m.group(1) + v, text, count=1)
    if n == 0:
        new = text.rstrip("\n") + f"\n{key}: {v}\n"
    CFG.write_text(new, encoding="utf-8", newline="\n")


def ask(prompt, default=""):
    try:
        a = input(f"{prompt}" + (f" [{default}]" if default else "") + ": ").strip()
    except EOFError:
        a = ""
    return a or default


def pick_mic():
    try:
        import sounddevice as sd
    except Exception as e:
        print(f"  Could not list microphones ({e}). Leaving the Windows default.")
        return ""
    devs = []
    seen = set()
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and d["name"] not in seen and "mapper" not in d["name"].lower():
            seen.add(d["name"])
            devs.append(d["name"])
    if not devs:
        print("  No microphone found. Plug one in, then re-run this wizard.")
        return ""
    print("  Microphones:")
    print("   0) Windows default (recommended unless it picks the wrong one)")
    for n, name in enumerate(devs, 1):
        print(f"   {n}) {name}")
    c = ask("  Which one", "0")
    if c.isdigit() and 1 <= int(c) <= len(devs):
        return devs[int(c) - 1]
    return ""


def mic_test():
    try:
        import numpy as np
        import sounddevice as sd
        print("  Say something for 2 seconds...")
        rec = sd.rec(int(2 * 16000), samplerate=16000, channels=1, dtype="float32")
        sd.wait()
        peak = float(np.max(np.abs(rec)))
        bar = "#" * int(min(1.0, peak * 3) * 30)
        print(f"  Level: [{bar:<30}] peak {peak:.3f}")
        if peak < 0.01:
            print("  That is very quiet or silent. Check the mic in Windows Sound settings (Input).")
        else:
            print("  Mic works.")
    except Exception as e:
        print(f"  Mic test failed: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--name")
    ap.add_argument("--sync", choices=["on", "off"])
    ap.add_argument("--cpu", action="store_true", help="no NVIDIA GPU: never try CUDA")
    args = ap.parse_args()

    if not CFG.exists():
        shutil.copyfile(EXAMPLE, CFG)
        print("Created config.yaml from config.example.yaml.")
    if args.cpu:
        set_key("whisper_device", "cpu")
    if args.sync:
        set_key("sync_memory", args.sync == "on")
    if args.name:
        set_key("user_name", args.name)
    if args.yes:
        return 0

    print("\n=== AIDA first-run setup (press Enter to accept the [default]) ===")
    name = ask("What should AIDA call you", args.name or "")
    if name:
        set_key("user_name", name)

    print("\nSpelling and conventions Claude should use:")
    print("   1) British English (UK dates, GBP)")
    print("   2) American English (US dates, USD)")
    print("   3) Something else (you type it, e.g. 'Australian English')")
    c = ask("  Choice", "1")
    if c == "2":
        set_key("language_style", "American English")
        set_key("search_url", "https://www.google.com/search?q=")
    elif c == "3":
        set_key("language_style", ask("  Describe it", "British English"))
    else:
        set_key("language_style", "British English")
    tz = ask("Short label shown after the time, e.g. UK or Eastern (optional)", "")
    set_key("time_label", tz)

    print("\nVoices (Kokoro, runs on this PC):")
    for n, (v, _, desc) in enumerate(VOICES, 1):
        print(f"   {n}) {v:<11} {desc}")
    c = ask("  Choice", "1")
    v = VOICES[int(c) - 1] if c.isdigit() and 1 <= int(c) <= len(VOICES) else VOICES[0]
    set_key("kokoro_voice", v[0])
    set_key("kokoro_lang", v[1])

    print("\nMicrophone:")
    set_key("microphone", pick_mic())
    if ask("  Test it now (y/n)", "y").lower().startswith("y"):
        mic_test()

    extra = ask("\nAny names or jargon AIDA often mishears? Comma-separated (optional)", "")
    if extra:
        set_key("vocabulary", "Claude, Claude Code, AIDA, " + extra)

    print("\nSaved to config.yaml. Edit it any time; every setting is documented in the file and README.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
