"""Find individual pieces in a photo of bricks spread out on a plain background.

Approach: estimate the background colour from the image border, white-balance
against it, mark every pixel that differs enough from the background, then
take each connected blob as one piece. Works well when pieces don't touch and
the background contrasts with the bricks.
"""
from dataclasses import dataclass

import cv2
import numpy as np

WORK_SIZE = 1600


@dataclass
class Piece:
    crop_jpeg: bytes       # full-resolution crop for recognition / display
    bbox: tuple            # x, y, w, h in original image pixels
    lab: tuple             # median CIELAB colour of the piece (white-balanced)
    area_frac: float       # piece area relative to image


def decode_image(data: bytes) -> np.ndarray:
    arr = np.frombuffer(data, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)  # applies EXIF orientation
    if img is None:
        raise ValueError("Could not read image (use JPEG or PNG)")
    return img


def _border(img: np.ndarray, k: int) -> np.ndarray:
    return np.concatenate([
        img[:k].reshape(-1, 3), img[-k:].reshape(-1, 3),
        img[:, :k].reshape(-1, 3), img[:, -k:].reshape(-1, 3),
    ])


def white_balance(img: np.ndarray, bg_bgr: np.ndarray) -> np.ndarray:
    """Scale channels so a near-neutral background becomes neutral grey."""
    bg = bg_bgr.astype(np.float32) + 1e-3
    gains = np.clip(bg.mean() / bg, 0.75, 1.35)
    # Only correct if the background is roughly neutral already (not a red mat).
    sat = (bg.max() - bg.min()) / max(bg.max(), 1)
    if sat > 0.35:
        return img
    out = img.astype(np.float32) * gains
    return np.clip(out, 0, 255).astype(np.uint8)


def to_lab(bgr: np.ndarray) -> np.ndarray:
    """Real CIELAB (L 0..100) from uint8 BGR."""
    f = bgr.astype(np.float32) / 255.0
    return cv2.cvtColor(f, cv2.COLOR_BGR2LAB)


def segment(img: np.ndarray, max_pieces: int = 250) -> list[Piece]:
    H, W = img.shape[:2]
    scale = min(1.0, WORK_SIZE / max(H, W))
    small = cv2.resize(img, (int(W * scale), int(H * scale)), interpolation=cv2.INTER_AREA) if scale < 1 else img.copy()
    h, w = small.shape[:2]

    k = max(4, int(min(h, w) * 0.02))
    bg_bgr = np.median(_border(small, k), axis=0)
    balanced = white_balance(small, bg_bgr)
    bg_bgr_bal = np.median(_border(balanced, k), axis=0)

    lab = to_lab(cv2.GaussianBlur(balanced, (5, 5), 0))
    bg_lab = to_lab(bg_bgr_bal.reshape(1, 1, 3).astype(np.uint8))[0, 0]
    # Weight chroma more than lightness so soft shadows count less.
    d = lab - bg_lab
    dist = np.sqrt((d[..., 0] * 0.6) ** 2 + d[..., 1] ** 2 + d[..., 2] ** 2)

    dist8 = np.clip(dist * 3, 0, 255).astype(np.uint8)
    otsu, _ = cv2.threshold(dist8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    # Otsu adapts to the most saturated pieces; cap it so black, white and grey
    # pieces (which differ from the background mostly in lightness) still count.
    thr = min(max(otsu, 30), 54)  # ~10..18 Lab units
    mask = (dist8 > thr).astype(np.uint8) * 255

    ksz = max(3, int(min(h, w) * 0.004) | 1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((ksz, ksz), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((ksz * 2 + 1, ksz * 2 + 1), np.uint8))

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    total = h * w
    min_area = max(40, total * 0.00008)
    max_area = total * 0.25

    balanced_full = white_balance(img, bg_bgr)
    pieces: list[Piece] = []
    for c in sorted(contours, key=cv2.contourArea, reverse=True):
        area = cv2.contourArea(c)
        if area < min_area or area > max_area:
            continue
        x, y, bw, bh = cv2.boundingRect(c)
        # Skip blobs glued to the image edge (table edge, hand, etc.)
        if x <= 1 or y <= 1 or x + bw >= w - 1 or y + bh >= h - 1:
            continue

        # Colour: median of interior pixels, ignoring specular highlights.
        pm = np.zeros((h, w), np.uint8)
        cv2.drawContours(pm, [c], -1, 255, -1)
        er = cv2.erode(pm, np.ones((5, 5), np.uint8))
        if cv2.countNonZero(er) > 20:
            pm = er
        px = to_lab(balanced[pm > 0].reshape(-1, 1, 3)).reshape(-1, 3)
        if len(px) > 50:
            hi = np.percentile(px[:, 0], 92)
            px = px[px[:, 0] <= hi]
        med = tuple(float(v) for v in np.median(px, axis=0))

        # Crop from the full-resolution, white-balanced image with padding.
        pad = int(max(bw, bh) * 0.15) + 4
        x0 = max(0, int((x - pad) / scale)); y0 = max(0, int((y - pad) / scale))
        x1 = min(W, int((x + bw + pad) / scale)); y1 = min(H, int((y + bh + pad) / scale))
        crop = balanced_full[y0:y1, x0:x1]
        if max(crop.shape[:2]) > 800:
            f = 800 / max(crop.shape[:2])
            crop = cv2.resize(crop, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
        ok, jpg = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            continue
        pieces.append(Piece(jpg.tobytes(), (x0, y0, x1 - x0, y1 - y0), med, area / total))
        if len(pieces) >= max_pieces:
            break

    # Reading order: top-to-bottom, left-to-right in rows.
    pieces.sort(key=lambda p: (p.bbox[1] // max(1, H // 12), p.bbox[0]))
    return pieces


def preview_jpeg(img: np.ndarray, max_side: int = 1400) -> bytes:
    H, W = img.shape[:2]
    f = min(1.0, max_side / max(H, W))
    small = cv2.resize(img, None, fx=f, fy=f, interpolation=cv2.INTER_AREA) if f < 1 else img
    return cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()
