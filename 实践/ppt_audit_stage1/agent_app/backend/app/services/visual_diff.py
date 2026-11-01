"""Pixel-level proof that a repaired page actually looks different.

The geometry fingerprint in ``repair.py`` can only say "some attribute moved".
It cannot tell a real fix from a z-order reshuffle: lifting a text box that was
never painted over changes the XML, keeps every rule happy, and renders the
exact same picture.  Users read that as "the repair did nothing", correctly.

So the rendered before/after images are the authority.  A page only counts as
changed when enough pixels really differ; everything else is reported as
"no visible change" instead of "fixed".
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops

# A pixel counts as "really different" only above this channel delta, so that
# anti-aliasing and JPEG-free re-renders do not fabricate changes.
STRONG_DELTA = 24
# Both gates must pass: enough pixels *and* enough of the page.  Measured on a
# real deck, a pure z-order reshuffle lands at ~370 px / 0.018% (noise), while
# moving three small labels lands at ~1800 px / 0.087% (a real fix).
VISIBLE_MIN_PX = 600
VISIBLE_MIN_RATIO = 0.0002


def compare(before: Path | str, after: Path | str) -> dict:
    """Compare two renders of the same page.

    Returns ``{"ratio", "px", "mean", "visible"}`` where ``ratio`` is the share
    of strongly differing pixels (0-1) and ``visible`` says whether a human
    would notice the difference at all.
    """
    left = Image.open(before)
    right = Image.open(after)
    if left.size != right.size:  # render settings changed between runs
        right = right.resize(left.size)
    diff = ImageChops.difference(left.convert("RGB"), right.convert("RGB")).convert("L")
    histogram = diff.histogram()
    total = max(1, left.size[0] * left.size[1])
    strong = sum(histogram[STRONG_DELTA + 1:])
    ratio = strong / total
    return {
        "ratio": round(ratio, 6),
        "px": strong,
        "mean": round(sum(i * n for i, n in enumerate(histogram)) / total, 4),
        "visible": strong >= VISIBLE_MIN_PX and ratio >= VISIBLE_MIN_RATIO,
    }


def compare_pages(before_dir: Path, after_dir: Path, indexes=None) -> dict[int, dict]:
    """Compare every ``sNNNN.png`` pair; missing renders are simply skipped."""
    out: dict[int, dict] = {}
    for path in sorted(Path(after_dir).glob("s[0-9]*.png")):
        try:
            index = int(path.stem[1:])
        except ValueError:
            continue
        if indexes is not None and index not in indexes:
            continue
        original = Path(before_dir) / path.name
        if not original.is_file():
            continue
        try:
            out[index] = compare(original, path)
        except Exception:  # a broken PNG must not fail the whole repair
            continue
    return out


def describe(visual: dict) -> str:
    """One-line, human wording for the result page."""
    if not visual:
        return ""
    percent = visual["ratio"] * 100
    if visual["visible"]:
        return f"画面变化 {percent:.2f}%（{visual['px']} 像素）"
    return f"画面无可见变化（差异 {percent:.2f}%、{visual['px']} 像素，属抗锯齿级别）"
