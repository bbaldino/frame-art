from __future__ import annotations

import io
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms, ImageDraw, ImageEnhance, ImageFilter

# Museum masters run past 100MP; Pillow's bomb guard stops at ~89MP.
Image.MAX_IMAGE_PIXELS = 400_000_000

PANEL = (3840, 2160)
PANEL_AR = PANEL[0] / PANEL[1]
# How much of the picture a crop to 16:9 may cut off; past this the work is padded.
# strict (13%) is aspect ~1.55-2.05, medium (21%) ~1.40-2.25, loose (27%) ~1.30-2.45;
# native only crops what is already 16:9 give or take a scan edge.
CROP_LEVELS = {"native": 0.02, "strict": 0.13, "medium": 0.21, "loose": 0.27}
# Beyond these a padded work is a sliver on the screen (altarpiece wings, scrolls).
AR_MIN, AR_MAX = 0.5, 3.5
# A padded work sits inside this fraction of the panel, leaving a margin.
PAD_FILL = 0.92
_SRGB = ImageCms.createProfile("sRGB")


@dataclass(frozen=True)
class Plan:
    mode: str  # "crop" | "pad"
    scale: float  # source pixels -> panel pixels; > 1 means upscaling

    def needed_width(self, src_width: int) -> int:
        """Smallest source width that renders without upscaling."""
        return min(src_width, math.ceil(src_width * self.scale) + 8)


def crop_loss(width: int, height: int) -> float:
    """Fraction of the picture lost cropping it to the panel's shape."""
    ar = width / height
    return 1 - min(ar, PANEL_AR) / max(ar, PANEL_AR)


def plan_for(width: int, height: int, max_crop: float = CROP_LEVELS["strict"]) -> Plan:
    if crop_loss(width, height) <= max_crop:
        return Plan("crop", max(PANEL[0] / width, PANEL[1] / height))
    return Plan("pad", min(PANEL[0] * PAD_FILL / width, PANEL[1] * PAD_FILL / height))


def peek_size(data: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(data)).size


def load(data: bytes, max_side: int | None = None) -> Image.Image:
    im = Image.open(io.BytesIO(data))
    if max_side and im.format == "JPEG":
        # Let libjpeg decode at a reduced DCT scale when we need far fewer pixels.
        ratio = max_side / max(im.size)
        if ratio < 1:
            im.draft("RGB", (int(im.width * ratio), int(im.height * ratio)))
    icc = im.info.get("icc_profile")
    if max_side and max(im.size) > max_side:
        # Shrink before colour conversion so a 100MP master is held only once.
        im.thumbnail((max_side, max_side), Image.LANCZOS, reducing_gap=3.0)
    if icc:
        try:
            src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            im = ImageCms.profileToProfile(im, src, _SRGB, outputMode="RGB")
        except (ImageCms.PyCMSError, OSError):
            pass
    if im.mode != "RGB":
        if im.mode in ("I;16", "I;16B", "I"):
            im = im.point(lambda v: v / 256).convert("L")
        im = im.convert("RGB")
    return im


def trim_borders(im: Image.Image, max_frac: float = 0.08) -> Image.Image:
    """Strip flat scan margins (black strips, white paper, grey card) from the edges."""
    small = im.copy()
    small.thumbnail((800, 800))
    gray = np.asarray(small.convert("L"), dtype=np.float32)
    h, w = gray.shape

    def depth(lines: np.ndarray) -> int:
        # lines[i] is the i-th row/column in from the edge.
        limit = int(len(lines) * max_frac)
        n = 0
        while n < limit and lines[n].std() < 6:
            n += 1
        return n

    top, bottom = depth(gray), depth(gray[::-1])
    left, right = depth(gray.T), depth(gray.T[::-1])
    if not (top or bottom or left or right):
        return im
    sx, sy = im.width / w, im.height / h
    # One extra small-pixel inward to drop the anti-aliased edge too.
    left, top, right, bottom = (n + 1 if n else 0 for n in (left, top, right, bottom))
    return im.crop(
        (
            round(left * sx),
            round(top * sy),
            im.width - round(right * sx),
            im.height - round(bottom * sy),
        )
    )


