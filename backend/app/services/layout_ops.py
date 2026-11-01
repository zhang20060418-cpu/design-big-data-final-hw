"""Geometry-only page clean-up used by the repair pass.

Two jobs, both deliberately conservative:

* **seat a text block in its card** — pulling an overflowing text box back onto
  the canvas is only half the fix: it usually still hangs off the rounded
  rectangle it was designed for.  Seating re-aligns it to the card's inner
  edges with an even padding, and grows the card when the text genuinely needs
  more room than the card currently has.
* **tidy a dense page** — remove block overlaps, snap a column to one left edge
  and pull runaway gaps back to a normal rhythm.

Nothing here rewrites text, changes a font size or deletes a shape: every
operation is a rectangle change that can be measured, and the caller re-parses
the deck afterwards to see whether the rule actually stopped firing.
"""
from __future__ import annotations

import math
import re

from pptx.enum.dml import MSO_FILL
from pptx.util import Emu

EMU_IN = 914400
MARGIN_IN = 0.03
MIN_GAP = 0.10          # 两块内容之间的最小呼吸距离
PAD = 0.12              # 文字相对衬底的内边距
FULL_BLEED = 0.55       # 面积超过画布 55% 的按背景/大底板处理，不参与整理
MAX_SHIFT = 0.80        # 单块一次最多挪动的距离，避免把版式改形
NEAR_CARD = 1.6         # 衬底中心与文字中心的允许距离（英寸）

CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


# ------------------------------------------------------------------- helpers
def text_of(shape) -> str:
    try:
        return shape.text_frame.text if shape.has_text_frame else ""
    except Exception:
        return ""


def need_height_in(text: str, size_pt: float, width_in: float) -> tuple[float, int]:
    """Rough height the paragraph needs at ``size_pt`` inside ``width_in``."""
    cjk = len(CJK.findall(text))
    other = max(0, len(re.sub(r"\s+", "", text)) - cjk)
    est_w = (cjk + other * 0.52) * size_pt
    inner_w = max(12.0, (width_in - 0.2) * 72)
    lines = max(1, math.ceil(est_w / inner_w))
    return (lines * size_pt * 1.32) / 72 + 0.16, lines


def box(leaf) -> dict:
    """Live rectangle in inches — reads the shape, not the parse-time snapshot."""
    shape = leaf.shape
    return {
        "x": (shape.left or 0) / EMU_IN,
        "y": (shape.top or 0) / EMU_IN,
        "w": (shape.width or 0) / EMU_IN,
        "h": (shape.height or 0) / EMU_IN,
    }


def area_of(rect: dict) -> float:
    return max(0.0, rect["w"]) * max(0.0, rect["h"])


