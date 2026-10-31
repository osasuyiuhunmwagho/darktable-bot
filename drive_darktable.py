"""
Google Drive -> darktable -> Google Drive pipeline.

1. Finds the newest shoot folder inside Drive.ROOT_FOLDER_ID (or the one given with --folder)
2. Downloads each image (plus its .xmp sidecar if one sits next to it in Drive)
3. Runs darktable-cli on it (optionally applying a darktable style)
4. Uploads the result to an "Edited" subfolder inside that shoot folder

Images that already have an output are skipped, so the script is safe to re-run.
All paths and settings live in constants.py.

Usage:
    python drive_darktable.py --test          # one random image from the newest folder
    python drive_darktable.py                 # every image in the newest folder
    python drive_darktable.py --folder <id>   # a specific shoot folder
"""

import argparse
import io
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from constants import Darktable, Drive, Export, Paths


def folder_id_from(value: str) -> str:
    """Accepts either a raw folder ID or a full Drive folder URL."""
    value = value.strip()
    if "/folders/" in value:
        value = value.split("/folders/")[1]
    return value.split("?")[0].split("/")[0]


def get_drive_service():
    creds = None
    if Paths.TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(Paths.TOKEN_FILE), Drive.SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Paths.CREDENTIALS_FILE.exists():
                sys.exit(
                    f"Missing {Paths.CREDENTIALS_FILE}. Download an OAuth 'Desktop app' client "
                    "from Google Cloud Console and save it there (see README.md)."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(Paths.CREDENTIALS_FILE), Drive.SCOPES)
            creds = flow.run_local_server(port=0)
        Paths.TOKEN_FILE.write_text(creds.to_json())
    return build("drive", "v3", credentials=creds)


