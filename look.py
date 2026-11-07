"""
The Real Victors look: per-photo exposure matching followed by a 3D LUT learned from your own edits.

All images here are float32 RGB in [0, 1], sRGB-encoded, shaped (h, w, 3).
"""

import json
import re
from pathlib import Path

import cv2
import numpy as np
from scipy import sparse
from scipy.sparse.linalg import spsolve

from constants import Look

LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
SHOULDER = 0.8  # linear values above this are rolled off instead of clipped when brightening


# --- Image IO ---------------------------------------------------------------

def read_image(path: Path) -> np.ndarray:
    """Reads an 8- or 16-bit image (darktable TIFF or edited JPG) as float RGB in [0, 1]."""
    if path.suffix.lower() in (".tif", ".tiff"):
        flags = cv2.IMREAD_UNCHANGED | cv2.IMREAD_IGNORE_ORIENTATION  # 16-bit; darktable already rotated it
    else:
        flags = cv2.IMREAD_COLOR  # applies the JPG's EXIF rotation
    img = cv2.imread(str(path), flags)
    if img is None:
        raise ValueError(f"could not read {path}")
    scale = 65535.0 if img.dtype == np.uint16 else 255.0
    return cv2.cvtColor(img[..., :3], cv2.COLOR_BGR2RGB).astype(np.float32) / scale


def write_jpeg(rgb: np.ndarray, path: Path, quality: int = Look.JPEG_QUALITY) -> None:
    bgr = cv2.cvtColor(np.clip(rgb * 255 + 0.5, 0, 255).astype(np.uint8), cv2.COLOR_RGB2BGR)
    if not cv2.imwrite(str(path), bgr, [cv2.IMWRITE_JPEG_QUALITY, quality]):
        raise ValueError(f"could not write {path}")


# --- Exposure ---------------------------------------------------------------

def srgb_to_linear(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0, 1)
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4).astype(np.float32)


def linear_to_srgb(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0, 1)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(x, 1 / 2.4) - 0.055).astype(np.float32)


def median_luminance(rgb: np.ndarray) -> float:
    """Typical brightness of a photo in linear light, ignoring blown highlights and black areas."""
    lum = srgb_to_linear(rgb[::4, ::4]) @ LUMA
    useful = lum[(lum > 0.002) & (lum < 0.98)]
    return float(np.median(useful if useful.size else lum))


def exposure_gain(rgb: np.ndarray, target: float, strength: float = Look.EXPOSURE_STRENGTH) -> float:
    """How much to brighten (>1) or darken (<1) a photo so its typical brightness moves toward the target."""
    gain = (target / max(median_luminance(rgb), 1e-4)) ** strength
    limit = 2.0 ** Look.MAX_EXPOSURE_EV
    return float(np.clip(gain, 1 / limit, limit))


def apply_exposure(rgb: np.ndarray, gain: float) -> np.ndarray:
    lin = srgb_to_linear(rgb) * gain
    if gain > 1:
        # Roll highlights off smoothly so brightening doesn't create hard clipped patches.
        over = lin > SHOULDER
        lin[over] = SHOULDER + (1 - SHOULDER) * np.tanh((lin[over] - SHOULDER) / (1 - SHOULDER))
    return linear_to_srgb(lin)


# --- 3D LUT -----------------------------------------------------------------

def _trilinear_weights(x: np.ndarray, n: int) -> sparse.csr_matrix:
    """Sparse (len(x), n**3) matrix that trilinearly interpolates a LUT stored as lut[r, g, b]."""
    g = np.clip(x, 0, 1) * (n - 1)
    i0 = np.minimum(np.floor(g).astype(np.int64), n - 2)
    f = (g - i0).astype(np.float32)
    cols, weights = [], []
    for dr in (0, 1):
        for dg in (0, 1):
            for db in (0, 1):
                cols.append(((i0[:, 0] + dr) * n + (i0[:, 1] + dg)) * n + (i0[:, 2] + db))
                weights.append((f[:, 0] if dr else 1 - f[:, 0])
                               * (f[:, 1] if dg else 1 - f[:, 1])
                               * (f[:, 2] if db else 1 - f[:, 2]))
    rows = np.repeat(np.arange(len(x)), 8)
    return sparse.csr_matrix(
        (np.stack(weights, 1).ravel(), (rows, np.stack(cols, 1).ravel())), shape=(len(x), n ** 3)
    )


def identity_lut(n: int) -> np.ndarray:
    axis = np.linspace(0, 1, n, dtype=np.float32)
    r, g, b = np.meshgrid(axis, axis, axis, indexing="ij")
    return np.stack([r, g, b], -1)


