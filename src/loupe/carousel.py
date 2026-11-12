"""M12: render carousel slides from the M11 drafts as 4:5 PNGs in her fonts and colours.

The cover sits on her photo's local preview. Writes to data/renders/ only; nothing is sent.
"""

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from loupe.brand import BrandProfile, hex_to_rgb

WIDTH = 1080
HEIGHT = 1350
MARGIN = 96
COVER_MAX_SIZE = 112
BODY_MAX_SIZE = 76
MIN_FONT_SIZE = 32
FONT_STEP = 4
LINE_SPACING = 1.18
COVER_SHADE_ALPHA = 150
RULE_WIDTH = 120
RULE_HEIGHT = 8
COUNTER_SIZE = 30

Font = ImageFont.FreeTypeFont
RGB = tuple[int, int, int]
Size = tuple[int, int]


@dataclass(frozen=True)
class Canvas:
    """Image size plus the top and bottom bands kept clear of text."""

    width: int
    height: int
    top: int = MARGIN
    bottom: int = MARGIN

    @property
    def size(self) -> Size:
        return (self.width, self.height)


SLIDE_CANVAS = Canvas(WIDTH, HEIGHT)


@dataclass(frozen=True)
class SlideText:
    lines: tuple[str, ...]
    font: Font


def wrap(text: str, font: Font, width: int) -> tuple[str, ...]:
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        current = ""
        for word in paragraph.split():
            candidate = f"{current} {word}".strip()
            if current and font.getlength(candidate) > width:
                lines.append(current)
                current = word
            else:
                current = candidate
        lines.append(current)
    return tuple(lines)


def line_height(font: Font) -> int:
    ascent, descent = font.getmetrics()
    return round((ascent + descent) * LINE_SPACING)


def fit_text(text: str, font_path: Path, max_size: int, box: tuple[int, int]) -> SlideText:
    """Largest size from max_size down that fits the box without breaking a word."""
    width, height = box
    size = max_size
    while True:
        font = ImageFont.truetype(str(font_path), size=size)
        lines = wrap(text, font, width)
        widest = max((font.getlength(line) for line in lines), default=0)
        if (widest <= width and len(lines) * line_height(font) <= height) or size <= MIN_FONT_SIZE:
            return SlideText(lines, font)
        size -= FONT_STEP


def draw_lines(
    draw: ImageDraw.ImageDraw, text: SlideText, origin: tuple[int, int], colour: RGB
) -> None:
    x, y = origin
    for line in text.lines:
        draw.text((x, y), line, font=text.font, fill=colour)
        y += line_height(text.font)


def cover_background(
    preview: Path | None, fallback: RGB, size: Size = (WIDTH, HEIGHT)
) -> Image.Image:
    """Her photo filling the slide, or a solid cover when the preview can't be decoded."""
    solid = Image.new("RGB", size, fallback)
    if preview is None:
        return solid
    try:
        with Image.open(preview) as photo:
            return ImageOps.fit(ImageOps.exif_transpose(photo).convert("RGB"), size)
    except (OSError, UnidentifiedImageError):
        return solid


def shade_bottom(image: Image.Image, colour: RGB) -> Image.Image:
    """Darken the lower half so cover text reads on any photo."""
    gradient = Image.linear_gradient("L").resize(image.size)
    mask = gradient.point(lambda value: round(value * COVER_SHADE_ALPHA / 255))
    return Image.composite(Image.new("RGB", image.size, colour), image, mask)


def render_cover(
    brand: BrandProfile,
    text: str,
    preview: Path | None,
    counter: str,
    canvas: Canvas = SLIDE_CANVAS,
) -> Image.Image:
    ink, paper = hex_to_rgb(brand.colors.ink), hex_to_rgb(brand.colors.paper)
    background = cover_background(preview, hex_to_rgb(brand.colors.secondary), canvas.size)
    image = shade_bottom(background, ink)
    draw = ImageDraw.Draw(image)
    box = (canvas.width - 2 * MARGIN, canvas.height // 2 - canvas.bottom)
    headline = fit_text(text, brand.fonts.display, COVER_MAX_SIZE, box)
    block_height = len(headline.lines) * line_height(headline.font)
    top = canvas.height - canvas.bottom - block_height
    draw.rectangle(
        (MARGIN, top - 3 * RULE_HEIGHT - RULE_HEIGHT, MARGIN + RULE_WIDTH, top - 3 * RULE_HEIGHT),
        fill=hex_to_rgb(brand.colors.accent),
    )
    draw_lines(draw, headline, (MARGIN, top), paper)
    _draw_counter(draw, brand, counter, paper, canvas)
    return image


def render_body(
    brand: BrandProfile, text: str, counter: str, canvas: Canvas = SLIDE_CANVAS
) -> Image.Image:
    ink, paper = hex_to_rgb(brand.colors.ink), hex_to_rgb(brand.colors.paper)
    image = Image.new("RGB", canvas.size, paper)
    draw = ImageDraw.Draw(image)
    draw.rectangle(
        (MARGIN, canvas.top, MARGIN + RULE_WIDTH, canvas.top + RULE_HEIGHT),
        fill=hex_to_rgb(brand.colors.accent),
    )
    box = (canvas.width - 2 * MARGIN, canvas.height - canvas.top - canvas.bottom - 2 * MARGIN)
    body = fit_text(text, brand.fonts.body, BODY_MAX_SIZE, box)
    block_height = len(body.lines) * line_height(body.font)
    draw_lines(draw, body, (MARGIN, (canvas.height - block_height) // 2), ink)
    _draw_counter(draw, brand, counter, ink, canvas)
    return image


def _draw_counter(
    draw: ImageDraw.ImageDraw, brand: BrandProfile, counter: str, colour: RGB, canvas: Canvas
) -> None:
    font = ImageFont.truetype(str(brand.fonts.body), size=COUNTER_SIZE)
    left = canvas.width - MARGIN - font.getlength(counter)
    draw.text((left, canvas.top - COUNTER_SIZE // 2), counter, font=font, fill=colour)


def render_sequence(
    brand: BrandProfile,
    texts: list[str],
    cover_preview: Path | None,
    target_dir: Path,
    canvas: Canvas = SLIDE_CANVAS,
    prefix: str = "slide",
) -> list[Path]:
    """First text over her photo, the rest on paper. Writes <prefix>-NN.png."""
    if not texts:
        raise ValueError("a sequence needs at least one text")
    target_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for number, text in enumerate(texts, start=1):
        counter = f"{number:02d}/{len(texts):02d}"
        image = (
            render_cover(brand, text, cover_preview, counter, canvas)
            if number == 1
            else render_body(brand, text, counter, canvas)
        )
        path = target_dir / f"{prefix}-{number:02d}.png"
        image.save(path, optimize=True)
        paths.append(path)
    return paths


def render_carousel(
    brand: BrandProfile, slides: list[str], cover_preview: Path | None, target_dir: Path
) -> list[Path]:
    return render_sequence(brand, slides, cover_preview, target_dir)


def carousel_dir(renders_dir: Path, week_start: str, day: str) -> Path:
    return renders_dir / "carousel" / f"{week_start}-{day.lower()}"
