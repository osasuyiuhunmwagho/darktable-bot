"""
darktable styles for darktable-cli.

darktable-cli only applies styles that are stored in the data.db of the config dir it runs with; it
ignores .dtstyle files lying around. So before exporting, prepare_config():
  1. copies data.db from your normal darktable config (every style you created in the darktable app), and
  2. imports every .dtstyle file in styles/ (styles exported from darktable, e.g. from another computer).

A shoot uses the style named like its Drive folder ("Nov 1st Game" or "RV Nov 1st Game", case and
punctuation don't matter); otherwise Darktable.STYLE (DARKTABLE_STYLE in .env); otherwise none.

Try a style on local files (nothing is uploaded):
    python dt_styles.py                                              # list your styles
    python dt_styles.py --try images/uneditedphoto.CR3 --style "Nov 1st Game"
"""

import argparse
import base64
import hashlib
import re
import sqlite3
import subprocess
import sys
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

from constants import Darktable, Export, Paths

BUILTIN_PREFIX = "_l10n_"  # darktable's own camera/example styles


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _decode(value: str | None) -> bytes:
    """Params in .dtstyle/.xmp files are hex, or 'gz' + 2 digits + base64(zlib)."""
    value = (value or "").strip()
    if value.startswith("gz"):
        return zlib.decompress(base64.b64decode(value[4:]))
    return bytes.fromhex(value)


def read_dtstyle(path: Path) -> tuple[str, str, list[tuple]]:
    root = ET.parse(path).getroot()
    items = []
    for p in root.iter("plugin"):
        get = lambda key, default="0": p.findtext(key) or default
        items.append((int(get("num")), int(get("module")), get("operation", ""), _decode(p.findtext("op_params")),
                      int(get("enabled", "1")), _decode(p.findtext("blendop_params")), int(get("blendop_version")),
                      int(get("multi_priority")), p.findtext("multi_name") or "", int(get("multi_name_hand_edited"))))
    return root.findtext("info/name") or path.stem, root.findtext("info/description") or "", items


def _import_dtstyle(db: sqlite3.Connection, path: Path) -> str:
    name, description, items = read_dtstyle(path)
    for (sid,) in db.execute("SELECT id FROM styles WHERE name = ?", (name,)).fetchall():  # file wins
        db.execute("DELETE FROM style_items WHERE styleid = ?", (sid,))
        db.execute("DELETE FROM styles WHERE id = ?", (sid,))
    sid = db.execute("INSERT INTO styles (name, description) VALUES (?, ?)", (name, description)).lastrowid
    db.executemany("INSERT INTO style_items (styleid, num, module, operation, op_params, enabled, blendop_params, "
                   "blendop_version, multi_priority, multi_name, multi_name_hand_edited) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [(sid, *item) for item in items])
    return name


def _user_styles(db_path: Path) -> list[str]:
    with sqlite3.connect(db_path) as db:
        return [n for (n,) in db.execute("SELECT name FROM styles ORDER BY name") if not n.startswith(BUILTIN_PREFIX)]


def prepare_config(config_dir: Path) -> list[str]:
    """Make config_dir/data.db hold your darktable styles + styles/*.dtstyle. Returns your style names."""
    config_dir.mkdir(parents=True, exist_ok=True)
    db_path = config_dir / "data.db"
    if config_dir.resolve() == Darktable.USER_CONFIG_DIR.resolve():
        return _user_styles(db_path)  # running on your real config: use it as-is, never write to it

    source = Darktable.USER_CONFIG_DIR / "data.db"
    if source.exists():
        # sqlite backup gives a consistent copy even while the darktable app has the file open.
        with sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True) as src, sqlite3.connect(db_path) as dst:
            src.backup(dst)
    elif not db_path.exists():
        sys.exit(f"No darktable data.db at {source}. Open darktable once (or set DARKTABLE_USER_CONFIG_DIR).")

    with sqlite3.connect(db_path) as db:
        for path in sorted(Paths.STYLES_DIR.glob("*.dtstyle")):
            print(f"style file: {path.name} -> '{_import_dtstyle(db, path)}'")
    return _user_styles(db_path)


