"""
Keeps every shoot's "Auto Edited" folder up to date. schedule_sweep.ps1 runs it every hour while the
laptop is on; a run that finds nothing new takes a few seconds.

A shoot is (re)processed by drive_darktable.process_shoot when, since the last run:
  - photos were added to it,
  - hand edits of it were added to an "EDITED ..." folder (its look is re-learned), or
  - the darktable style named after it was created or changed.
Drive changes come from the Drive Changes API, so nothing is re-listed unless something changed.
Shoots with photos that failed stay queued and are retried next run. Shoots still waiting for hand edits
are dropped from the queue; adding the edits queues them again.

The first run only starts watching Drive from now on, and queues the newest shoot.

Usage:
    python sweep.py              # check now
    python sweep.py --dry-run    # show what would be processed; changes nothing
    pythonw sweep.py --log       # what the scheduled task runs (output goes to work/sweep.log)
"""

import argparse
import hashlib
import json
import msvcrt
import sys
from datetime import datetime
from pathlib import Path

import dt_styles
from build_look import find_pairs
from constants import Darktable, Drive, Export, Look, Paths, Sweep
from drive import folder_id_from, get_drive_service, is_edit_folder, list_children, newest_shoot_folder
from drive_darktable import build_parser, check_setup, process_shoot


def take_lock():
    """An open, locked file while this sweep runs (Windows releases it if the process dies), or None."""
    Sweep.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = open(Sweep.LOCK_FILE, "w")
    try:
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        fh.close()
        return None
    return fh


def load_state() -> dict:
    return json.loads(Sweep.STATE_FILE.read_text()) if Sweep.STATE_FILE.exists() else {}


def save_state(state: dict) -> None:
    tmp = Sweep.STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(Sweep.STATE_FILE)  # never leave a half-written state file if the laptop dies mid-write


def drive_changes(service, token: str) -> tuple[list[dict], str]:
    """Files added or changed anywhere in Drive since `token`, and the token for next time."""
    files = []
    while True:
        resp = service.changes().list(
            pageToken=token,
            fields="nextPageToken, newStartPageToken, changes(file(id, name, parents, trashed))",
            pageSize=1000,
            includeRemoved=False,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files += [c["file"] for c in resp.get("changes", []) if c.get("file") and not c["file"].get("trashed")]
        if "newStartPageToken" in resp:
            return files, resp["newStartPageToken"]
        token = resp["nextPageToken"]


def folder_map(service, root_id: str) -> tuple[dict[str, dict], set[str]]:
    """Raw shoot folders (id -> folder), and ids of "EDITED ..." folders, in the root or inside a shoot."""
    shoots, edit_folders = {}, set()
    for top in list_children(service, root_id, folders=True):
        if is_edit_folder(top["name"]):
            edit_folders.add(top["id"])
            continue
        shoots[top["id"]] = top
        for sub in list_children(service, top["id"], folders=True):
            if sub["name"] != Drive.OUTPUT_SUBFOLDER_NAME and is_edit_folder(sub["name"]):
                edit_folders.add(sub["id"])
    return shoots, edit_folders


def changed_styles(state: dict) -> list[str]:
    """darktable styles created or edited since the last run (none on the first run)."""
    config_dir = Path(Darktable.CONFIG_DIR) if Darktable.CONFIG_DIR else Paths.WORK_DIR / "dt-config"
    hashes = {s: dt_styles.style_hash(config_dir, s) for s in dt_styles.prepare_config(config_dir)}
    # LUTs you dropped in styles/luts/ count too: "Nov 1st Game.cube" matches like a style named that.
    luts = {p.stem: hashlib.sha1(p.read_bytes()).hexdigest()[:12] for p in Look.USER_LUTS_DIR.glob("*.cube")}
    before, before_luts = state.get("styles"), state.get("luts")
    state["styles"], state["luts"] = hashes, luts
    changed = [] if before is None else [s for s, h in hashes.items() if before.get(s) != h]
    if before_luts is not None:
        changed += [s for s, h in luts.items() if before_luts.get(s) != h]
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description="Process every shoot with new photos, hand edits or style changes.")
    parser.add_argument("--dry-run", action="store_true", help="Only show which shoots would be processed")
    parser.add_argument("--log", action="store_true", help=f"Append output to {Sweep.LOG_FILE} (for the scheduled task)")
    args = parser.parse_args()

    if args.log:
        Sweep.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(Sweep.LOG_FILE, "a", encoding="utf-8", buffering=1)
    print(f"\n=== sweep {datetime.now():%Y-%m-%d %H:%M} ===")

    if not Drive.ROOT_FOLDER_ID:
        sys.exit("Set DRIVE_ROOT_FOLDER_ID in .env (copy .env.example).")
    check_setup()
    lock = take_lock()
    if not lock:
        print("Another sweep is still running; skipping this one.")
        return

    service = get_drive_service()
    root_id = folder_id_from(Drive.ROOT_FOLDER_ID)
    state = load_state()
    queued = set(state.get("pending", []))
    styles = changed_styles(state)

    if "page_token" in state:
        changes, next_token = drive_changes(service, state["page_token"])
    else:
        changes = []
        next_token = service.changes().getStartPageToken(supportsAllDrives=True).execute()["startPageToken"]
        queued.add(newest_shoot_folder(service, root_id)["id"])
        print("First run: watching Drive for changes from now on, and queuing the newest shoot.")
    changes = [f for f in changes if Path(f["name"]).suffix.lower() in Export.IMAGE_EXTENSIONS]

    if not (changes or styles or queued):
        print("Nothing new.")
        state["page_token"] = next_token
        if not args.dry_run:
            save_state(state)
        return

    shoots, edit_folders = folder_map(service, root_id)
    new_edit_ids = set()
    for f in changes:
        for parent in f.get("parents", []):
            if parent in shoots:
                queued.add(parent)
            elif parent in edit_folders:
                new_edit_ids.add(f["id"])
    queued |= {sid for sid, shoot in shoots.items() if dt_styles.find_style(styles, shoot["name"])}
    pairs = find_pairs(service) if queued or new_edit_ids else None  # also reused by process_shoot
    if new_edit_ids:
        queued |= {p["raw_folder"]["id"] for p in pairs if p["edited"]["id"] in new_edit_ids}
    queued &= shoots.keys()  # drops raw subfolders and shoots that were deleted

    todo = sorted(queued, key=lambda sid: shoots[sid]["createdTime"])  # oldest shoot first
    print(f"{len(changes)} Drive change(s), {len(styles)} style/LUT change(s). "
          f"Shoots to process: {', '.join(shoots[sid]['name'] for sid in todo) or 'none'}")
    if args.dry_run:
        return

    # Save before processing so a crash or shutdown mid-shoot doesn't lose what's queued.
    state["page_token"], state["pending"] = next_token, todo
    save_state(state)

    run_args = build_parser().parse_args([])
    for sid in todo:
        print()
        try:
            result = process_shoot(service, shoots[sid], run_args, pairs)
        except Exception as e:  # one broken shoot shouldn't stop the others
            print(f"FAIL  shoot '{shoots[sid]['name']}': {e}", file=sys.stderr)
            result = "failed"
        if result != "failed":
            queued.discard(sid)
        state["pending"] = sorted(queued, key=lambda i: shoots[i]["createdTime"])
        save_state(state)


if __name__ == "__main__":
    main()
