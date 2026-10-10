"""
Safety hook for claude2claude voice (AIDA).
Claude Code runs this before every tool call. Anything destructive, or anything
that sends, posts or pays, is blocked unless the user has said "confirmed" in the
last 3 minutes (voice_claude.py writes .confirm_token when they do).
Exit code 2 blocks the call and hands the message back to Claude.

This is a speed bump, not a sandbox: it pattern-matches commands. It will not catch everything.
"""
import re
import sys
import json
import time
import pathlib

TOKEN = pathlib.Path(__file__).resolve().parent / ".confirm_token"

DESTRUCTIVE = re.compile(
    r"(?<![\w-])("
    r"rm|rmdir|del|erase|rd|Remove-Item|ri|Clear-Content|Clear-RecycleBin|Format-Volume|diskpart|"
    r"Remove-ItemProperty|reg\s+delete|Stop-Computer|Restart-Computer|shutdown|"
    r"Send-MailMessage|Uninstall-Package|winget\s+uninstall|"
    r"git\s+push|git\s+reset\s+--hard|git\s+clean|git\s+branch\s+-D|"
    r"format\s+[a-z]:"
    r")(?![\w-])",
    re.I,
)
# Python/Node one-liners that delete things
SNEAKY = re.compile(r"(os\.remove|os\.unlink|shutil\.rmtree|rmtree|unlinkSync|rmSync|\.unlink\(|fs\.rm)", re.I)
RISKY_TOOL = re.compile(r"(send|post|reply|forward|publish|delete|remove|trash|purchase|buy|pay|transfer|share|invite)", re.I)


def confirmed():
    try:
        return time.time() < float(TOKEN.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    tool = data.get("tool_name", "") or ""
    inp = data.get("tool_input", {}) or {}
    command = str(inp.get("command", "") or "")

    risk = None
    if command and (DESTRUCTIVE.search(command) or SNEAKY.search(command)):
        risk = "command: " + command[:200]
    elif tool.startswith("mcp__") and RISKY_TOOL.search(tool.split("__")[-1]):
        risk = "tool: " + tool

    if risk and not confirmed():
        print(
            "BLOCKED until the user confirms by voice (" + risk + "). "
            "Stop here. In one short spoken sentence tell them exactly what you are about to do "
            "and ask them to say 'confirmed'. Do not look for another way to do it.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