def find_style(styles: list[str], name: str) -> str | None:
    """The style called `name` or 'RV <name>', ignoring case and punctuation."""
    wanted = {slug(name), slug(f"rv {name}")}
    return next((s for s in styles if s == name or slug(s.split("|")[-1]) in wanted), None)


def style_for_shoot(styles: list[str], shoot_name: str) -> str | None:
    """Your style named after the shoot, else the default style (if it exists), else None."""
    style = find_style(styles, shoot_name)
    if not style and Darktable.STYLE:
        style = find_style(styles, Darktable.STYLE)
        if not style:
            print(f"WARNING: default style '{Darktable.STYLE}' not found in darktable", file=sys.stderr)
    return style


def style_hash(config_dir: Path, name: str) -> str:
    """Fingerprint of a style's modules and settings; changes when you edit the style in darktable."""
    h = hashlib.sha1(name.encode())
    with sqlite3.connect(config_dir / "data.db") as db:
        for row in db.execute("SELECT i.num, i.operation, i.op_params, i.enabled, i.blendop_params, i.multi_priority "
                              "FROM style_items i JOIN styles s ON s.id = i.styleid WHERE s.name = ? ORDER BY i.num",
                              (name,)):
            h.update(repr(row).encode())
    return h.hexdigest()[:12]


def export(src: Path, dest: Path, style: str | None, config_dir: Path, xmp: Path | None = None,
           tiff16: bool = False) -> None:
    """One darktable-cli export. Raises if darktable didn't write dest."""
    cmd = [Darktable.CLI, str(src)]
    if xmp:
        cmd.append(str(xmp))
    cmd.append(dest.as_posix())  # darktable-cli treats "\" in the output pattern as an escape
    if style:
        cmd += ["--style", style]
    if Export.MAX_WIDTH:
        cmd += ["--width", str(Export.MAX_WIDTH)]
    if Export.MAX_HEIGHT:
        cmd += ["--height", str(Export.MAX_HEIGHT)]
    cmd += ["--hq", "true", "--core", "--configdir", str(config_dir)]
    if tiff16:
        cmd += ["--conf", "plugins/imageio/format/tiff/bpp=16"]
    if dest.exists():
        dest.unlink()  # darktable-cli appends _01 instead of overwriting
    result = subprocess.run(cmd, capture_output=True, text=True, creationflags=Darktable.NO_WINDOW)
    # darktable-cli exits 0 even when it can't find the style, so check for the file.
    if result.returncode != 0 or not dest.exists():
        log = (result.stderr or result.stdout).strip().splitlines()
        raise RuntimeError(f"darktable-cli failed ({result.returncode}):\n" + "\n".join(log[-5:]))


def main() -> None:
    parser = argparse.ArgumentParser(description="List darktable styles, or try one on local raws.")
    parser.add_argument("--try", dest="files", nargs="+", type=Path, help="Raw file(s) or folder(s) to export")
    parser.add_argument("--style", help="Style name (default: the DARKTABLE_STYLE setting)")
    args = parser.parse_args()

    config_dir = Path(Darktable.CONFIG_DIR) if Darktable.CONFIG_DIR else Paths.WORK_DIR / "dt-config"
    styles = prepare_config(config_dir)
    print("Your darktable styles:" if styles else "No styles yet: create one in darktable (history stack -> create style).")
    for name in styles:
        print(f"  {name}")
    if not args.files:
        return

    wanted = args.style or Darktable.STYLE
    style = find_style(styles, wanted) if wanted else None
    if wanted and not style:
        sys.exit(f"No style named '{wanted}'.")
    raws = [p for f in args.files for p in (sorted(f.iterdir()) if f.is_dir() else [f])
            if p.suffix.lower() in Export.RAW_EXTENSIONS]
    out_dir = Darktable.STYLE_TEST_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    for raw in raws:
        for label, s in (("default", None), (slug(style) if style else None, style)):
            if label is None:
                continue
            dest = out_dir / f"{raw.stem}_{label}.jpg"
            export(raw, dest, s, config_dir)
            print(f"wrote {dest}")


if __name__ == "__main__":
    main()