def _energy(im: Image.Image) -> np.ndarray:
    """Per-pixel interest: edge strength plus a little saturation."""
    small = im.copy()
    small.thumbnail((512, 512))
    gray = np.asarray(small.convert("L"), dtype=np.float32)
    gx = np.abs(np.diff(gray, axis=1, prepend=gray[:, :1]))
    gy = np.abs(np.diff(gray, axis=0, prepend=gray[:1, :]))
    hsv = np.asarray(small.convert("HSV"), dtype=np.float32)
    return gx + gy + 0.25 * hsv[:, :, 1]


def crop_box(im: Image.Image) -> tuple[int, int, int, int]:
    """Choose the 16:9 window with the most visual interest, biased to the centre."""
    w, h = im.size
    if w / h > PANEL_AR:
        win, length, axis = round(h * PANEL_AR), w, 0
    else:
        win, length, axis = round(w / PANEL_AR), h, 1
    if win >= length:
        return (0, 0, w, h)
    energy = _energy(im)
    profile = energy.sum(axis=axis)  # per column (axis 0) or per row (axis 1)
    n = len(profile)
    k = max(1, round(n * win / length))
    sums = np.convolve(profile, np.ones(k), mode="valid")
    offsets = np.arange(len(sums))
    centre = (n - k) / 2
    bias = 1 - 0.35 * np.abs(offsets - centre) / max(centre, 1)
    best = int(np.argmax(sums * bias))
    start = round(best * length / n)
    start = min(start, length - win)
    return (start, 0, start + win, h) if axis == 0 else (0, start, w, start + win)


def _background(im: Image.Image) -> Image.Image:
    """A blurred, darkened, screen-filling copy of the work."""
    w, h = im.size
    small = (PANEL[0] // 10, PANEL[1] // 10)
    scale = max(small[0] / w, small[1] / h)
    bg = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.BILINEAR)
    left, top = (bg.width - small[0]) // 2, (bg.height - small[1]) // 2
    bg = bg.crop((left, top, left + small[0], top + small[1]))
    bg = bg.filter(ImageFilter.GaussianBlur(10))
    bg = bg.resize(PANEL, Image.BICUBIC)
    bg = ImageEnhance.Color(bg).enhance(0.75)
    return ImageEnhance.Brightness(bg).enhance(0.55)


PAD_STYLES = ("blur", "solid", "matte")


def _solid_background(im: Image.Image) -> Image.Image:
    """A flat, dark colour taken from the work itself."""
    small = im.copy()
    small.thumbnail((64, 64))
    mean = np.asarray(small, dtype=np.float32).reshape(-1, 3).mean(axis=0)
    return Image.new("RGB", PANEL, tuple(int(c * 0.3) for c in mean))


