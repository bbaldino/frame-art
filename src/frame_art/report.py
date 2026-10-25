from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .model import Work
from .pipeline import FilterReport

TILE = (640, 360)
CAPTION_H = 64
COLS = 4
FONT_PATHS = [
    "/usr/share/fonts/truetype/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for p in FONT_PATHS:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size)


def _fit(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> str:
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text + "…"


@dataclass
class Rendered:
    work: Work
    path: Path
    mode: str
    scale: float


def contact_sheet(items: list[Rendered], dest: Path, title: str) -> None:
    rows = (len(items) + COLS - 1) // COLS
    header = 56
    gap = 12
    cell_w, cell_h = TILE[0], TILE[1] + CAPTION_H
    sheet = Image.new(
        "RGB",
        (COLS * cell_w + (COLS + 1) * gap, header + rows * (cell_h + gap) + gap),
        (24, 24, 26),
    )
    draw = ImageDraw.Draw(sheet)
    big, small, tiny = _font(26), _font(16), _font(14)
    draw.text((gap, 14), title, font=big, fill=(235, 235, 235))
    for i, it in enumerate(items):
        x = gap + (i % COLS) * (cell_w + gap)
        y = header + (i // COLS) * (cell_h + gap)
        with Image.open(it.path) as im:
            sheet.paste(im.resize(TILE, Image.LANCZOS), (x, y))
        w = it.work
        line1 = _fit(draw, w.title, small, cell_w - 8)
        who = ", ".join(x for x in (w.artist, w.date) if x) or "unknown"
        line2 = _fit(draw, who, tiny, cell_w - 8)
        flags = [w.source, it.mode, f"{it.scale:.2f}x"]
        if w.highlight:
            flags.append("highlight")
        flags += w.tags
        draw.text((x + 4, y + TILE[1] + 6), line1, font=small, fill=(235, 235, 235))
        draw.text((x + 4, y + TILE[1] + 27), line2, font=tiny, fill=(175, 175, 175))
        draw.text(
            (x + 4, y + TILE[1] + 45),
            " · ".join(flags),
            font=tiny,
            fill=(120, 170, 220),
        )
    sheet.save(dest, "JPEG", quality=88)


def markdown(
    per_source: dict[str, FilterReport], rendered: list[Rendered], dupes: list[str]
) -> str:
    lines = [
        "# Frame art dry run",
        "",
        "## Filter funnel (crawled sample per source)",
        "",
    ]
    lines.append(
        "| source | crawled | passed | crop | pad | rejected (reason: n) | season tags |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for name, r in per_source.items():
        rej = "; ".join(f"{k}: {v}" for k, v in r.rejected.most_common()) or "-"
        seasons = ", ".join(f"{k} {v}" for k, v in r.seasons.most_common()) or "-"
        lines.append(
            f"| {name} | {r.total} | {r.passed} | {r.modes['crop']} | {r.modes['pad']} "
            f"| {rej} | {seasons} |"
        )
    lines += ["", "## Rendered sample", ""]
    for it in rendered:
        w = it.work
        lines.append(
            f"- `{it.path.name}`: {w.title} by {w.artist or 'unknown'} ({w.date or 'n.d.'}), "
            f"{it.mode} at {it.scale:.2f}x, {w.license}, <{w.page_url}>"
        )
    if dupes:
        lines += ["", "## Perceptual duplicates dropped", ""] + [
            f"- {d}" for d in dupes
        ]
    return "\n".join(lines) + "\n"
