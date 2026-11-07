"""
Learns the Real Victors look for each shoot from the photos you edited by hand.

1. Finds raw + edited pairs in Drive by file name: "Nov 1st Game/4A8A0754.CR3" pairs with
   "EDITED NOV 1 part 2/4A8A0754.JPG". Edit folders are any folder with "edit" in the name.
2. Renders each raw with darktable's defaults (exactly what the pipeline starts from).
3. Lines your edit up with the render (handles crops), evens out exposure, and samples colors.
4. Fits a 3D LUT "darktable default -> your edit" per shoot, saved as styles/looks/<shoot>.cube + .json.
5. Tests it on held-out photos it never trained on, and writes side-by-sides to work/compare/<shoot>/.

drive_darktable.py calls look_for_shoot() itself, so you only need to run this by hand to
check or rebuild looks. Downloads are cached in work/training.

Usage:
    python build_look.py                      # every shoot with enough edits + the all-shoots fallback
    python build_look.py --shoot "Nov 1st Game"
"""

import argparse
import re
import subprocess
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import cv2
import numpy as np

import look
from constants import Darktable, Drive, Export, Look, Training
from drive import download, folder_id_from, get_drive_service, is_edit_folder, list_children, walk_folders

EDIT_SUFFIX = re.compile(r"(_\d{1,2}| \(\d+\)|-edit(-\d+)?)$", re.IGNORECASE)  # IMG_5967_1, 4A8A0754-Edit
EDITED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}


# --- Finding pairs ----------------------------------------------------------

def find_pairs(service) -> list[dict]:
    """Every (raw, your edit) pair in Drive. Each pair: raw, raw_folder, edited, edit_folder (Drive file dicts)."""
    raws = defaultdict(list)    # stem -> [(raw file, folder)]
    edits = []                  # (edited file, folder)
    for folder in walk_folders(service, folder_id_from(Drive.ROOT_FOLDER_ID)):
        if folder["name"] == Drive.OUTPUT_SUBFOLDER_NAME:
            continue  # the bot's own output is not something to learn from
        for f in list_children(service, folder["id"], folders=False):
            ext = Path(f["name"]).suffix.lower()
            if is_edit_folder(folder["name"]) and ext in EDITED_EXTENSIONS:
                edits.append((f, folder))
            elif not is_edit_folder(folder["name"]) and ext in Export.RAW_EXTENSIONS:
                raws[Path(f["name"]).stem.upper()].append((f, folder))

    pairs, used_raws = [], set()
    for edited, edit_folder in sorted(edits, key=lambda e: e[0]["name"]):
        stem = Path(edited["name"]).stem.upper()
        candidates = raws.get(stem) or raws.get(EDIT_SUFFIX.sub("", stem))
        if not candidates:
            continue
        # Same file number can exist in several shoots: take the latest raw folder made before the edit folder.
        before = [c for c in candidates if c[1]["createdTime"] <= edit_folder["createdTime"]]
        raw, raw_folder = max(before or candidates, key=lambda c: c[1]["createdTime"])
        if raw["id"] in used_raws:  # several edits of one raw (virtual copies): keep the first
            continue
        used_raws.add(raw["id"])
        pairs.append({"raw": raw, "raw_folder": raw_folder, "edited": edited, "edit_folder": edit_folder})
    return pairs


def spread(pairs: list[dict], limit: int) -> list[dict]:
    """Up to `limit` pairs, taken round-robin across edit folders so every shoot is represented."""
    queues = defaultdict(list)
    for p in pairs:
        queues[p["edit_folder"]["id"]].append(p)
    picked, queues = [], list(queues.values())
    while queues and len(picked) < limit:
        for q in list(queues):
            if len(picked) < limit:
                picked.append(q.pop(0))
            if not q:
                queues.remove(q)
    return picked


# --- Preparing one pair -----------------------------------------------------