def artwork_mask(im: Image.Image) -> Image.Image | None:
    """Mask (255 = artwork) for a work photographed on a flat backdrop, else None.

    Round and irregular works (tondi, fans, cut-out objects) are often scanned on
    a white card. When all four corners show the same flat colour, flood the
    backdrop in from the edges. The pipeline uses this to skip such works: they
    don't fill a 16:9 screen well.
    """
    small = im.copy()
    small.thumbnail((400, 400))
    px = np.asarray(small, dtype=np.float32)
    h, w = px.shape[:2]
    k = max(4, min(w, h) // 25)
    corners = [px[:k, :k], px[:k, -k:], px[-k:, :k], px[-k:, -k:]]
    if any(c.std(axis=(0, 1)).max() > 4 for c in corners):
        return None
    means = np.array([c.mean(axis=(0, 1)) for c in corners])
    if np.abs(means - means.mean(axis=0)).max() > 12:
        return None
    # Only near-white, neutral card. Dark corners are usually painted background
    # (flooding them would cut into the painting), and grey card with soft
    # shadows leaves a halo; both look fine left as they are.
    bg_colour = means.mean(axis=0)
    if bg_colour.min() < 215 or bg_colour.max() - bg_colour.min() > 18:
        return None

    marked = small.copy()
    sentinel = (255, 0, 255)
    # Seed from every backdrop-coloured border pixel, not just the corners: where
    # a round work touches the edge it splits the backdrop into separate pockets.
    border = [(x, 0) for x in range(w)] + [(x, h - 1) for x in range(w)]
    border += [(0, y) for y in range(h)] + [(w - 1, y) for y in range(h)]
    for x, y in border:
        if (
            np.abs(px[y, x] - bg_colour).max() <= 24
            and marked.getpixel((x, y)) != sentinel
        ):
            ImageDraw.floodfill(marked, (x, y), sentinel, thresh=24)
    bg = np.all(np.asarray(marked) == sentinel, axis=2)
    frac = bg.mean()
    # Too little backdrop: just a scan edge. Too much: the fill leaked into the work.
    if not 0.03 <= frac <= 0.6:
        return None
    mask = Image.fromarray(np.where(bg, 0, 255).astype(np.uint8))
    # The flood only reaches backdrop connected to the border, so there are no
    # pinholes to close. Pull the edge in a pixel past the anti-aliased rim,
    # then feather it slightly.
    mask = mask.filter(ImageFilter.MinFilter(3))
    return mask.resize(im.size, Image.BILINEAR).filter(ImageFilter.GaussianBlur(1))


def render(im: Image.Image, plan: Plan, pad_style: str = "blur") -> Image.Image:
    """Render to the panel. With ``pad_style="matte"`` a padded work is only
    scaled to fit, keeping its own shape, so the TV's matte can frame it."""
    if plan.mode == "crop":
        return im.crop(crop_box(im)).resize(PANEL, Image.LANCZOS)

    w, h = im.size
    if pad_style == "matte":
        fit = min(PANEL[0] / w, PANEL[1] / h)
        return im.resize((round(w * fit), round(h * fit)), Image.LANCZOS)

    size = (round(w * plan.scale), round(h * plan.scale))
    art = im.resize(size, Image.LANCZOS)
    canvas = _background(im) if pad_style == "blur" else _solid_background(im)
    pos = ((PANEL[0] - size[0]) // 2, (PANEL[1] - size[1]) // 2)
    # Soft drop shadow so the work reads as an object on the background.
    shadow = Image.new("L", PANEL, 0)
    ImageDraw.Draw(shadow).rectangle(
        (pos[0] + 6, pos[1] + 12, pos[0] + size[0] + 6, pos[1] + size[1] + 12), fill=150
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(24))
    canvas.paste(Image.new("RGB", PANEL, (0, 0, 0)), (0, 0), shadow)
    canvas.paste(art, pos)
    return canvas


def save_jpeg(im: Image.Image, path: Path) -> None:
    # Baseline 8-bit sRGB: the Frame silently fails to render progressive JPEGs.
    im.save(
        path, "JPEG", quality=90, optimize=True, progressive=False, subsampling="4:2:0"
    )


_DCT = None


def phash(im: Image.Image) -> int:
    """64-bit DCT perceptual hash."""
    global _DCT
    if _DCT is None:
        n = 32
        k = np.arange(n)
        _DCT = np.cos(np.pi * (2 * k[None, :] + 1) * k[:, None] / (2 * n))
    px = np.asarray(im.convert("L").resize((32, 32), Image.LANCZOS), dtype=np.float64)
    low = (_DCT @ px @ _DCT.T)[:8, :8].flatten()
    bits = low > np.median(low[1:])
    return int("".join("1" if b else "0" for b in bits), 2)


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def palette(im: Image.Image, n: int = 5) -> list[str]:
    """The ``n`` dominant colours as hex, most common first."""
    small = im.convert("RGB")
    small.thumbnail((96, 96))
    q = small.quantize(colors=n, method=Image.Quantize.MEDIANCUT)
    pal = q.getpalette() or []
    counts = sorted(q.getcolors() or [], reverse=True)
    return [
        "#{:02x}{:02x}{:02x}".format(*pal[i * 3 : i * 3 + 3]) for _, i in counts[:n]
    ]
