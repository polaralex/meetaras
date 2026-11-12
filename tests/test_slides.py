import pymupdf
import pytest

from meetaras.layouts import BACKGROUND_RGB
from meetaras.slides import page_count, page_titles, render_slides


@pytest.mark.slow
def test_render_slides(fixtures, tmp_path):
    pdf = fixtures / "deck.pdf"
    assert page_count(pdf) == 3
    paths = render_slides(pdf, tmp_path)
    assert [p.name for p in paths] == ["slide-001.png", "slide-002.png", "slide-003.png"]
    for p in paths:
        pix = pymupdf.Pixmap(str(p))
        assert (pix.width, pix.height) == (1920, 1080)

    # 16:9 page fills the frame (white corner); 4:3 page is letterboxed (background at the left edge)
    assert pymupdf.Pixmap(str(paths[0])).pixel(2, 540) == (255, 255, 255)
    assert pymupdf.Pixmap(str(paths[2])).pixel(2, 540) == BACKGROUND_RGB
    assert pymupdf.Pixmap(str(paths[2])).pixel(960, 540) == (255, 255, 255)


@pytest.mark.slow
def test_render_slides_cached(fixtures, tmp_path):
    first = render_slides(fixtures / "deck.pdf", tmp_path)
    mtime = first[0].stat().st_mtime_ns
    second = render_slides(fixtures / "deck.pdf", tmp_path)
    assert first == second and second[0].stat().st_mtime_ns == mtime


def test_page_titles(tmp_path):
    doc = pymupdf.open()
    p = doc.new_page(width=960, height=540)
    p.insert_text((60, 80), "Data Lake", fontsize=40)
    p.insert_text((60, 200), "Everything in one place", fontsize=20)
    p = doc.new_page(width=960, height=540)
    for i, line in enumerate(["- one", "- two", "- three"]):  # one size only: the first line
        p.insert_text((60, 80 + 40 * i), line, fontsize=24)
    doc.new_page(width=960, height=540)  # a picture only
    doc.save(tmp_path / "d.pdf")
    assert page_titles(tmp_path / "d.pdf") == ["Data Lake", "- one", ""]
