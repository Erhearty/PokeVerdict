# The Shortcuts route — no signing, no developer account

Two taps on the back of the phone: screenshot → POST → verdict notification.
Nothing installed on the iPhone but a Shortcut.

## Why this exists

The native app needs code signing, and a free Apple ID means reinstalling every
7 days. This path needs neither. The whole appraisal pipeline runs on your own
machine; the phone only takes a picture and shows the answer.

---

## Proven end to end

The server is real and tested. Measured on the three reference screenshots:

```
$ python3 tools/shortcut_server.py --port 8765 &
$ curl --data-binary @IMG_5324.PNG "http://localhost:8765/appraise?format=text"
KEEP · Mewtwo · CP 2387 · 15/15/15 = 100.0% · L20

$ curl --data-binary @IMG_5325.PNG "http://localhost:8765/appraise?format=text"
UNDECIDED · Heatmor · CP 1550 · 8/14/6 = 62.2% · L24

$ curl --data-binary @IMG_5326.PNG "http://localhost:8765/appraise?format=text"
UNDECIDED · Komala · CP 1525 · 8/9/10 = 60.0% · L22
```

Latency 1.5–1.9 s per request, dominated by OCR on a full-resolution
1320×2868 screenshot.

Error paths degrade cleanly rather than guessing:

```
non-appraisal image  -> ! appraisal card not open in this screenshot
empty body           -> {"ok": false, "error": "empty body"}
not an image         -> {"ok": false, "error": "cannot identify image file"}
bars mid-animation   -> ! bars caught mid-animation (confidence 0.31)
```

**What is not proven here:** the Shortcut itself. It was written from Apple's
documentation, not executed — no iPhone in the build environment. Every action
it uses is standard and documented; see the references at the bottom.

---

## CP is derived, not read

The one genuinely interesting thing the server does.

CP is white text over whatever the catch background happened to be. On the
Mewtwo screenshot it sits against a sunlit planet and OCR returns nothing at
all. So the server doesn't depend on it.

Given the species, the stamina IV from the bar, and max HP, the level is pinned
directly — and CP follows from the level. Across 2000 random Pokémon, HP alone
gives a unique level **54.5%** of the time. When it doesn't, the server reports
the candidate CPs and uses OCR to pick between them if it can.

Crucially, **the IVs and the verdict never depend on CP at all** — they come
from the bars and the species. CP only refines the level readout.

On the Mewtwo screenshot:

```json
"ocr":       { "name": "Mewtwo", "hp": "81 / 136 HP", "cp": null },
"cp":        2387,
"cpSource":  "derived (HP pinned the level uniquely)"
```

OCR failed, the answer was still exact. On the other two, OCR succeeded and
agreed with the derivation, which is used as a cross-check.

Note the HP string: `81 / 136 HP`. Mewtwo is damaged. The server takes the
maximum, not the first number.

---

## Running the server

```bash
sudo apt install tesseract-ocr
pip install pillow numpy pytesseract

python3 tools/shortcut_server.py --port 8765
python3 tools/shortcut_server.py --port 8765 --ntfy https://ntfy.sh/YOUR-SECRET-TOPIC
```

Check it works before touching the phone:

```bash
python3 tools/shortcut_server.py --selftest fixtures/shots/*.PNG
```

### Reaching it from the phone

| Option | Notes |
|---|---|
| Same Wi-Fi | Simplest. Use your machine's LAN IP. Breaks when you leave the house — which is where you play. |
| Tailscale | What I'd use. Free, no port forwarding, works on mobile data. |
| Cloudflare Tunnel | Public HTTPS without opening a port. |
| VPS | Always on, costs a few euros. |

Do not expose this to the open internet unprotected. It executes OCR on
arbitrary uploads. If it must be public, put a shared secret in a header and
check it.

### ntfy

`--ntfy` forwards each verdict to an [ntfy.sh](https://ntfy.sh) topic, which
the ntfy iOS app receives as a push. Pick an unguessable topic name — topics
are public by default. Self-hosting is a one-container job.

This is optional. Without it the Shortcut still shows the result directly, and
that path needs no third party at all.

**Untested here:** the outbound push. ntfy.sh isn't reachable from my build
sandbox, so `push_ntfy` is written but unverified. The local path is fully
tested.

---

## The Shortcut

New Shortcut, name it "Appraise", four actions:

**1. Take Screenshot**
No configuration.

**2. Get Contents of URL**
- URL: `http://YOUR-HOST:8765/appraise?format=text`
- Method: **POST**
- Request Body: **File**
- File: the `Screenshot` variable from step 1

**3. Show Result**
Pass the output of step 2. Renders as a banner over the game.

Optional **4. Set Variable / Add to Note** to keep a running log without any
database.

Then: Settings → Accessibility → Touch → **Back Tap** → Double Tap → Appraise.

### Using it

1. Catch something
2. Tap **Appraise** in the game — the bars must be on screen
3. Double-tap the back of the phone
4. Verdict appears in about two seconds

Screenshots land in Photos. A "Delete Photos" action at the end keeps the
library clean, at the cost of a confirmation prompt in some iOS versions.

### If it doesn't work

| Symptom | Cause |
|---|---|
| "Could not connect" | Wrong host, or phone not on the same network. Test the URL in Safari on the phone first. |
| `appraisal card not open` | Screenshot taken before the appraisal bars rendered. Add a 0.5 s Wait action before the screenshot. |
| `bars caught mid-animation` | The fill was still sweeping. Wait for it to settle. |
| `unknown species from OCR` | Species name misread. The raw OCR string is in the JSON response — switch to `format=json` to see it. |
| Wrong IVs | Geometry differs on your device. Re-run `tools/calibrate.py` on your own screenshot. |

---

## What you give up versus the native app

- **No collection database.** The server is stateless. Adding SQLite is
  straightforward — the schema is already in `CollectionDatabase.swift` — but
  it isn't wired up here.
- **Two taps, not zero.** The native extension watches the screen
  continuously; this needs a deliberate trigger.
- **Server must be reachable.** No signal, no verdict.
- **Screenshots in Photos** unless you delete them.

What you gain: it works today, on Linux, with no Apple developer account, no
Xcode, no weekly reinstall, and no signing.

---

## References

- [Request your first API in Shortcuts](https://support.apple.com/guide/shortcuts/request-your-first-api-apd58d46713f/ios) —
  Apple's documentation for Get Contents of URL, including the File request body
- [Get Contents of URL action reference](https://matthewcassinelli.com/actions/get-contents-of-url/) —
  full parameter list
