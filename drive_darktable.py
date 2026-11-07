"""
Google Drive -> darktable -> Google Drive pipeline.

1. Finds the newest raw shoot folder inside Drive.ROOT_FOLDER_ID (folders with "edit" in the name are
   finished edits and are skipped), or the one given with --folder
2. Learns that shoot's look from the photos of it you edited by hand (build_look.py); needs
   Look.MIN_EDITS_PER_SHOOT edits in an "EDITED ..." folder, otherwise it waits (or use --fallback)
3. Downloads each raw you haven't edited yourself (plus its .xmp sidecar if one sits next to it)
4. Runs darktable-cli on it, then applies the look: exposure matching + the shoot's LUT
5. Uploads the result to an "Auto Edited" subfolder inside that shoot folder

Images that already have an output are skipped, so the script is safe to re-run. Each output is tagged with
the style/look that made it; when that changes (you edited the style, or added hand edits), outputs made the
old way are redone. sweep.py runs this on a schedule for every shoot that changed.
All paths and settings live in constants.py.

Usage:
    python drive_darktable.py --test          # one random image from the newest folder
    python drive_darktable.py                 # every image in the newest folder
    python drive_darktable.py --folder <id>   # a specific shoot folder
"""

import argparse
import hashlib
import random
import shutil
import sys
import tempfile
from pathlib import Path

import dt_styles
import look
from build_look import all_shoots_look, find_pairs, look_for_shoot
from constants import Darktable, Drive, Export, Look, Paths, Sweep
from drive import (download, folder_id_from, get_drive_service, get_or_create_folder, is_edit_folder,
                   list_children, newest_shoot_folder, replace, upload)


def run_darktable(src: Path, xmp: Path | None, dest: Path, style: str | None, config_dir: Path,
                  rv_look: "look.RVLook | None" = None) -> None:
    # With a look, darktable exports a 16-bit TIFF and the look is applied to that before saving dest.
    out = dest.with_suffix(".tif") if rv_look else dest
    dt_styles.export(src, out, style, config_dir, xmp=xmp, tiff16=bool(rv_look))
    if rv_look:
        look.write_jpeg(rv_look.apply(look.read_image(out)), dest)
        out.unlink()


def edit_tag(config_dir: Path, style: str | None, rv_look: "look.RVLook | None") -> str:
    """Short ID for how outputs are made; changes when the style's settings or the learned look change."""
    h = hashlib.sha1(dt_styles.style_hash(config_dir, style).encode() if style else b"no style")
    if rv_look:
        h.update(rv_look.lut.tobytes())
        h.update(str(rv_look.exposure_target).encode())
    return h.hexdigest()[:12]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Edit Drive photos with darktable and upload the results.")
    parser.add_argument("--folder", help="Shoot folder ID/URL to process (default: newest raw folder in the root)")
    parser.add_argument("--test", action="store_true", help="Process one random image only, to check the setup")
    parser.add_argument("--style", help="darktable style to apply (default: the style named after the shoot, "
                                        "else DARKTABLE_STYLE)")
    parser.add_argument("--force", action="store_true", help="Re-process images that already have an output")
    parser.add_argument("--keep-local", action="store_true", help=f"Keep downloaded/exported files in {Paths.WORK_DIR}")
    parser.add_argument("--no-look", action="store_true", help="Skip the learned look (plain darktable export)")
    parser.add_argument("--fallback", action="store_true",
                        help="If this shoot doesn't have enough hand edits yet, use the all-shoots look instead of waiting")
    return parser


def check_setup() -> None:
    if not Path(Darktable.CLI).exists() and not shutil.which(Darktable.CLI):
        sys.exit(f"darktable-cli not found at '{Darktable.CLI}'. Install darktable or set DARKTABLE_CLI.")