def cached_download(service, file: dict, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{file['id'][:10]}_{file['name']}"
    if not dest.exists():
        tmp = dest.with_suffix(dest.suffix + ".part")
        download(service, file["id"], tmp)
        tmp.rename(dest)
    return dest


def render_default(raw: Path, dest: Path, config_dir: Path) -> Path:
    """darktable's default rendering of the raw as a small 16-bit TIFF."""
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [Darktable.CLI, str(raw), dest.as_posix(),
           "--width", str(Training.RENDER_SIZE), "--height", str(Training.RENDER_SIZE),
           "--core", "--configdir", str(config_dir), "--conf", "plugins/imageio/format/tiff/bpp=16"]
    result = subprocess.run(cmd, capture_output=True, text=True, creationflags=Darktable.NO_WINDOW)
    if not dest.exists():
        raise RuntimeError(f"darktable-cli failed on {raw.name}:\n{result.stderr or result.stdout}")
    return dest


def align(render: np.ndarray, edited: np.ndarray) -> tuple[np.ndarray, np.ndarray, float] | None:
    """Finds where the (possibly cropped) edit sits inside the render; returns both cut to the same area."""
    gray = lambda img: cv2.cvtColor((img * 255).astype(np.uint8), cv2.COLOR_RGB2GRAY)
    small = 0.5
    r = cv2.resize(gray(render), None, fx=small, fy=small, interpolation=cv2.INTER_AREA)
    e = gray(edited)
    best = (-1.0, 1.0, (0, 0))
    for frac in np.arange(0.5, 1.0001, 0.02):
        w = int(r.shape[1] * frac)
        h = int(e.shape[0] * w / e.shape[1])
        if h > r.shape[0] or h < 16:
            continue
        template = cv2.resize(e, (w, h), interpolation=cv2.INTER_AREA)
        _, score, _, loc = cv2.minMaxLoc(cv2.matchTemplate(r, template, cv2.TM_CCOEFF_NORMED))
        if score > best[0]:
            best = (score, frac, loc)
    score, frac, (x, y) = best
    if score < Training.MIN_MATCH_SCORE:
        return None
    w = int(render.shape[1] * frac)
    h = int(edited.shape[0] * w / edited.shape[1])
    x, y = int(x / small), int(y / small)
    crop = render[y:y + h, x:x + w]
    return crop, cv2.resize(edited, (crop.shape[1], crop.shape[0]), interpolation=cv2.INTER_AREA), score


def prepare(service, pair: dict, label: str) -> dict | None:
    """Downloads, renders and aligns one pair. None if it can't be used."""
    name = pair["edited"]["name"]
    try:
        raw = cached_download(service, pair["raw"], Training.DIR / "raw")
        edited_path = cached_download(service, pair["edited"], Training.DIR / "edited")
        render_path = render_default(raw, Training.DIR / "render" / f"{raw.stem}.tif", Training.DIR / "dt-config")
        aligned = align(look.read_image(render_path), look.read_image(edited_path))
    except Exception as e:
        print(f"  {label} FAIL {name}: {e}")
        return None
    if aligned is None:
        print(f"  {label} skip {name}: edit doesn't line up with the raw (rotated or heavily cropped?)")
        return None
    render, edited, score = aligned
    print(f"  {label} ok   {name}  (match {score:.2f})")
    return {**pair, "render": render, "edited_img": edited, "median": look.median_luminance(render)}


# --- Fitting and testing ----------------------------------------------------

def sample_colors(src: np.ndarray, dst: np.ndarray, rng) -> tuple[np.ndarray, np.ndarray]:
    """Random pixels from flat areas, at low res, so small misalignments don't teach the LUT wrong colors."""
    size = (Training.SAMPLE_WIDTH, int(Training.SAMPLE_WIDTH * src.shape[0] / src.shape[1]))
    src = cv2.GaussianBlur(cv2.resize(src, size, interpolation=cv2.INTER_AREA), (3, 3), 0)
    dst = cv2.GaussianBlur(cv2.resize(dst, size, interpolation=cv2.INTER_AREA), (3, 3), 0)
    g = cv2.cvtColor(src, cv2.COLOR_RGB2GRAY)
    edges = np.hypot(cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1))
    flat = np.flatnonzero(edges <= np.percentile(edges, Training.FLAT_PERCENTILE))
    idx = rng.choice(flat, size=min(Training.SAMPLES_PER_PAIR, flat.size), replace=False)
    return src.reshape(-1, 3)[idx], dst.reshape(-1, 3)[idx]


