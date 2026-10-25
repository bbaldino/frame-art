from PIL import Image, ImageDraw

from frame_art import imaging


def test_plan_crops_near_16_9_and_pads_portraits():
    assert imaging.plan_for(4000, 2400).mode == "crop"  # 1.67
    assert imaging.plan_for(3000, 4000).mode == "pad"
    assert imaging.plan_for(4000, 3000).mode == "pad"  # 4:3 loses too much to crop


def test_plan_scale_is_upscale_factor():
    crop = imaging.plan_for(3840, 2160)
    assert crop.scale == 1.0
    pad = imaging.plan_for(1000, 2000)
    assert round(pad.scale * 2000) == round(2160 * imaging.PAD_FILL)


def test_crop_box_is_16_9_and_follows_the_subject():
    im = Image.new("RGB", (4000, 2000), (128, 128, 128))
    # Busy checkerboard on the right third; flat everywhere else.
    d = ImageDraw.Draw(im)
    for x in range(3000, 4000, 40):
        for y in range(0, 2000, 40):
            if (x // 40 + y // 40) % 2:
                d.rectangle((x, y, x + 39, y + 39), fill=(255, 255, 255))
    left, top, right, bottom = imaging.crop_box(im)
    assert abs((right - left) / (bottom - top) - 16 / 9) < 0.01
    assert left > 200  # pulled right, toward the detail


def test_trim_borders_removes_flat_strip():
    im = Image.new("RGB", (1000, 600), (0, 0, 0))
    inner = Image.effect_noise((950, 600), 60).convert("RGB")
    im.paste(inner, (50, 0))
    out = imaging.trim_borders(im)
    assert 940 <= out.width <= 952
    assert out.height == 600


def test_trim_borders_leaves_busy_image_alone():
    im = Image.effect_noise((800, 450), 60).convert("RGB")
    assert imaging.trim_borders(im).size == im.size


def test_render_output_is_panel_size():
    im = Image.effect_noise((900, 1200), 40).convert("RGB")
    out = imaging.render(im, imaging.plan_for(*im.size))
    assert out.size == imaging.PANEL


def test_phash_matches_resized_copy_not_different_image():
    a = Image.effect_noise((600, 400), 80).convert("RGB")
    b = a.resize((300, 200))
    c = Image.effect_noise((600, 400), 80).convert("RGB")
    assert imaging.hamming(imaging.phash(a), imaging.phash(b)) <= 8
    assert imaging.hamming(imaging.phash(a), imaging.phash(c)) > 8


def _tondo(bg=(250, 250, 250)) -> Image.Image:
    im = Image.new("RGB", (1200, 1200), bg)
    disc = Image.effect_noise((1100, 1100), 70).convert("RGB")
    mask = Image.new("L", disc.size, 0)
    ImageDraw.Draw(mask).ellipse((0, 0, 1099, 1099), fill=255)
    im.paste(disc, (50, 50), mask)
    return im


def test_artwork_mask_finds_round_work_on_card():
    mask = imaging.artwork_mask(_tondo())
    assert mask is not None
    assert mask.getpixel((5, 5)) < 20  # corner is backdrop
    assert mask.getpixel((600, 600)) > 235  # centre is the work


def test_artwork_mask_ignores_ordinary_paintings():
    assert imaging.artwork_mask(Image.effect_noise((900, 1200), 60).convert("RGB")) is None
