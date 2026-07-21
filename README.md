# PoE Flipper

Tray tool for the Path of Exile currency exchange. Press **Alt+Q** while the
Market Ratio panel is visible (hover the ratio + hold Alt in game), type how
many of your "I Have" currency you're selling, and get 2–3 clean, divisible
ratio suggestions with copy buttons.

## Install on a new machine

Requirements: Windows 10/11 x64 with winget, PoE in (windowed) fullscreen on
the primary monitor. Any resolution works: coordinates are kept in 1920x1080
reference space and mapped by a height-scaled, center-anchored transform;
on the first capture (or whenever a capture stops parsing) the tool OCRs the
panel headers ("Market Ratio" / "I Want" / "I Have") to calibrate the exact
transform, verifies it, and caches it in `flipper_calib.json`. Auto-fill
click positions go through the same transform, so no manual calibration is
needed. Delete `flipper_calib.json` to force a fresh calibration.

1. Copy the folder (or unzip `poe-flipper.zip`) anywhere, e.g. `C:\poe-flipper`.
2. Run `setup.bat` once — it installs Python 3.12 (winget) if missing,
   creates the `.venv` with dependencies (~200 MB), and compiles
   `PoE Flipper.exe` with the icon.
3. Start `PoE Flipper.exe`, pin it to the taskbar if you like.
4. Open the exchange in game, hold Alt over the Market Ratio bar, press
   Alt+Q once — the first capture self-calibrates. Optionally sanity-check
   with `.venv\Scripts\python.exe flipper.py --pos`.

## Run

Launch `PoE Flipper.exe` — a tiny native launcher (compiled from
`launcher.cs` with the .NET Framework `csc` that ships with Windows) that
starts the tray app with the proper icon. Pin it to the taskbar or Start via
right-click. Launching it again while running does nothing (single
instance). `start_flipper.bat` still works too.

An arrow icon appears in the tray. Exit via right-click → Exit.

To auto-start with Windows: put a shortcut to `PoE Flipper.exe` in
`shell:startup`.

If the flipper.py path ever moves, recompile the launcher:
`C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe /target:winexe
/win32icon:flipper.ico /out:"PoE Flipper.exe" launcher.cs`

## Flow

1. In game, open the currency exchange, set I Want / I Have, hover the
   Market Ratio bar and hold Alt so Available/Competing tables show.
2. Press **Alt+Q** (Q is suppressed so it won't trigger your Q skill).
   The input popup appears instantly while OCR runs in the background.
3. Type your sell quantity, and/or Tab to the second field and enter a flip
   budget (in the I-Have currency) to plan buying the I-Want item for
   resale. Press Enter.
4. You get up to three suggestions:
   - **Fast** — undercuts the best competing offer (~3%), fills quickest.
   - **Fair** — matches the best competing price.
   - **Greedy** — prices between the 1st and 2nd competing offers.
   Every ratio is snapped so your quantity divides cleanly; if it can't
   (e.g. a prime quantity), it sells the largest clean chunk and tells you
   what's left over. The green "Instant" line estimates what you'd get by
   just taking the available trades right now.
5. With a flip budget, a "BUY → RESELL" section suggests bid ratios in the
   current orientation (outbid or match the rival buyers in Competing
   Trades) with projected resale revenue and profit, assuming you later
   resell 1% under the current sellers (Available Trades). Set up the panel
   with I Want = the item you're flipping and I Have = what you pay; no
   swapping needed.
6. Each row has a ⧉ button (copies the ratio, e.g. `22:1`) and a ⤷ button
   that auto-fills the game: it clicks the I Have amount field, clears it,
   types the amount, then does the same for I Want. Click positions come
   from the calibrated geometry automatically; `python flipper.py --pos`
   shows the computed points and live cursor position for sanity checks.
7. Esc, Enter, or clicking outside closes the popup.

If no competing offers exist (dead market), targets fall back to
market/available ratio +0/15/30% and a warning is shown.

## Auto-update

The app checks the repo's latest GitHub release shortly after startup and
silently updates itself: downloads the release zip, extracts it over the
app folder, refreshes venv dependencies / recompiles the launcher if those
changed, and restarts (waiting until no popup is open). A tray notification
confirms the new version. Disable with `"auto_update": false` in
`flipper_config.json`. Config, calibration and logs are never touched.

## Rebinding the hotkey

On first run the app writes `flipper_config.json` next to the script. Edit
it and restart:

```json
{ "hotkey": "ctrl+shift+x" }
```

Combos are modifiers (`alt`, `ctrl`, `shift`, `win`) plus one key: a letter,
digit, `f1`–`f24` or `space`. Invalid combos fall back to `alt+q` (see
`flipper.log`).

## Assumptions & tuning

- PoE in (windowed) fullscreen on the primary monitor; any resolution and
  Windows display scaling (the process is DPI-aware). Reference-space crop
  regions and anchors are at the top of `flipper.py`. Run
  `python flipper.py --dump` with the panel open to check alignment — it
  saves the crops as PNGs next to the script.
- Ratios are read as `want : have` (e.g. 13 : 1 = 13 chaos per 1 ancient).
- The "Instant" estimate assumes Available-Trades stock is denominated in the
  I-Want currency.
- Suggestion knobs at the top of `flipper.py`: `MAX_DENOM`, `WASTE_WEIGHT`,
  `ERR_WEIGHT`, `DENOM_WEIGHT`.
- Every capture saves `last_capture.png` / `last_ocr.txt` and appends to
  `flipper.log` for troubleshooting (set `DEBUG_SAVE = False` to disable).

## Test without the game

```
python flipper.py --test screenshot.png --qty 352
```

## Stack

Python 3.12 (venv in `.venv`) with: `mss` (screen capture), RapidOCR
(`rapidocr_onnxruntime`, offline OCR — Windows' built-in OCR proved unable to
read short table tokens like "8 : 1"), native Win32 `RegisterHotKey` for the
global Alt+Q (the `keyboard` library's hook delayed/re-injected Alt events and
made holding Alt in game unreliable), `pystray` (tray icon), `tkinter`
(popups), `Pillow`/`numpy` (imaging).