def process_shoot(service, shoot: dict, args: argparse.Namespace, pairs: list[dict] | None = None) -> str:
    """
    Edits every photo of one shoot that needs it. Returns "done", "waiting" (no style and not enough
    hand edits yet) or "failed" (some photos failed; running again retries just those).
    """
    print(f"Shoot folder: {shoot['name']} ({shoot['id']})")

    config_dir = Path(Darktable.CONFIG_DIR) if Darktable.CONFIG_DIR else Paths.WORK_DIR / "dt-config"
    style = args.style
    if not style:
        style = dt_styles.style_for_shoot(dt_styles.prepare_config(config_dir), shoot["name"])
    elif not Darktable.CONFIG_DIR:
        dt_styles.prepare_config(config_dir)

    user_lut = None if args.no_look else look.user_lut_for(shoot["name"])
    if pairs is None and (style or user_lut or (not args.no_look and Look.ENABLED)):
        pairs = find_pairs(service)
    rv_look, hand_edited = None, set()
    if style or user_lut:
        # Your own darktable style and/or LUT does the edit; the learned look is only for shoots without one.
        if style:
            print(f"darktable style: {style}")
        if user_lut:
            print(f"Your LUT: {user_lut.meta['source']}")
        rv_look = user_lut
        hand_edited = {p["raw"]["id"] for p in pairs if p["raw_folder"]["id"] == shoot["id"]}
    elif not args.no_look and Look.ENABLED:
        rv_look, hand_edited = look_for_shoot(service, shoot, pairs)
        if rv_look:
            print(f"Look: learned from {rv_look.meta.get('edits_used')} of your edits of this shoot")
        elif args.fallback:
            rv_look = all_shoots_look(service, pairs)
            print("Look: all-shoots fallback (edit a few photos of this shoot for a closer match)")
        else:
            print(f"Waiting: no darktable style named '{shoot['name']}', and you've edited {len(hand_edited)} "
                  f"photo(s) of this shoot (the bot needs {Look.MIN_EDITS_PER_SHOOT} in an 'EDITED ...' folder "
                  "to learn its look). Create the style, run with --fallback, or --no-look for a plain export.")
            return "waiting"
    if not rv_look and not style:
        print("Look: none (plain darktable export)")
    out_ext = "jpg" if rv_look else Export.FORMAT.lower().lstrip(".")
    out_mime = Export.MIME_TYPES[out_ext]
    tag = edit_tag(config_dir, style, rv_look)

    all_files = list_children(service, shoot["id"], folders=False)
    by_name = {f["name"]: f for f in all_files}
    images = [f for f in all_files
              if Path(f["name"]).suffix.lower() in Export.IMAGE_EXTENSIONS and f["id"] not in hand_edited]
    if hand_edited:
        print(f"Skipping {len(hand_edited)} photo(s) you already edited by hand.")
    if not images:
        print("No images to process in that folder.")
        return "done"
    if args.test:
        images = [random.choice(images)]

    output_id = get_or_create_folder(service, shoot["id"], Drive.OUTPUT_SUBFOLDER_NAME)
    existing_outputs = {f["name"]: f for f in list_children(service, output_id, folders=False)}
    print(f"{len(images)} image(s) to process, {len(existing_outputs)} already in '{Drive.OUTPUT_SUBFOLDER_NAME}'.")

    if args.keep_local:
        download_dir, export_dir = Paths.DOWNLOAD_DIR, Paths.EXPORT_DIR
        temp_root = None
    else:
        temp_root = Path(tempfile.mkdtemp(prefix="rv-darktable-"))
        download_dir, export_dir = temp_root / "downloads", temp_root / "exports"
    download_dir.mkdir(parents=True, exist_ok=True)
    export_dir.mkdir(parents=True, exist_ok=True)

    done = redone = skipped = failed = 0
    try:
        for f in images:
            out_name = f"{Path(f['name']).stem}.{out_ext}"
            existing = existing_outputs.get(out_name)
            # Outputs uploaded before tagging existed count as current, so they aren't all redone at once.
            existing_tag = (existing or {}).get("appProperties", {}).get(Sweep.EDIT_TAG_KEY, tag)
            if existing and existing_tag == tag and not args.force and not args.test:
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

                print(f"edit  {f['name']}" + (f" (style: {style})" if style else "") + (" (+xmp)" if xmp else ""))
                run_darktable(src, xmp, dest, style, config_dir, rv_look)

                properties = {Sweep.EDIT_TAG_KEY: tag}
                if existing:
                    print(f"redo  {out_name} -> {Drive.OUTPUT_SUBFOLDER_NAME}/")
                    replace(service, existing["id"], dest, out_mime, properties)
                    redone += 1
                else:
                    print(f"put   {out_name} -> {Drive.OUTPUT_SUBFOLDER_NAME}/")
                    upload(service, dest, output_id, out_mime, properties)
                    done += 1
            except Exception as e:  # keep going on a bad file
                print(f"FAIL  {f['name']}: {e}", file=sys.stderr)
                failed += 1
            finally:
                if not args.keep_local:
                    src.unlink(missing_ok=True)  # raws are ~30 MB; don't let a 700-photo shoot fill the disk
    finally:
        if temp_root:
            shutil.rmtree(temp_root, ignore_errors=True)

    print(f"\nDone: {done} uploaded, {redone} redone (style/look changed), {skipped} skipped, {failed} failed.")
    return "failed" if failed else "done"


def main() -> None:
    args = build_parser().parse_args()

    if not args.folder and not Drive.ROOT_FOLDER_ID:
        sys.exit("Set DRIVE_ROOT_FOLDER_ID in .env (copy .env.example) or pass --folder.")
    check_setup()

    service = get_drive_service()

    if args.folder:
        shoot = service.files().get(fileId=folder_id_from(args.folder), fields="id, name, createdTime",
                                    supportsAllDrives=True).execute()
        if is_edit_folder(shoot["name"]):
            sys.exit(f"'{shoot['name']}' holds finished edits, not raws; pass the raw shoot folder instead.")
    else:
        shoot = newest_shoot_folder(service, folder_id_from(Drive.ROOT_FOLDER_ID))
    sys.exit(1 if process_shoot(service, shoot, args) == "failed" else 0)


if __name__ == "__main__":
    main()
