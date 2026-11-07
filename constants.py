"""
All paths, Drive folders and darktable settings for the edit pipeline, grouped by class.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")


class Paths:
    """Local files and folders on this machine."""

    PIPELINE_DIR = Path(__file__).resolve().parent
    CREDENTIALS_FILE = PIPELINE_DIR / "credentials.json"  # OAuth "Desktop app" client from Google Cloud
    TOKEN_FILE = PIPELINE_DIR / "token.json"              # created on first sign-in
    WORK_DIR = PIPELINE_DIR / "work"                      # downloads + exports when --keep-local is used
    DOWNLOAD_DIR = WORK_DIR / "downloads"
    EXPORT_DIR = WORK_DIR / "exports"
    STYLES_DIR = PIPELINE_DIR / "styles"                  # exported darktable .dtstyle files


class Drive:
    """Google Drive folders and API settings."""

    SCOPES = ["https://www.googleapis.com/auth/drive"]
    # Parent folder that new shoot folders get dropped into. Accepts an ID or a full folder URL.
    # Set in .env (gitignored) so the folder link isn't committed.
    ROOT_FOLDER_ID = os.getenv("DRIVE_ROOT_FOLDER_ID")
    # Edited images are uploaded into this subfolder inside the shoot folder (created if missing).
    OUTPUT_SUBFOLDER_NAME = os.getenv("OUTPUT_SUBFOLDER_NAME", "Auto Edited")
    # Folders whose name contains this (any case) hold finished edits, not raws: never processed,
    # and used as the "after" side when learning the look.
    EDITED_FOLDER_KEYWORD = "edit"
    FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
    DOWNLOAD_CHUNK_SIZE = 10 * 1024 * 1024


class Darktable:
    """darktable-cli location and the edit (style) to apply."""

    CLI = os.getenv("DARKTABLE_CLI", r"C:\Program Files\darktable\bin\darktable-cli.exe")
    STYLE = os.getenv("DARKTABLE_STYLE") or None   # style for shoots that don't have their own
    CONFIG_DIR = os.getenv("DARKTABLE_CONFIG_DIR") or None
    # Your normal darktable config. Styles you create in the darktable app live in its data.db;
    # the pipeline copies that file so darktable-cli can use them (darktable can stay open).
    USER_CONFIG_DIR = Path(os.getenv("DARKTABLE_USER_CONFIG_DIR",
                                     Path(os.getenv("LOCALAPPDATA", Path.home())) / "darktable"))
    STYLE_TEST_DIR = Paths.WORK_DIR / "style-test"   # output of `python dt_styles.py --try ...`
    # Stops darktable-cli opening a console window when the scheduled sweep runs under pythonw.
    NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # subprocess.CREATE_NO_WINDOW


class Export:
    """Output file format and size."""

    FORMAT = os.getenv("OUTPUT_FORMAT", "jpg")
    MAX_WIDTH = int(os.getenv("OUTPUT_MAX_WIDTH", "0"))    # 0 = original size
    MAX_HEIGHT = int(os.getenv("OUTPUT_MAX_HEIGHT", "0"))

    MIME_TYPES = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "tif": "image/tiff",
        "tiff": "image/tiff",
    }

    IMAGE_EXTENSIONS = {
        ".jpg", ".jpeg", ".png", ".tif", ".tiff",
        ".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf", ".rw2", ".pef", ".srw",
    }
    RAW_EXTENSIONS = {".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf", ".rw2", ".pef", ".srw"}


class Look:
    """
    The learned looks applied after darktable: exposure matching, then a 3D LUT fitted from your edits.
    Each shoot gets its own look, learned from the photos of that shoot you edited by hand.
    """

    LOOKS_DIR = Paths.STYLES_DIR / "looks"          # <shoot>.cube (also usable in darktable's lut3d) + <shoot>.json
    ALL_SHOOTS_NAME = "all-shoots"                  # fallback look learned across every shoot
    # Your own LUTs: drop "<shoot folder name>.cube" here (or default.cube for every shoot without one).
    # They win over the learned looks. Applied after darktable (and your darktable style, if any).
    USER_LUTS_DIR = Paths.STYLES_DIR / "luts"
    DEFAULT_USER_LUT = "default"
    ENABLED = os.getenv("LOOK_ENABLED", "1") != "0"
    MIN_EDITS_PER_SHOOT = 5                         # hand edits needed before a shoot gets its own look
    MAX_EDITS_PER_SHOOT = 30                        # more than this adds download time but little accuracy
    # 0 = keep the camera's exposure, 1 = bring every photo fully to the same brightness.
    EXPOSURE_STRENGTH = float(os.getenv("EXPOSURE_STRENGTH", "0.4"))
    MAX_EXPOSURE_EV = 1.5                           # never brighten/darken a photo by more than this
    JPEG_QUALITY = 95


class Training:
    """Settings for build_look.py, which learns the look from raw + edited pairs in Drive."""

    DIR = Paths.WORK_DIR / "training"               # cached raws, edits and renders
    COMPARE_DIR = Paths.WORK_DIR / "compare"        # side-by-side checks on held-out photos
    RENDER_SIZE = 1200                              # px; training renders are small to keep it fast
    ALL_SHOOTS_MAX_PAIRS = int(os.getenv("TRAINING_MAX_PAIRS", "80"))
    HOLDOUT_EVERY = 5                               # every 5th pair is kept out of training to test on
    MIN_MATCH_SCORE = 0.8                           # skip pairs whose edit can't be lined up with the raw
    SAMPLES_PER_PAIR = 30000
    SAMPLE_WIDTH = 200                              # px; colors are compared at low res so tiny misalignments don't matter
    FLAT_PERCENTILE = 50                            # only sample the flattest half of each photo (no edges)
    FIT_LUT_SIZE = 17                               # grid the LUT is solved on
    CUBE_SIZE = 33                                  # grid written to the .cube file
    SMOOTHNESS = 1.0                                # higher = smoother, more conservative LUT


class Sweep:
    """sweep.py: the hourly check for new raws, hand edits and style changes (see README)."""

    STATE_FILE = Paths.WORK_DIR / "sweep-state.json"   # Drive change token + shoots still to finish
    LOCK_FILE = Paths.WORK_DIR / "sweep.lock"           # stops two sweeps running at once
    LOG_FILE = Paths.WORK_DIR / "sweep.log"             # output of scheduled runs (--log)
    # Tag on each uploaded edit naming the style/look that made it, so edits are redone when that changes.
    EDIT_TAG_KEY = "rvEdit"