def fit_lut(inputs: np.ndarray, targets: np.ndarray, n: int, smoothness: float) -> np.ndarray:
    """
    Least-squares 3D LUT mapping input colors to target colors, (n, n, n, 3).

    Solved as identity + correction, with a smoothness penalty so colors that never appear in the
    training photos fall back to a smooth extension of the ones that do instead of jumping around.
    """
    W = _trilinear_weights(inputs, n)
    second_diff = sparse.diags([1, -2, 1], [0, 1, 2], shape=(n - 2, n))
    eye = sparse.identity(n)
    L = sparse.vstack([
        sparse.kron(sparse.kron(second_diff, eye), eye),
        sparse.kron(sparse.kron(eye, second_diff), eye),
        sparse.kron(sparse.kron(eye, eye), second_diff),
    ])
    scale = len(inputs) / n ** 3  # keeps smoothness meaningful regardless of sample count
    A = (W.T @ W + smoothness * scale * (L.T @ L) + 1e-3 * scale * sparse.identity(n ** 3)).tocsc()
    correction = np.stack([spsolve(A, W.T @ (targets[:, c] - inputs[:, c])) for c in range(3)], -1)
    return np.clip(identity_lut(n) + correction.reshape(n, n, n, 3), 0, 1).astype(np.float32)


def resample_lut(lut: np.ndarray, n: int) -> np.ndarray:
    grid = identity_lut(n).reshape(-1, 3)
    return apply_lut(grid[None], lut)[0].reshape(n, n, n, 3)


def apply_lut(rgb: np.ndarray, lut: np.ndarray, chunk: int = 1 << 20) -> np.ndarray:
    n = lut.shape[0]
    flat_lut = lut.reshape(-1, 3)
    pixels = rgb.reshape(-1, 3)
    out = np.empty_like(pixels)
    for start in range(0, len(pixels), chunk):
        out[start:start + chunk] = _trilinear_weights(pixels[start:start + chunk], n) @ flat_lut
    return out.reshape(rgb.shape)


def write_cube(lut: np.ndarray, path: Path, title: str) -> None:
    n = lut.shape[0]
    lines = [f'TITLE "{title}"', f"LUT_3D_SIZE {n}", ""]
    # .cube order: red changes fastest, then green, then blue.
    for r, g, b in lut.transpose(2, 1, 0, 3).reshape(-1, 3):
        lines.append(f"{r:.6f} {g:.6f} {b:.6f}")
    path.write_text("\n".join(lines) + "\n")


def read_cube(path: Path) -> np.ndarray:
    n, values = None, []
    for line in path.read_text().splitlines():
        parts = line.split()
        if not parts or parts[0].startswith("#"):
            continue
        if parts[0] == "LUT_3D_SIZE":
            n = int(parts[1])
        elif parts[0][0].isdigit() or parts[0][0] in "-.":
            values.append([float(v) for v in parts[:3]])
    if n is None:
        raise ValueError(f"{path} is not a 3D .cube file")
    return np.asarray(values, dtype=np.float32).reshape(n, n, n, 3).transpose(2, 1, 0, 3)


# --- The whole look ---------------------------------------------------------

def look_paths(name: str) -> tuple[Path, Path]:
    """styles/looks/<name>.cube and .json, with the shoot name made filename-safe."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "look"
    return Look.LOOKS_DIR / f"{slug}.cube", Look.LOOKS_DIR / f"{slug}.json"


class RVLook:
    """A look: exposure target + LUT (+ info about how it was learned). Use .apply(rgb)."""

    def __init__(self, lut: np.ndarray, exposure_target: float | None, meta: dict | None = None):
        self.lut = lut
        self.exposure_target = exposure_target
        self.meta = meta or {}

    @classmethod
    def load(cls, name: str) -> "RVLook | None":
        cube, meta_file = look_paths(name)
        if not (cube.exists() and meta_file.exists()):
            return None
        meta = json.loads(meta_file.read_text())
        return cls(read_cube(cube), meta["exposure_target"], meta)

    def save(self, name: str) -> Path:
        cube, meta_file = look_paths(name)
        cube.parent.mkdir(parents=True, exist_ok=True)
        write_cube(self.lut, cube, name)
        meta_file.write_text(json.dumps({**self.meta, "name": name, "exposure_target": self.exposure_target}, indent=2))
        return cube

    def apply(self, rgb: np.ndarray) -> np.ndarray:
        if self.exposure_target is not None:  # None = keep darktable's exposure (your own LUTs)
            rgb = apply_exposure(rgb, exposure_gain(rgb, self.exposure_target))
        return apply_lut(rgb, self.lut)


def user_lut_for(shoot_name: str) -> RVLook | None:
    """A .cube you dropped in styles/luts/ named after the shoot ('Nov 1st Game.cube'), else default.cube.
    Applied as-is on darktable's sRGB render, no exposure changes."""
    slugify = lambda s: re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    files = {slugify(p.stem): p for p in sorted(Look.USER_LUTS_DIR.glob("*.cube"))}
    path = (files.get(slugify(shoot_name)) or files.get(slugify(f"rv {shoot_name}"))
            or files.get(slugify(Look.DEFAULT_USER_LUT)))
    return RVLook(read_cube(path), None, {"source": str(path)}) if path else None