def list_children(service, folder_id: str, folders: bool) -> list[dict]:
    op = "=" if folders else "!="
    query = f"'{folder_id}' in parents and trashed = false and mimeType {op} '{Drive.FOLDER_MIME_TYPE}'"
    items, page_token = [], None
    while True:
        resp = service.files().list(
            q=query,
            fields="nextPageToken, files(id, name, mimeType, createdTime)",
            orderBy="createdTime desc",
            pageSize=1000,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        items.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return items

def newest_shoot_folder(service, root_id: str) -> dict:
    subfolders = [f for f in list_children(service, root_id, folders=True) if f["name"] != Drive.OUTPUT_SUBFOLDER_NAME]
    if not subfolders:
        sys.exit(f"No subfolders found in Drive folder {root_id}.")
    return subfolders[0]  # list is sorted newest first


def get_or_create_folder(service, parent_id: str, name: str) -> str:
    for f in list_children(service, parent_id, folders=True):
        if f["name"] == name:
            return f["id"]
    created = service.files().create(
        body={"name": name, "mimeType": Drive.FOLDER_MIME_TYPE, "parents": [parent_id]},
        fields="id",
        supportsAllDrives=True,
    ).execute()
    return created["id"]

def download(service, file_id: str, dest: Path) -> None:
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    with io.FileIO(dest, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=Drive.DOWNLOAD_CHUNK_SIZE)
        done = False
        while not done:
            _, done = downloader.next_chunk()

def upload(service, src: Path, folder_id: str, mime_type: str) -> str:
    media = MediaFileUpload(str(src), mimetype=mime_type, resumable=True)
    created = service.files().create(
        body={"name": src.name, "parents": [folder_id]},
        media_body=media,
        fields="id",
        supportsAllDrives=True,
    ).execute()
    return created["id"]

def run_darktable(src: Path, xmp: Path | None, dest: Path, style: str | None, config_dir: Path) -> None:
    cmd = [Darktable.CLI, str(src)]
    if xmp:
        cmd.append(str(xmp))
    cmd.append(str(dest))
    if style:
        cmd += ["--style", style]
    if Export.MAX_WIDTH:
        cmd += ["--width", str(Export.MAX_WIDTH)]
    if Export.MAX_HEIGHT:
        cmd += ["--height", str(Export.MAX_HEIGHT)]
    cmd += ["--hq", "true", "--core", "--configdir", str(config_dir)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not dest.exists():
        raise RuntimeError(f"darktable-cli failed ({result.returncode}):\n{result.stderr or result.stdout}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Edit Drive photos with darktable and upload the results.")
    parser.add_argument("--folder", help="Shoot folder ID/URL to process (default: newest folder in the root)")
    parser.add_argument("--test", action="store_true", help="Process one random image only, to check the connection")
    parser.add_argument("--style", default=Darktable.STYLE, help="darktable style name to apply")
    parser.add_argument("--force", action="store_true", help="Re-process images that already have an output")
    parser.add_argument("--keep-local", action="store_true", help=f"Keep downloaded/exported files in {Paths.WORK_DIR}")
    args = parser.parse_args()

    if not args.folder and not Drive.ROOT_FOLDER_ID:
        sys.exit("Set DRIVE_ROOT_FOLDER_ID in .env (copy .env.example) or pass --folder.")
    if not Path(Darktable.CLI).exists() and not shutil.which(Darktable.CLI):
        sys.exit(f"darktable-cli not found at '{Darktable.CLI}'. Install darktable or set DARKTABLE_CLI.")

    out_ext = Export.FORMAT.lower().lstrip(".")
    out_mime = Export.MIME_TYPES[out_ext]

    service = get_drive_service()

    if args.folder:
        shoot = {"id": folder_id_from(args.folder), "name": args.folder}
    else:
        shoot = newest_shoot_folder(service, folder_id_from(Drive.ROOT_FOLDER_ID))
    print(f"Shoot folder: {shoot['name']} ({shoot['id']})")

    all_files = list_children(service, shoot["id"], folders=False)
    by_name = {f["name"]: f for f in all_files}
    images = [f for f in all_files if Path(f["name"]).suffix.lower() in Export.IMAGE_EXTENSIONS]
    if not images:
        sys.exit("No images found in that folder.")
    if args.test:
        images = [random.choice(images)]

    output_id = get_or_create_folder(service, shoot["id"], Drive.OUTPUT_SUBFOLDER_NAME)
    existing_outputs = {f["name"] for f in list_children(service, output_id, folders=False)}
    print(f"{len(images)} image(s) to process, {len(existing_outputs)} already in '{Drive.OUTPUT_SUBFOLDER_NAME}'.")

    if args.keep_local:
        download_dir, export_dir = Paths.DOWNLOAD_DIR, Paths.EXPORT_DIR
        temp_root = None
    else:
        temp_root = Path(tempfile.mkdtemp(prefix="rv-darktable-"))
        download_dir, export_dir = temp_root / "downloads", temp_root / "exports"
    download_dir.mkdir(parents=True, exist_ok=True)
    export_dir.mkdir(parents=True, exist_ok=True)
    config_dir = Path(Darktable.CONFIG_DIR) if Darktable.CONFIG_DIR else (temp_root or Paths.WORK_DIR) / "dt-config"

    done = skipped = failed = 0
    try:
        for f in images:
            out_name = f"{Path(f['name']).stem}.{out_ext}"
            if out_name in existing_outputs and not args.force and not args.test:
                print(f"skip  {f['name']} (already edited)")
                skipped += 1
                continue

            src = download_dir / f["name"]
            dest = export_dir / out_name
            if dest.exists():
                dest.unlink()  # darktable-cli appends _01 instead of overwriting

            try:
                print(f"get   {f['name']}")
                download(service, f["id"], src)

                xmp = None
                sidecar = by_name.get(f"{f['name']}.xmp") or by_name.get(f"{Path(f['name']).stem}.xmp")
                if sidecar:
                    xmp = download_dir / f"{f['name']}.xmp"
                    download(service, sidecar["id"], xmp)

                print(f"edit  {f['name']}" + (f" (style: {args.style})" if args.style else "") + (" (+xmp)" if xmp else ""))
                run_darktable(src, xmp, dest, args.style, config_dir)

                print(f"put   {out_name} -> {Drive.OUTPUT_SUBFOLDER_NAME}/")
                upload(service, dest, output_id, out_mime)
                done += 1
            except Exception as e:  # keep going on a bad file
                print(f"FAIL  {f['name']}: {e}", file=sys.stderr)
                failed += 1
    finally:
        if temp_root:
            shutil.rmtree(temp_root, ignore_errors=True)

    print(f"\nDone: {done} uploaded, {skipped} skipped, {failed} failed.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
