"""
Google Drive helpers shared by drive_darktable.py and build_look.py.
"""

import io
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from constants import Drive, Paths


def folder_id_from(value: str) -> str:
    """Accepts either a raw folder ID or a full Drive folder URL."""
    value = value.strip()
    if "/folders/" in value:
        value = value.split("/folders/")[1]
    return value.split("?")[0].split("/")[0]


def is_edit_folder(name: str) -> bool:
    """Folders with "edit" in the name hold finished edits (yours, or the bot's "Auto Edited")."""
    return Drive.EDITED_FOLDER_KEYWORD in name.lower()


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
            fields="nextPageToken, files(id, name, mimeType, createdTime, appProperties)",
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


def walk_folders(service, folder_id: str, depth: int = 2) -> list[dict]:
    """All folders under folder_id, down to `depth` levels (shoot folders and their subfolders)."""
    found = []
    for f in list_children(service, folder_id, folders=True):
        found.append(f)
        if depth > 1:
            found.extend(walk_folders(service, f["id"], depth - 1))
    return found


def newest_shoot_folder(service, root_id: str) -> dict:
    subfolders = [f for f in list_children(service, root_id, folders=True) if not is_edit_folder(f["name"])]
    if not subfolders:
        sys.exit(f"No raw shoot folders found in Drive folder {root_id}.")
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


def upload(service, src: Path, folder_id: str, mime_type: str, properties: dict | None = None) -> str:
    media = MediaFileUpload(str(src), mimetype=mime_type, resumable=True)
    created = service.files().create(
        body={"name": src.name, "parents": [folder_id], "appProperties": properties or {}},
        media_body=media,
        fields="id",
        supportsAllDrives=True,
    ).execute()
    return created["id"]


def replace(service, file_id: str, src: Path, mime_type: str, properties: dict | None = None) -> None:
    """Uploads src as the new content of an existing file (keeps its ID and any share links)."""
    media = MediaFileUpload(str(src), mimetype=mime_type, resumable=True)
    service.files().update(
        fileId=file_id,
        body={"appProperties": properties or {}},
        media_body=media,
        supportsAllDrives=True,
    ).execute()
