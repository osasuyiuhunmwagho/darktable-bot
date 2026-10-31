# Real Victors edit pipeline

Takes the newest shoot folder in a Google Drive folder, edits its photos with `darktable-cli`, and uploads the results to an `Edited` subfolder inside that shoot folder.

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

## Run

```sh
python drive_darktable.py --test --keep-local   # one random image from the newest folder; keeps files in work/
python drive_darktable.py                       # every image in the newest folder
python drive_darktable.py --folder <id or URL>  # a specific shoot folder
```

The first run opens a browser to sign in to Google, then saves `token.json`. Images that already have an output in `Edited/` are skipped. Use `--force` to re-process them.

## Applying an edit (darktable styles)

A **style** is a saved set of darktable edits (exposure, color, crop, and so on) that can be applied to any photo. This is the edit the pipeline applies.

1. Open a typical photo from a shoot in darktable and edit it in the darkroom until it looks right.
2. In the **history stack** module (left panel), click **compress history stack**, then the **create style** button. Name it, e.g. `RV Look`. Untick modules that shouldn't carry over to other photos (crop, spot removal).
3. Optional: in the lighttable **styles** module, export it to a `.dtstyle` file in `styles/` so it's in git and other machines can import it.
4. Set `DARKTABLE_STYLE=RV Look` and `DARKTABLE_CONFIG_DIR=C:\Users\<you>\AppData\Local\darktable` in `.env`. The CLI can only see styles from that config folder, so close the darktable GUI while the script runs.
5. Run `python drive_darktable.py --test --keep-local`, check `work/exports/`, adjust the style, and repeat.

Where to get presets:
- darktable's built-in module presets (the ☰ icon on each module) can go into a style.
- Community styles are available as `.dtstyle` files, e.g. on https://discuss.pixls.us. Import them from the lighttable **styles** module.
- Lightroom `.xmp` presets don't work in darktable and have to be recreated.

**Per-photo edits:** if `IMG_001.CR3.xmp` (or `IMG_001.xmp`) sits next to `IMG_001.CR3` in the shoot folder, darktable applies it. Use this for photos that need hand edits on top of the style.
