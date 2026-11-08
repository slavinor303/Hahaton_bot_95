import io
import logging
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from config import (
    DEJAVU_FONT_PATH,
    DIFFICULTY_STARS,
    FONTS_DIR,
    SUBJECT_LABELS,
    difficulty_mark,
)
from math_render import (
    FORMULA_DPI,
    latex_to_pdf_text,
    prepare_task_display,
    render_latex_to_png,
    split_definite_integral,
)
from models import CardSet

DEJAVU_URL = (
    "https://github.com/dejavu-fonts/dejavu-fonts/raw/version_2_37/ttf/DejaVuSans.ttf"
)
_FONT_REGISTERED = False
_FONT_NAME = "PortalFont"
# Единый размер строки задания (номер, текст, формула, «Отв:»).
_TASK_CONTENT_RATIO = 0.86


@dataclass
class CardTypography:
    subject: float
    topic: float
    variant: float
    meta: float
    task: float
    answer: float
    math: int
    diff_label: float
    task_block_gap: float
    header_zone_pt: float
    row_height: float
    task_num: float


def _task_columns(task_count: int) -> int:
    return 2 if task_count > 6 else 1


def _typography(task_count: int, card_h_pt: float) -> CardTypography:
    """Шрифты ~ от 1/6 высоты карточки (два варианта на A4)."""
    cols = _task_columns(task_count)
    row_count = max(1, (task_count + cols - 1) // cols)

    header_zone_pt = card_h_pt / 6.0
    if task_count > 6:
        header_zone_pt *= 0.92
    base = header_zone_pt * 0.27

    subject = base * 1.18
    topic = base * 1.05
    variant = base * 0.9
    meta = base * 0.82
    diff_label = base * 0.8

    extra = max(0, row_count - 3)
    content_shrink = 0.94**extra
    content_pt = base * 0.86 * content_shrink * _TASK_CONTENT_RATIO

    tasks_area = card_h_pt - header_zone_pt - 12 * mm
    per_row = max(tasks_area / row_count, 1.0)
    fit = min(1.0, per_row / 48.0)
    if fit < 1.0:
        content_pt *= fit
    if task_count >= 10:
        content_pt *= 0.92

    task = content_pt
    task_num = content_pt
    math = max(6, int(round(content_pt * 1.32)))
    answer = content_pt
    task_block_gap = max(1.0 * mm, min(per_row * 0.06, 2.2 * mm))
    row_height = 0.0

    return CardTypography(
        subject=subject,
        topic=topic,
        variant=variant,
        meta=meta,
        task=task,
        answer=answer,
        math=math,
        diff_label=diff_label,
        task_block_gap=task_block_gap,
        header_zone_pt=header_zone_pt,
        row_height=row_height,
        task_num=task_num,
    )


def _ensure_font() -> str:
    global _FONT_REGISTERED
    if _FONT_REGISTERED:
        return _FONT_NAME

    candidates = [
        DEJAVU_FONT_PATH,
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path(r"C:\Windows\Fonts\arial.ttf"),
        Path(r"C:\Windows\Fonts\DejaVuSans.ttf"),
    ]
    font_path: Path | None = None
    for path in candidates:
        if path.exists():
            font_path = path
            break

    if font_path is None:
        FONTS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            urllib.request.urlretrieve(DEJAVU_URL, DEJAVU_FONT_PATH)
            font_path = DEJAVU_FONT_PATH
        except Exception:
            raise FileNotFoundError(
                "Не найден TTF-шрифт с кириллицей. Положите DejaVuSans.ttf в assets/fonts/"
            )

    pdfmetrics.registerFont(TTFont(_FONT_NAME, str(font_path)))
    _FONT_REGISTERED = True
    return _FONT_NAME


def _card_difficulty_label(card_set: CardSet) -> str:
    if card_set.topics:
        order = {"basic": 1, "medium": 2, "advanced": 3}
        hardest = max(card_set.topics, key=lambda t: order.get(t.difficulty, 1))
        return difficulty_mark(hardest.difficulty)
    return difficulty_mark(card_set.difficulty)


def _wrap_plain(
    c: canvas.Canvas, text: str, x: float, y: float, max_w: float, font: str, size: float
) -> float:
    c.setFont(font, size)
    words = text.split()
    line = ""
    line_h = size * 1.35
    cy = y
    for word in words:
        trial = f"{line} {word}".strip()
        if pdfmetrics.stringWidth(trial, font, size) <= max_w:
            line = trial
        else:
            if line:
                c.drawString(x, cy, line)
                cy -= line_h
            line = word
    if line:
        c.drawString(x, cy, line)
        cy -= line_h
    return cy


def _fit_png(png: bytes, max_h: float, max_w: float) -> tuple[ImageReader, float, float]:
    img = ImageReader(io.BytesIO(png))
    iw, ih = img.getSize()
    scale = min(max_w / max(iw, 1), max_h / max(ih, 1))
    return img, iw * scale, ih * scale


def _draw_integral_limits(
    c: canvas.Canvas,
    latex: str,
    x: float,
    y: float,
    content_w: float,
    typo: CardTypography,
    floor: float | None = None,
) -> float | None:
    """Пределы-дроби рисуем отдельно, размером почти как само задание, а не как индекс."""
    parts = split_definite_integral(latex)
    if not parts:
        return None
    lower, upper, body = parts
    fs = typo.math
    limit_fs = fs
    png_i = render_latex_to_png(r"\int", fontsize=fs)
    png_u = render_latex_to_png(upper, fontsize=limit_fs)
    png_l = render_latex_to_png(lower, fontsize=limit_fs)
    png_b = render_latex_to_png(body, fontsize=fs) if body else None
    if not (png_i and png_u and png_l):
        return None

    limit_h = typo.task * 1.7
    gap = 0.3 * mm
    half = max(content_w * 0.38, 8 * mm)
    uimg, uw, uh = _fit_png(png_u, limit_h, half)
    limg, lw, lh = _fit_png(png_l, limit_h, half)
    iimg, iw, ih = _fit_png(png_i, typo.task * 1.9, content_w * 0.2)
    bimg = bw = bh = None
    col_w = max(uw, lw)
    body_max_w = max(content_w - iw - col_w - 2.2 * mm, 10 * mm)
    if png_b:
        bimg, bw, bh = _fit_png(png_b, typo.task * 1.2, body_max_w)

    stack_h = uh + gap + lh
    block_h = max(ih, stack_h, bh or 0)
    top = y + typo.task * 0.4
    bottom = top - block_h
    if floor is not None and bottom < floor:
        return None

    c.drawImage(iimg, x, bottom + (block_h - ih) / 2, width=iw, height=ih, mask="auto")
    lim_x = x + iw + 0.3 * mm
    lim_top = bottom + (block_h + stack_h) / 2
    c.drawImage(uimg, lim_x, lim_top - uh, width=uw, height=uh, mask="auto")
    c.drawImage(limg, lim_x, lim_top - uh - gap - lh, width=lw, height=lh, mask="auto")
    if bimg is not None:
        c.drawImage(
            bimg,
            lim_x + col_w + 0.7 * mm,
            bottom + (block_h - bh) / 2,
            width=bw,
            height=bh,
            mask="auto",
        )
    return bottom


def _draw_latex_block(
    c: canvas.Canvas,
    latex: str,
    x: float,
    y: float,
    content_w: float,
    typo: CardTypography,
    *,
    below: bool = False,
    floor: float | None = None,
) -> float | None:
    """Формула под текстом. floor — нижняя граница: ниже неё уже строка «Ответ»."""
    drawn_bottom = _draw_integral_limits(c, latex, x, y, content_w, typo, floor)
    if drawn_bottom is not None:
        return drawn_bottom
    img, dw, dh = _formula_image(latex, content_w, typo.task)
    if img is None:
        return None
    gap = 0.6 * mm
    if below:
        img_bottom = y - gap - dh
    else:
        img_bottom = y + typo.task * 0.85 - dh
    if floor is not None and img_bottom < floor:
        img_bottom = floor
    c.drawImage(img, x, img_bottom, width=dw, height=dh, mask="auto")
    return img_bottom


def _formula_image(latex: str, max_w: float, text_size: float):
    """Формула того же роста, что строка задания. Короткую не растягиваем на колонку."""
    fontsize = max(7, min(13, int(round(text_size))))
    raw = render_latex_to_png(latex, fontsize=fontsize, dpi=FORMULA_DPI)
    if not raw:
        return None, 0.0, 0.0
    img = ImageReader(io.BytesIO(raw))
    iw, ih = img.getSize()
    # PNG в пикселях при FORMULA_DPI. В PDF нужны пункты, иначе «x» становится огромным.
    nat_w = iw * 72.0 / FORMULA_DPI
    nat_h = ih * 72.0 / FORMULA_DPI
    scale = 1.0
    if nat_w > max_w:
        scale = max_w / nat_w
    # Дробь выше строки. Сильно ужимать её нельзя: числитель становится верхним индексом.
    max_h = text_size * 2.5
    if nat_h * scale > max_h:
        scale = max_h / nat_h
    return img, nat_w * scale, nat_h * scale


def _draw_mixed(
    c: canvas.Canvas,
    parts: list[tuple[str, str]],
    x: float,
    y: float,
    max_w: float,
    font: str,
    typo: CardTypography,
    floor: float,
) -> float:
    """Фраза и формулы в одну строку: sin^2 и π/4 рисуются, а не пишутся текстом."""
    size = typo.task
    line_h = size * 1.35
    cx = x
    cy = y
    line_floor = y - size * 0.3
    right = x + max_w
    c.setFont(font, size)

    def next_line() -> None:
        nonlocal cx, cy, line_floor
        # Полная строка вниз, иначе картинка формулы залезает на текст сверху.
        cy = line_floor - size * 1.05
        cx = x
        line_floor = cy - size * 0.35

    for index, (kind, value) in enumerate(parts):
        if kind == "text":
            for word in value.split():
                width = pdfmetrics.stringWidth(word + " ", font, size)
                if cx > x and cx + width > right:
                    next_line()
                c.setFont(font, size)
                c.drawString(cx, cy, word)
                cx += width
            continue
        png, dw, dh = _formula_image(value, max_w, size)
        if png is None:
            fallback = latex_to_pdf_text(value)
            width = pdfmetrics.stringWidth(fallback + " ", font, size)
            if cx > x and cx + width > right:
                next_line()
            c.setFont(font, size)
            c.drawString(cx, cy, fallback)
            cx += width
            continue
        tall = dh > size * 1.45
        # Перенос только если формула не влезает. Иначе дробь спрыгивает
        # под текст и уравнение расползается по строкам.
        if cx > x and cx + dw > right:
            next_line()
        img_top = cy + size * 0.85
        img_bottom = img_top - dh
        c.drawImage(png, cx, img_bottom, width=dw, height=dh, mask="auto")
        line_floor = min(line_floor, img_bottom)
        cx += dw + 1.1 * mm
        nxt = parts[index + 1] if index + 1 < len(parts) else None
        if (
            tall
            and nxt
            and nxt[0] == "text"
            and re.search(r"[A-Za-zА-Яа-яЁё]", nxt[1])
        ):
            next_line()
    return line_floor


def _draw_task(
    c: canvas.Canvas,
    task,
    x: float,
    y: float,
    max_w: float,
    font: str,
    typo: CardTypography,
    card_bottom: float,
    compact: bool,
) -> float:
    """Номер и условие в одной строке, «Отв:» сразу под ними, без наложения."""
    c.setFont(font, typo.task_num)
    num_label = f"{task.number}."
    c.drawString(x, y, num_label)
    num_w = pdfmetrics.stringWidth(num_label, font, typo.task_num)
    content_x = x + num_w + 1.6 * mm
    content_w = max(max_w - (content_x - x), 12 * mm)

    display = prepare_task_display(task.text, task.text_latex)
    prefix, latex = display.prefix, display.latex

    content_bottom = y
    max_lines = 6 if compact else 8
    answer_reserve = typo.answer * 0.95 + (0.8 * mm if compact else 1.6 * mm)
    formula_floor = card_bottom + answer_reserve

    if display.parts:
        content_bottom = _draw_mixed(
            c, display.parts, content_x, y, content_w, font, typo, formula_floor
        )
        c.setFont(font, typo.answer)
        answer_label = "Отв: ______" if compact else "Ответ: __________________"
        answer_y = content_bottom - answer_reserve
        if answer_y < card_bottom and content_bottom > card_bottom + typo.answer:
            answer_y = card_bottom
        c.drawString(content_x, answer_y, answer_label)
        return answer_y - typo.answer * 0.7 - typo.task_block_gap - 0.8 * mm

    if prefix:
        content_bottom = _wrap_plain_limited(
            c, prefix, content_x, y, content_w, font, typo.task, max_lines
        )
    if latex:
        formula_y = content_bottom if prefix else y
        drawn_bottom = _draw_latex_block(
            c,
            latex,
            content_x,
            formula_y,
            content_w,
            typo,
            below=bool(prefix),
            floor=formula_floor,
        )
        if drawn_bottom is not None:
            content_bottom = drawn_bottom
        else:
            text = latex_to_pdf_text(latex)
            content_bottom = _wrap_plain_limited(
                c,
                text,
                content_x,
                formula_y,
                content_w,
                font,
                typo.task,
                4,
            )
    elif not prefix:
        content_bottom = y - typo.task

    c.setFont(font, typo.answer)
    answer_label = "Отв: ______" if compact else "Ответ: __________________"
    answer_y = content_bottom - answer_reserve
    if answer_y < card_bottom and content_bottom > card_bottom + typo.answer:
        answer_y = card_bottom
    c.drawString(content_x, answer_y, answer_label)
    return answer_y - typo.answer * 0.7 - typo.task_block_gap - 0.8 * mm


def _wrap_plain_limited(
    c: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    max_w: float,
    font: str,
    size: float,
    max_lines: int,
    min_y: float | None = None,
) -> float:
    c.setFont(font, size)
    words = text.split()
    line = ""
    line_h = size * 1.18
    cy = y
    lines_used = 0
    for word in words:
        trial = f"{line} {word}".strip()
        if pdfmetrics.stringWidth(trial, font, size) <= max_w:
            line = trial
        else:
            if line:
                if min_y is not None and cy - line_h < min_y:
                    return min_y
                c.drawString(x, cy, line)
                cy -= line_h
                lines_used += 1
                if lines_used >= max_lines:
                    return cy
            line = word
    if line:
        if min_y is not None and cy - line_h < min_y:
            return min_y
        c.drawString(x, cy, line)
        cy -= line_h
    return cy


def _draw_dashed_rect(c: canvas.Canvas, x: float, y: float, w: float, h: float) -> None:
    c.saveState()
    c.setDash(3, 3)
    c.setStrokeColor(colors.grey)
    c.rect(x, y, w, h, stroke=1, fill=0)
    c.restoreState()


def _draw_card(
    c: canvas.Canvas,
    card_set: CardSet,
    variant_index: int,
    x: float,
    y: float,
    w: float,
    h: float,
    font: str,
) -> None:
    variant = card_set.variants[variant_index]
    subject = SUBJECT_LABELS.get(card_set.subject, card_set.subject)
    task_count = card_set.task_count or len(variant.tasks)
    typo = _typography(task_count, h)

    padding = 4 * mm
    inner_top = y + h - padding
    right_x = x + w - padding

    # Строка 1: сложность и вариант. Строка 2: предмет на всю ширину, без наезда на ФИО.
    diff_text = _card_difficulty_label(card_set)
    top_y = inner_top - 1 * mm
    c.setFont(font, typo.diff_label)
    c.drawString(x + padding, top_y, diff_text)

    c.setFont(font, typo.variant)
    vline = f"Вариант {variant.variant_id}"
    var_w = pdfmetrics.stringWidth(vline, font, typo.variant)
    c.drawString(right_x - var_w, top_y - 2 * mm, vline)

    subj_size = typo.subject * 0.95
    max_subj_w = w - 2 * padding
    subj_w = pdfmetrics.stringWidth(subject, font, subj_size)
    while subj_w > max_subj_w and subj_size > 6:
        subj_size *= 0.9
        subj_w = pdfmetrics.stringWidth(subject, font, subj_size)
    subj_y = top_y - max(typo.diff_label, typo.variant, subj_size) * 1.35
    c.setFont(font, subj_size)
    c.drawString(x + (w - subj_w) / 2, subj_y, subject)

    hy = subj_y - subj_size * 1.35
    c.setFont(font, typo.meta)
    meta_lines = ("ФИО: ______________", "Класс: ______")
    meta_w = max(pdfmetrics.stringWidth(hl, font, typo.meta) for hl in meta_lines)
    for hl in meta_lines:
        c.drawString(right_x - meta_w, hy, hl)
        hy -= max(typo.meta * 1.35, 3.4 * mm)

    card_bottom = y + padding
    task_y = hy - typo.task * 1.15
    compact = task_count >= 7
    cols = _task_columns(task_count)
    gutter = 3 * mm

    c.saveState()
    clip_x = x + padding
    clip_y = card_bottom
    clip_w = w - 2 * padding
    # До верха карточки: степень и индекс не срезаются границей строки.
    clip_h = max((y + h - padding) - card_bottom, 1.0)
    p = c.beginPath()
    p.rect(clip_x, clip_y, clip_w, clip_h)
    c.clipPath(p, stroke=0, fill=0)

    if cols == 2:
        col_w = (w - 2 * padding - gutter) / 2
        split = (len(variant.tasks) + 1) // 2
        left_tasks = variant.tasks[:split]
        right_tasks = variant.tasks[split:]
        col_top = y + h - padding
        for tasks, col_x in (
            (left_tasks, x + padding),
            (right_tasks, x + padding + col_w + gutter),
        ):
            c.saveState()
            col_path = c.beginPath()
            col_path.rect(col_x, card_bottom, col_w, max(col_top - card_bottom, 1))
            c.clipPath(col_path, stroke=0, fill=0)
            col_y = task_y
            for task in tasks:
                col_y = _draw_task(
                    c,
                    task,
                    col_x,
                    col_y,
                    col_w,
                    font,
                    typo,
                    card_bottom,
                    compact,
                )
            c.restoreState()
    else:
        for task in variant.tasks:
            task_y = _draw_task(
                c,
                task,
                x + padding,
                task_y,
                w - 2 * padding,
                font,
                typo,
                card_bottom,
                compact,
            )

    c.restoreState()


def build_pdf(card_set: CardSet) -> bytes:
    if len(card_set.variants) != 2:
        raise ValueError("На листе должно быть ровно 2 варианта")

    logger.info(
        "PDF: старт сборки subject=%s tasks=%s",
        card_set.subject,
        card_set.task_count,
    )
    font = _ensure_font()
    buffer = io.BytesIO()
    page_w, page_h = A4
    margin = 10 * mm
    c = canvas.Canvas(buffer, pagesize=A4)

    usable_w = page_w - 2 * margin
    usable_h = page_h - 2 * margin
    card_w = usable_w
    card_h = usable_h / 2
    for i, py in enumerate([margin + card_h, margin]):
        _draw_dashed_rect(c, margin, py, card_w, card_h)
        _draw_card(c, card_set, i, margin, py, card_w, card_h, font)
    c.setDash(3, 3)
    c.setStrokeColor(colors.grey)
    c.line(margin, margin + card_h, margin + usable_w, margin + card_h)

    c.showPage()
    c.save()
    logger.info("PDF: сборка завершена, bytes=%s", buffer.tell())
    return buffer.getvalue()


def save_pdf(card_set: CardSet, path: Path) -> Path:
    path.write_bytes(build_pdf(card_set))
    return path
