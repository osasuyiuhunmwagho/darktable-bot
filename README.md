# Real Victors edit pipeline

Takes the newest raw shoot in a Google Drive folder, learns that shoot's look from the handful of photos you edited by hand, applies it to every other raw with `darktable-cli`, and uploads the results to an `Auto Edited` subfolder inside that shoot folder.

All folders and settings are in [constants.py](constants.py). Any value can be overridden in `.env` (see [.env.example](.env.example)).

## Setup

1. **Install darktable:** `winget install darktable.darktable` or https://www.darktable.org/install/. It includes `darktable-cli.exe`.
2. **Python deps:**
   ```sh
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. **Google OAuth client:**
   - In Google Cloud Console, create a project and enable the **Google Drive API**.
   - Go to *APIs & Services → Credentials → Create credentials → OAuth client ID → Desktop app*.
   - Download the JSON and save it as `credentials.json` in this folder.
   - If the consent screen is in "Testing" mode, add your Google account as a test user.
4. **Config:** copy `.env.example` to `.env` and set `DRIVE_ROOT_FOLDER_ID` to the Drive folder link. `.env` is gitignored, so the link stays out of the repo.

## How it works

### Option A: your darktable style (most precise)

1. In darktable, edit one typical raw from the shoot exactly how you want the shoot to look.
2. In the darkroom's **history stack** module (bottom left): **compress history stack**, then **create style**. Name it after the Drive shoot folder, e.g. `Nov 1st Game` or `RV Nov 1st Game`. Untick crop, rotate, retouch and other per-photo modules.
   For photos that came out brighter/darker than others: in the **exposure** module set **mode: automatic** before creating the style. darktable then measures each raw and sets its exposure so they all land at the same brightness (tested on 8 Nov 1 raws: brightness spread dropped from 32 to 3.5 levels). Tune **target level** for how bright you want them.
3. Check it on local raws (nothing is uploaded): `python dt_styles.py --try images --style "Nov 1st Game"` -> `work/style-test/`.
4. When you run the pipeline on that shoot, every raw is exported by `darktable-cli` with your style, then uploaded to `<shoot>/Auto Edited/`.

darktable can stay open: the pipeline copies the styles out of your darktable config (`%LOCALAPPDATA%\darktable\data.db`). Styles exported as `.dtstyle` files into `styles/` work too. Set `DARKTABLE_STYLE` in `.env` for a default style used by shoots without their own.

### Option B: learned look (no style for the shoot)

1. A shoot lands in Drive as a raw folder, e.g. `Nov 8 Game/` (CR2/CR3).
2. You edit **5-10 photos** from it as usual and put the JPGs in a folder with "edit" in the name, e.g. `EDITED NOV 8/`. File names must stay the same as the raw (`4A8A0754.CR3` -> `4A8A0754.JPG`; `IMG_5967_1.JPG` virtual copies are fine).
3. The bot pairs your edits with their raws, renders each raw with darktable's defaults, and learns a **3D LUT** that turns "darktable default" into "your edit" for that shoot. It's saved in `styles/looks/<shoot>.cube` (also loadable in darktable's *lut3d* module or other editors).
4. Every other raw in the shoot gets: darktable default render -> exposure matching (so a photo shot a stop dark comes out like the rest) -> the shoot's LUT -> JPG, uploaded to `<shoot>/Auto Edited/`.
5. Photos you edited by hand are skipped. Add more edits later and the look is re-learned automatically.

The LUT copies your color and tone grade. Crops, spot fixes and local edits aren't learned, so photos that need those still get a hand touch.

Each shoot gets its own look because you grade each venue/light differently: in testing, a per-shoot look was much closer to your edits than one look for every shoot (e.g. Thanksgiving: 6.0 vs 11.7 average color difference, where darktable's default was 8.9).

## Run

```sh
python drive_darktable.py --test --keep-local   # one random photo from the newest raw folder; keeps files in work/
python drive_darktable.py                       # every photo in the newest raw folder
python drive_darktable.py --folder <id or URL>  # a specific shoot folder
python drive_darktable.py --fallback            # shoot has no edits yet: use the all-shoots look
python build_look.py                            # (re)learn looks for every shoot and check them
```

The first run opens a browser to sign in to Google, then saves `token.json`. Images that already have an output in `Auto Edited/` are skipped, unless the shoot's darktable style or learned look has changed since that output was made; then it's redone in place (same Drive file). Use `--force` to re-process everything.

## Keep it up to date automatically

`sweep.py` checks Drive and your darktable styles, and processes every shoot that has new photos, new hand edits in an `EDITED ...` folder, or a new or changed style named after it. A check with nothing new takes a few seconds.

```sh
python sweep.py --dry-run                                  # show which shoots it would process
python sweep.py                                            # check and process now
powershell -ExecutionPolicy Bypass -File schedule_sweep.ps1   # run it every hour from Task Scheduler
```

The scheduled task runs every hour and at log-on, only while the laptop is on (it never wakes it). A check missed while the laptop was off runs as soon as it's back on. Pass `-Hours 2` for every two hours. Output goes to `work/sweep.log`, and `work/sweep-state.json` remembers where Drive was at the last check. The first run only starts watching from that moment and queues the newest shoot. For an older shoot, run `drive_darktable.py --folder` once.

If Google sign-in expires, the scheduled run can't open a browser: run `python sweep.py` by hand once to sign in again.

`build_look.py` prints how close each look gets on photos it didn't train on and writes `darktable default | auto look | your edit` side-by-sides to `work/compare/<shoot>/`.

Settings (minimum edits per shoot, exposure-matching strength, etc.) are in the `Look` and `Training` classes in [constants.py](constants.py).
