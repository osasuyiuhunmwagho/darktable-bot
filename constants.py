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
    OUTPUT_SUBFOLDER_NAME = os.getenv("OUTPUT_SUBFOLDER_NAME", "Edited")
    FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
    DOWNLOAD_CHUNK_SIZE = 10 * 1024 * 1024


class Darktable:
    """darktable-cli location and the edit (style) to apply."""

    CLI = os.getenv("DARKTABLE_CLI", r"C:\Program Files\darktable\bin\darktable-cli.exe")
    STYLE = os.getenv("DARKTABLE_STYLE") or None
    CONFIG_DIR = os.getenv("DARKTABLE_CONFIG_DIR") or None


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
