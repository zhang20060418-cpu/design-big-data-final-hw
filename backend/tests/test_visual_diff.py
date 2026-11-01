"""Pixel proof: a repair only counts when the rendered page actually changes.

The z-order reshuffle case is the important one — it edits the XML, satisfies
every structure rule, and renders the exact same picture.  Users call that
"nothing happened", so the result page has to call it the same thing.

All images here are generated in a temp dir; nothing touches real task data.
"""
from __future__ import annotations

from PIL import Image, ImageDraw

from app.services import visual_diff
from app.services.repair import demote_invisible

W, H = 960, 540


def _page(path, blocks=()) -> str:
    image = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(image)
    for x, y, w, h in blocks:
        draw.rectangle([x, y, x + w, y + h], fill="black")
    image.save(path)
    return str(path)


def test_identical_renders_are_not_a_repair(tmp_path):
    before = _page(tmp_path / "before.png")
    after = _page(tmp_path / "after.png")

    result = visual_diff.compare(before, after)

    assert result["px"] == 0
    assert result["ratio"] == 0.0
    assert result["visible"] is False
    assert "无可见变化" in visual_diff.describe(result)


def test_a_real_edit_is_visible(tmp_path):
    before = _page(tmp_path / "before.png")
    after = _page(tmp_path / "after.png", blocks=[(120, 90, 200, 90)])

    result = visual_diff.compare(before, after)

    assert result["visible"] is True
    assert result["px"] >= visual_diff.VISIBLE_MIN_PX
    assert "画面变化" in visual_diff.describe(result)


def test_antialiasing_sized_noise_does_not_count(tmp_path):
    """A few hundred stray pixels are noise, not a fix (measured on real runs)."""
    before = _page(tmp_path / "before.png")
    after = _page(tmp_path / "after.png", blocks=[(300, 300, 12, 12)])

    assert visual_diff.compare(before, after)["visible"] is False


def test_compare_pages_skips_pages_without_a_before_render(tmp_path):
    before_dir = tmp_path / "before"
    after_dir = tmp_path / "after"
    before_dir.mkdir()
    after_dir.mkdir()
    _page(before_dir / "s0001.png")
    _page(after_dir / "s0001.png", blocks=[(10, 10, 200, 100)])
    _page(after_dir / "s0002.png")

    result = visual_diff.compare_pages(before_dir, after_dir)

    assert set(result) == {1}, "没有基准图的页不能假装比对过"


def test_invisible_change_is_demoted_not_reported_as_fixed():
    ops = [
        {"issue_id": "a", "slide_index": 5, "status": "applied",
         "detail": "把 16 个被压文字提到最上层，本页遮挡已清除。"},
        {"issue_id": "b", "slide_index": 6, "status": "applied", "detail": "文本框高度撑开 0.4in。"},
        {"issue_id": "c", "slide_index": 5, "status": "manual", "detail": "留待人工确认。"},
    ]
    visuals = {
        5: {"ratio": 0.0, "px": 0, "mean": 0.0, "visible": False},
        6: {"ratio": 0.008, "px": 4100, "mean": 1.2, "visible": True},
    }

    demoted = demote_invisible(ops, visuals)

    assert demoted == {5}
    assert ops[0]["status"] == "partial"
    assert "画面一致" in ops[0]["detail"] and "误报" in ops[0]["detail"]
    # 画面真的变了的页、以及本来就人工的条目，都不能被顺手改掉
    assert ops[1]["status"] == "applied"
    assert ops[2]["status"] == "manual" and ops[2]["detail"] == "留待人工确认。"