def fit(prepared: list[dict]) -> look.RVLook:
    # The brightness every photo is moved toward: the typical brightness of these renders.
    target = float(np.exp(np.mean([np.log(p["median"]) for p in prepared])))
    rng = np.random.default_rng(0)
    xs, ys = [], []
    for p in prepared:
        normalized = look.apply_exposure(p["render"], look.exposure_gain(p["render"], target))
        x, y = sample_colors(normalized, p["edited_img"], rng)
        xs.append(x)
        ys.append(y)
    lut = look.fit_lut(np.concatenate(xs), np.concatenate(ys), Training.FIT_LUT_SIZE, Training.SMOOTHNESS)
    return look.RVLook(look.resample_lut(lut, Training.CUBE_SIZE), target)


def delta_e(a: np.ndarray, b: np.ndarray) -> float:
    """Average color difference (CIE76). Roughly: <2 hard to see, 2-5 noticeable side by side, >5 obvious."""
    lab = lambda img: cv2.cvtColor(np.clip(img, 0, 1).astype(np.float32), cv2.COLOR_RGB2Lab)
    return float(np.linalg.norm(lab(a) - lab(b), axis=-1).mean())


def side_by_side(images: list[np.ndarray], labels: list[str], path: Path, height: int = 420) -> None:
    tiles = []
    for img, label in zip(images, labels):
        tile = cv2.resize(img, (int(img.shape[1] * height / img.shape[0]), height), interpolation=cv2.INTER_AREA)
        tile = (np.clip(tile, 0, 1) * 255).astype(np.uint8).copy()
        cv2.rectangle(tile, (0, 0), (tile.shape[1], 30), (0, 0, 0), -1)
        cv2.putText(tile, label, (8, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)
        tiles.append(tile)
    path.parent.mkdir(parents=True, exist_ok=True)
    look.write_jpeg(np.hstack(tiles).astype(np.float32) / 255, path, 90)


def train(prepared: list[dict], name: str) -> look.RVLook:
    """
    Tests on held-out photos first (so the score is honest), writes side-by-sides,
    then fits the saved look on all photos.
    """
    test = prepared[2::Training.HOLDOUT_EVERY] if len(prepared) >= 8 else []
    stats = {}
    if test:
        test_ids = {id(p) for p in test}
        trial = fit([p for p in prepared if id(p) not in test_ids])
        before = [delta_e(p["render"], p["edited_img"]) for p in test]
        after = [delta_e(trial.apply(p["render"]), p["edited_img"]) for p in test]
        print(f"  test on {len(test)} held-out photos (color difference from your edit, lower is better): "
              f"darktable default {np.mean(before):.1f} -> auto look {np.mean(after):.1f}")
        for p in test:
            side_by_side([p["render"], trial.apply(p["render"]), p["edited_img"]],
                         ["darktable default", "auto look", "your edit"],
                         Training.COMPARE_DIR / look.look_paths(name)[0].stem / f"{Path(p['edited']['name']).stem}.jpg")
        stats = {"test_photos": len(test), "test_delta_e_default": round(float(np.mean(before)), 2),
                 "test_delta_e_look": round(float(np.mean(after)), 2)}
    final = fit(prepared)
    final.meta = {
        "built": date.today().isoformat(),
        "edits_used": len(prepared),
        "pair_ids": sorted(p["edited"]["id"] for p in prepared),
        "edit_folders": sorted({p["edit_folder"]["name"] for p in prepared}),
        **stats,
    }
    print(f"  saved {final.save(name)}")
    return final


# --- Entry points -----------------------------------------------------------

def look_for_shoot(service, shoot: dict, pairs: list[dict] | None = None) -> tuple[look.RVLook | None, set[str]]:
    """
    The look for a raw shoot folder, learned from your edits of that shoot (re-learned when you add edits).
    Returns (look or None if there aren't enough edits yet, ids of raws you already edited by hand).
    """
    if pairs is None:
        pairs = find_pairs(service)
    mine = [p for p in pairs if p["raw_folder"]["id"] == shoot["id"]]
    edited_raw_ids = {p["raw"]["id"] for p in mine}
    if len(mine) < Look.MIN_EDITS_PER_SHOOT:
        return None, edited_raw_ids

    mine = spread(mine, Look.MAX_EDITS_PER_SHOOT)
    existing = look.RVLook.load(shoot["name"])
    if existing and existing.meta.get("pair_ids") == sorted(p["edited"]["id"] for p in mine):
        return existing, edited_raw_ids

    print(f"Learning the look for '{shoot['name']}' from {len(mine)} of your edits...")
    prepared = [r for i, p in enumerate(mine, 1) if (r := prepare(service, p, f"[{i}/{len(mine)}]"))]
    if len(prepared) < Look.MIN_EDITS_PER_SHOOT:
        print(f"  only {len(prepared)} usable edits; need {Look.MIN_EDITS_PER_SHOOT}.")
        return None, edited_raw_ids
    return train(prepared, shoot["name"]), edited_raw_ids


def all_shoots_look(service, pairs: list[dict] | None = None) -> look.RVLook | None:
    """Fallback look learned across every shoot (less accurate than a shoot's own look)."""
    existing = look.RVLook.load(Look.ALL_SHOOTS_NAME)
    if existing:
        return existing
    pairs = spread(pairs if pairs is not None else find_pairs(service), Training.ALL_SHOOTS_MAX_PAIRS)
    print(f"Learning the all-shoots fallback look from {len(pairs)} edits...")
    prepared = [r for i, p in enumerate(pairs, 1) if (r := prepare(service, p, f"[{i}/{len(pairs)}]"))]
    return train(prepared, Look.ALL_SHOOTS_NAME) if len(prepared) >= Look.MIN_EDITS_PER_SHOOT else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Learn per-shoot looks from raw + edited pairs in Drive.")
    parser.add_argument("--shoot", help="Only this raw shoot folder (by name)")
    parser.add_argument("--rebuild", action="store_true", help="Re-learn looks even if nothing changed")
    args = parser.parse_args()
    if not Drive.ROOT_FOLDER_ID:
        sys.exit("Set DRIVE_ROOT_FOLDER_ID in .env first.")

    service = get_drive_service()
    print("Finding raw + edited pairs in Drive...")
    pairs = find_pairs(service)
    shoots = {}
    for p in pairs:
        shoots.setdefault(p["raw_folder"]["id"], {"shoot": p["raw_folder"], "count": 0})["count"] += 1
    for s in sorted(shoots.values(), key=lambda s: s["shoot"]["createdTime"]):
        print(f"  {s['count']:4d} edits  {s['shoot']['name']}")

    if args.rebuild:
        for f in Look.LOOKS_DIR.glob("*.json"):
            f.unlink()
    for s in shoots.values():
        if args.shoot and s["shoot"]["name"].strip() != args.shoot.strip():
            continue
        if s["count"] < Look.MIN_EDITS_PER_SHOOT:
            print(f"\n'{s['shoot']['name']}': only {s['count']} edits, skipping (needs {Look.MIN_EDITS_PER_SHOOT}).")
            continue
        print()
        lk, _ = look_for_shoot(service, s["shoot"], pairs)
        if lk:
            print(f"  '{s['shoot']['name']}': look ready ({lk.meta.get('edits_used')} edits)")
    if not args.shoot:
        print()
        all_shoots_look(service, pairs)
    print(f"\nLooks: {Look.LOOKS_DIR}\nSide-by-sides: {Training.COMPARE_DIR}")


if __name__ == "__main__":
    main()
