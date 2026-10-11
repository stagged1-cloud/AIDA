# winctl.py - native Windows app control (UI Automation)

JSON out, one object per call, exit code 0 = ok, 1 = not ok. Uses the `uiautomation` package (installed in `.venv`, pulls in `comtypes`).
Prefers methods that never move the real mouse. Every result has `method`, `real_input_used`, `cursor_touched`, `cursor_before`, `cursor_after`.

    $py = "$env:USERPROFILE\AIDA\.venv\Scripts\python.exe"     # adjust if AIDA is installed elsewhere
    $w  = "$env:USERPROFILE\AIDA\winctl.py"

## Selectors
- Window: title substring (`Calculator`), `pid:1234`, `hwnd:0x1A2B`, `process:notepad.exe`. First match wins. Calculator and other UWP apps live under ApplicationFrameHost, so use the title, not the app's pid.
- Control: `name=Equals`, `id=num2Button`, `name~=part`, `type=Button`, `class=Edit`, `index=1` (nth match, 0-based), combined with commas (`name=Two,type=Button`). A bare word means name-contains or id-equals. Quote values containing commas.

## Commands
    & $py $w list-windows                         # windows (title, process, pid, hwnd, monitor, rect) + monitors (number, label left/middle/right, rect, dpi, scale)
    & $py $w tree Calculator --depth 6 --patterns # control tree; --patterns shows which UIA patterns each node supports
    & $py $w click Calculator "id=num2Button"     # InvokePattern -> Toggle -> SelectionItem -> LegacyIAccessible
    & $py $w type pid:1234 "type=Document" "hello" [--append]   # ValuePattern -> WM_SETTEXT/WM_CHAR
    & $py $w read Calculator "id=CalculatorResults"             # Value -> Text -> LegacyIAccessible -> Name
    & $py $w focus Notepad ["selector"]           # SetFocus + SetForegroundWindow (changes focus, never the cursor)
    & $py $w screenshot-window Calculator C:\temp\calc.png      # that window only, even if partly covered
    & $py $w screenshot-monitor 2 C:\temp\mon2.png              # whole monitor, numbered left to right

`click` and `type` take `--allow-real-input` to permit a real mouse click / real keystrokes when no UIA method exists.
Without it they fail cleanly: `{"ok": false, "error": "no_uia_method", "fallback_available": "real_mouse_click", ...}` and nothing is done.
With it, a click reports `method: real_mouse_click`, `cursor_touched: true`, and the cursor is put back afterwards (`cursor_restored`).

Errors (`ok: false`): `window_not_found`, `control_not_found`, `bad_selector`, `no_uia_method`, `monitor_not_found`, `capture_failed`, `unexpected`.

## Worked example (verified)
Calculator 12*12: `click` clearButton, num1Button, num2Button, multiplyButton, num1Button, num2Button, equalButton, then `read Calculator "id=CalculatorResults"` gives `Display is 144`. Notepad: `type pid:N "type=Document" "text"` then `read` returns it. Real cursor identical before and after.

## Known limits
- Everything depends on the target app exposing UIA. Win32/WinUI/UWP/WPF are good; games, custom-drawn UIs, some Electron/Java apps expose little or nothing. Run `tree` first; if it is near-empty, UIA will not work.
- `type` with ValuePattern replaces the whole field (use `--append` to add). It does not press Enter or fire per-key events; apps that only react to keystrokes may need `--allow-real-input`.
- LegacyIAccessible DoDefaultAction reports ok even if the app ignores it; verify with `read`.
- `focus` can steal keyboard focus from the user. Avoid it unless the next step needs real input.
- Calculator-style apps give state as a Name (`Display is 144`), not a Value.
- Minimised windows cannot be screenshotted; `focus` first. PrintWindow can come back black for some GPU-drawn apps; the tool then falls back to a screen-region grab and says so (`note`), which can include overlapping windows.
- The process is per-monitor DPI aware, so rects, monitor rects and screenshots are all physical pixels and agree. Monitor numbers are left to right by x position.
- Elevated (admin) windows cannot be driven from a non-elevated process.
- Tree walks over big apps are slow; keep `--depth` low and use `--patterns` sparingly.

Tests: `.venv\Scripts\python.exe -m unittest test_winctl -v` (the live ones open and close their own Notepad and Calculator).