def overlap(a: dict, b: dict) -> tuple[float, float]:
    return (max(0.0, min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"])),
            max(0.0, min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"])))


def inside_ratio(inner: dict, outer: dict) -> float:
    """How much of ``inner`` sits inside ``outer`` (0…1)."""
    ox, oy = overlap(inner, outer)
    return (ox * oy) / max(1e-6, area_of(inner))


def center(rect: dict) -> tuple[float, float]:
    return rect["x"] + rect["w"] / 2, rect["y"] + rect["h"] / 2


def filled(shape) -> bool:
    try:
        return shape.fill.type in (MSO_FILL.SOLID, MSO_FILL.GRADIENT, MSO_FILL.PATTERNED)
    except Exception:
        return False


def is_group_member(leaf) -> bool:
    return bool(getattr(leaf, "group_path", ()))


def is_decor(leaf) -> bool:
    rect = box(leaf)
    return rect["w"] < 0.30 or rect["h"] < 0.12


def is_content(leaf, page_area: float) -> bool:
    """Blocks worth moving: not grouped, not a hairline, not the background."""
    if is_group_member(leaf) or is_decor(leaf):
        return False
    return 0.02 * page_area <= area_of(box(leaf)) <= FULL_BLEED * page_area


def is_text_block(leaf) -> bool:
    return bool(text_of(leaf.shape).strip())


def inside_card(leaf, leaves, page_area: float) -> bool:
    """Is this block laid out *inside* a card?  Then the card owns its position.

    Nudging a caption that lives inside a card is how "整理" ends up pushing the
    text over the card's bottom edge — the card keeps its height, the text moves,
    and the result looks worse than before.
    """
    rect = box(leaf)
    own = area_of(rect)
    for other in leaves:
        if other is leaf or is_group_member(other) or not filled(other.shape):
            continue
        card = box(other)
        if area_of(card) <= own * 1.05:
            continue
        if area_of(card) > 0.85 * page_area:
            continue
        if inside_ratio(rect, card) >= 0.80:
            return True
    return False


def sizes_of(shape) -> list[float]:
    sizes = []
    try:
        for para in shape.text_frame.paragraphs:
            for run in para.runs:
                if run.font.size is not None and run.text.strip():
                    sizes.append(round(run.font.size.pt, 1))
    except Exception:
        pass
    return sizes


# ---------------------------------------------------------------- seating
def find_card(leaves, target, *, max_distance: float = NEAR_CARD,
              page_area: float | None = None):
    """The rounded panel / card a text block visually belongs to.

    A card is a filled, ungrouped shape that is clearly larger than the text,
    contains its centre, or sits right next to it (an overflowing block may have
    been pushed clean off its card before the repair ran).  The tightest such
    shape wins, so a nested panel beats the page-wide background.
    """
    target_rect = box(target)
    target_area = max(1e-6, area_of(target_rect))
    tx, ty = center(target_rect)
    best, best_score = None, 0.0
    for leaf in leaves:
        if leaf is target or is_group_member(leaf):
            continue
        if not filled(leaf.shape):
            continue
        rect = box(leaf)
        if area_of(rect) < max(target_area * 1.15, 0.90):
            continue
        if page_area and area_of(rect) > 0.85 * page_area:
            continue  # 铺满整页的底板不是「衬底卡片」
        cx, cy = center(rect)
        distance = math.hypot(cx - tx, cy - ty)
        if distance > max_distance:
            continue
        contains = 1.0 if inside_ratio(target_rect, rect) >= 0.98 else 0.0
        score = contains * 2.0 + inside_ratio(target_rect, rect) + (1.0 - distance / max_distance)
        if score > best_score:
            best, best_score = leaf, score
    return best


def free_below(leaves, card, slide_h: float) -> float:
    """Vertical room under the card before it would touch the next block."""
    rect = box(card)
    limit = slide_h - MARGIN_IN
    free = limit - (rect["y"] + rect["h"])
    for leaf in leaves:
        if leaf is card or is_group_member(leaf):
            continue
        other = box(leaf)
        if other["x"] + other["w"] <= rect["x"] + 0.05:
            continue
        if other["x"] >= rect["x"] + rect["w"] - 0.05:
            continue
        if other["y"] >= rect["y"] + rect["h"] - 0.05:
            free = min(free, other["y"] - (rect["y"] + rect["h"]) - MIN_GAP)
    return max(0.0, free)


def _collides(leaves, target, rect: dict) -> bool:
    """Would the candidate rectangle land on another text block?"""
    for leaf in leaves:
        if leaf is target or is_group_member(leaf) or not is_text_block(leaf):
            continue
        ox, oy = overlap(rect, box(leaf))
        if ox * oy >= 0.25 * min(area_of(rect), area_of(box(leaf))):
            return True
    return False


def seat_in_card(leaves, target, card, slide_h: float) -> tuple[str, str]:
    """Put one text block inside its card with even padding.  Returns status/detail."""
    shape = target.shape
    rect, card_rect = box(target), box(card)
    pad = PAD if card_rect["w"] >= 2.4 else 0.08
    inner = {"x": card_rect["x"] + pad, "y": card_rect["y"] + pad,
             "w": card_rect["w"] - 2 * pad, "h": card_rect["h"] - 2 * pad}
    if inner["w"] < 0.35 or inner["h"] < 0.10:
        return "skipped", "衬底太小，放不下这段文字。"

    size = max(sizes_of(shape) or [12.0])
    new_w = min(rect["w"], inner["w"])
    if rect["w"] > inner["w"] and inner["w"] < 0.50 * rect["w"]:
        # 衬底只有文字的一半宽，多半不是它的容器（可能是同在附近的小色块）
        return "skipped", ""
    need, _lines = need_height_in(text_of(shape), size, max(0.2, new_w))
    grown = 0.0
    if need > inner["h"] + 0.02:
        room = free_below(leaves, card, slide_h)
        grow = min(need - inner["h"], room, 1.20)
        if grow > 0.02:
            card.shape.height = Emu(int((card_rect["h"] + grow) * EMU_IN))
            card_rect["h"] += grow
            inner["h"] += grow
            grown = grow

    new_h = min(max(min(rect["h"], inner["h"]), min(need, inner["h"])), inner["h"])
    # 保持相对位置：卡片里常常还叠着别的文字，居中会把它们摞到一起
    nx = min(max(rect["x"], inner["x"]), inner["x"] + inner["w"] - new_w)
    ny = min(max(rect["y"], inner["y"]), inner["y"] + inner["h"] - new_h)
    if _collides(leaves, target, {"x": nx, "y": ny, "w": new_w, "h": new_h}):
        return "skipped", ""

    dx, dy = nx - rect["x"], ny - rect["y"]
    dw, dh = new_w - rect["w"], new_h - rect["h"]
    if max(abs(dx), abs(dy), abs(dw), abs(dh)) <= 0.02:
        return "skipped", ""
    shape.left = Emu(int(nx * EMU_IN))
    shape.top = Emu(int(ny * EMU_IN))
    shape.width = Emu(int(new_w * EMU_IN))
    shape.height = Emu(int(new_h * EMU_IN))

    parts = []
    if abs(dx) > 0.02:
        parts.append(f"水平 {dx:+.2f}in")
    if abs(dy) > 0.02:
        parts.append(f"垂直 {dy:+.2f}in")
    if abs(dw) > 0.02:
        parts.append(f"宽度 {rect['w']:.2f} → {new_w:.2f}in")
    if abs(dh) > 0.02:
        parts.append(f"高度 {rect['h']:.2f} → {new_h:.2f}in")
    detail = (f"收进衬底「{card.shape.name}」：{'、'.join(parts)}，"
              f"距内缘至少 {pad:.2f}in")
    if grown:
        detail += f"；衬底下延 {grown:.2f}in 以容纳正文"
    return "applied", detail + "。"


def seat_blocks(leaves, targets, slide_h: float, page_area: float | None = None,
                limit: int = 12) -> list[str]:
    """Seat several text blocks; returns one sentence per block that moved."""
    notes: list[str] = []
    for leaf in targets[:limit]:
        if leaf.group_path or not is_text_block(leaf):
            continue
        card = find_card(leaves, leaf, page_area=page_area)
        if card is None:
            continue
        try:
            status, detail = seat_in_card(leaves, leaf, card, slide_h)
        except Exception:
            continue
        if status == "applied" and detail:
            notes.append(f"把「{leaf.shape.name}」{detail}")
    return notes


# ------------------------------------------------------------------- tidy
def _column_of(columns: list[dict], rect: dict) -> dict | None:
    for column in columns:
        head = column["rect"]
        ox, _oy = overlap(head, rect)
        if ox >= 0.5 * min(head["w"], rect["w"]):
            return column
    return None


def page_blocks(leaves, page_area: float) -> list:
    """Blocks the tidy pass may move: page-level text only.

    Background panels, cards and pictures are *containers*: they hold other
    objects, so nudging them (or measuring overlaps against them) drags the
    whole page around.  Text that lives inside a card is also skipped — the card
    owns its position, and moving the text alone pushes it over the card edge.
    """
    return [leaf for leaf in leaves
            if is_text_block(leaf) and is_content(leaf, page_area)
            and not inside_card(leaf, leaves, page_area)]


def stray_blocks(leaves, page_area: float, low: float = 0.55, high: float = 0.99) -> list:
    """Text blocks that hang over the edge of their own card.

    No structure rule measures this: the box is still on the canvas, the text is
    still inside its frame — it just spills over the card it was designed for.
    That is the "改完了却还是不像样" case the tidy pass exists for.

    Only blocks that are *mostly* on the card qualify.  Something sitting a full
    inch below a card is not a spill — it is a different element (a table cell, a
    legend line) and moving it into the nearest card would eat real content.
    """
    out = []
    for leaf in leaves:
        if not is_text_block(leaf) or is_group_member(leaf):
            continue
        card = find_card(leaves, leaf, page_area=page_area)
        if card is None:
            continue
        ratio = inside_ratio(box(leaf), box(card))
        if low <= ratio < high:
            out.append(leaf)
    return out


def tidy(leaves, slide_w: float, slide_h: float, mode: str = "cluttered") -> list[str]:
    """Normalise a dense (or very airy) page without touching text or fonts.

    ``cluttered``: seat text that spills over its card, lift blocks that sit on
    top of each other, snap a column to a single left edge.
    ``sparse``: centre the text group vertically — only on pages without any
    filled panel, because moving text out of its card would look broken.

    Gap "balancing" is deliberately not done: pulling a wide footnote up between
    two columns looks worse than the gap it removes.
    """
    notes: list[str] = []
    page_area = slide_w * slide_h

    strays = stray_blocks(leaves, page_area)
    if strays:
        notes.extend(seat_blocks(leaves, strays, slide_h, page_area, limit=6))

    blocks = page_blocks(leaves, page_area)
    if len(blocks) >= 2:
        if mode == "cluttered":
            notes.extend(_separate(blocks, slide_h))
            notes.extend(_align_columns(blocks, slide_w))
        else:
            panels = [leaf for leaf in leaves
                      if not is_group_member(leaf) and filled(leaf.shape)
                      and 0.15 * page_area <= area_of(box(leaf)) <= 0.85 * page_area]
            if not panels:
                notes.extend(_center_group(blocks, slide_h))
    return notes[:12]


def _separate(blocks: list, slide_h: float) -> list[str]:
    """Push the lower of two overlapping blocks down, keeping a 0.10in gap.

    A block can be nudged in several rounds as its neighbours move, so the
    distances are summed per shape and reported once.
    """
    shifts: dict[str, float] = {}
    for _ in range(3):
        moved = False
        ordered = sorted(blocks, key=lambda leaf: (box(leaf)["y"], box(leaf)["x"]))
        for position, first in enumerate(ordered):
            a = box(first)
            for second in ordered[position + 1:]:
                b = box(second)
                ox, oy = overlap(a, b)
                if ox <= 0.02 or oy <= 0.02:
                    continue
                if ox < 0.25 * min(a["w"], b["w"]):
                    continue
                push = min(oy + MIN_GAP, MAX_SHIFT)
                if b["y"] + b["h"] + push > slide_h - MARGIN_IN:
                    continue
                try:
                    second.shape.top = Emu(int((b["y"] + push) * EMU_IN))
                except Exception:
                    continue
                shifts[second.shape.name] = shifts.get(second.shape.name, 0.0) + push
                moved = True
                break
            if moved:
                break
        if not moved:
            break
    return [f"把「{name}」下移 {shift:.2f}in，与上方对象拉开 {MIN_GAP:.2f}in"
            for name, shift in shifts.items()]


def _align_columns(blocks: list, slide_w: float) -> list[str]:
    """Snap a column to one left edge; full-width blocks are not a column."""
    notes: list[str] = []
    columns: list[dict] = []
    for leaf in sorted(blocks, key=lambda item: box(item)["x"]):
        rect = box(leaf)
        if rect["w"] >= 0.90 * slide_w:
            continue
        column = _column_of(columns, rect)
        if column is None:
            columns.append({"rect": rect, "items": [leaf]})
        else:
            column["items"].append(leaf)
    for column in columns:
        items = column["items"]
        if len(items) < 2:
            continue
        lefts = [round(box(item)["x"], 2) for item in items]
        target = max(lefts, key=lambda value: sum(1 for other in lefts if abs(other - value) <= 0.06))
        for leaf, value in zip(items, lefts):
            delta = target - value
            if not 0.02 < abs(delta) <= 0.25:
                continue
            try:
                leaf.shape.left = Emu(int(target * EMU_IN))
            except Exception:
                continue
            notes.append(f"把「{leaf.shape.name}」左边缘对齐到 {target:.2f}in（原 {value:.2f}in）")
    return notes


def _center_group(blocks: list, slide_h: float) -> list[str]:
    rects = [box(leaf) for leaf in blocks]
    top = min(rect["y"] for rect in rects)
    bottom = max(rect["y"] + rect["h"] for rect in rects)
    available = slide_h - 2 * MARGIN_IN
    height = bottom - top
    if height <= 0 or height >= available * 0.92:
        return []
    target_top = MARGIN_IN + (available - height) / 2
    shift = target_top - top
    if abs(shift) <= 0.10 or abs(shift) > 1.2:
        return []
    moved = 0
    for leaf in blocks:
        rect = box(leaf)
        try:
            leaf.shape.top = Emu(int((rect["y"] + shift) * EMU_IN))
            moved += 1
        except Exception:
            continue
    if not moved:
        return []
    return [f"把内容整体{'下移' if shift > 0 else '上移'} {abs(shift):.2f}in，"
            f"让内容区在页面中垂直居中"]
