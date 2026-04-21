"""Unit tests for the HTML layout renderer (``player-windows/layout_html.py``).

The module is PyQt-free by design so every case runs in headless CI
without a display server. We assert on the generated HTML text, not on
browser behaviour: the goal is to lock the **structure** of the
overlay (transparent root, absolute-positioned zones, escaped
user-supplied strings) so a future refactor can't silently break the
contract the ``QWebEngineView`` relies on.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest


_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from layout_html import (  # noqa: E402
    ensure_absolute_url,
    render_layout_html,
    render_placeholder_layout,
    render_single_image_layout,
)


# ---------------------------------------------------------------------------
# render_layout_html core
# ---------------------------------------------------------------------------


def test_empty_layout_produces_transparent_document() -> None:
    """A zero-zone layout is the "transparent overlay" state used when
    the video layer is the thing actually on screen. The HTML must
    declare ``background: transparent`` on html+body so the video
    shows through. No zones, no visible pixels beyond the transparent
    backdrop."""
    doc = render_layout_html({"resolution_w": 1920, "resolution_h": 1080, "zones": []})
    assert "<!doctype html>" in doc
    assert "background: transparent" in doc
    # No zone divs.
    assert 'class="screenview-zone"' not in doc


def test_canvas_dimensions_are_emitted_in_css_and_js() -> None:
    doc = render_layout_html({"resolution_w": 3840, "resolution_h": 2160, "zones": []})
    # CSS rule must use the canvas size (used for the scaling transform).
    assert "width: 3840px" in doc
    assert "height: 2160px" in doc
    # JS constants (used by the resize handler) must match.
    assert "var CANVAS_W = 3840;" in doc
    assert "var CANVAS_H = 2160;" in doc


def test_non_positive_resolution_raises() -> None:
    with pytest.raises(ValueError):
        render_layout_html({"resolution_w": 0, "resolution_h": 1080, "zones": []})
    with pytest.raises(ValueError):
        render_layout_html({"resolution_w": 1920, "resolution_h": -10, "zones": []})


def test_image_zone_emits_absolute_positioned_img() -> None:
    doc = render_layout_html(
        {
            "resolution_w": 1920,
            "resolution_h": 1080,
            "zones": [
                {
                    "id": "photo",
                    "kind": "image",
                    "position_x": 100,
                    "position_y": 200,
                    "width": 800,
                    "height": 600,
                    "z_index": 5,
                    "src": "file:///C:/a.png",
                }
            ],
        }
    )
    assert 'data-zone-id="photo"' in doc
    assert 'data-zone-kind="image"' in doc
    # Positioning CSS must be present.
    assert "left:100px;top:200px;" in doc
    assert "width:800px;height:600px;" in doc
    assert "z-index:5;" in doc
    # And the actual <img>.
    assert '<img class="zone-image" src="file:///C:/a.png"' in doc


def test_widget_zone_sandboxes_the_iframe() -> None:
    """Widget zones render as ``<iframe sandbox="…">`` so operator
    content can't do top-navigation, popups or form submits."""
    doc = render_layout_html(
        {
            "resolution_w": 1920,
            "resolution_h": 1080,
            "zones": [
                {
                    "id": "clock",
                    "kind": "widget",
                    "position_x": 0,
                    "position_y": 0,
                    "width": 480,
                    "height": 120,
                    "z_index": 10,
                    "src": "http://example.com/clock.html",
                }
            ],
        }
    )
    assert 'class="zone-widget"' in doc
    assert 'src="http://example.com/clock.html"' in doc
    assert 'sandbox="allow-scripts allow-same-origin"' in doc
    # referrerpolicy is a privacy best-practice; pin it.
    assert 'referrerpolicy="no-referrer"' in doc


def test_widget_zone_inline_html_is_embedded_verbatim() -> None:
    doc = render_layout_html(
        {
            "resolution_w": 1920,
            "resolution_h": 1080,
            "zones": [
                {
                    "id": "ticker",
                    "kind": "widget",
                    "position_x": 0,
                    "position_y": 1000,
                    "width": 1920,
                    "height": 80,
                    "z_index": 20,
                    "html": "<marquee>Latest news — breaking</marquee>",
                }
            ],
        }
    )
    assert "<marquee>Latest news — breaking</marquee>" in doc


def test_text_zone_escapes_user_supplied_strings() -> None:
    """Operators can drop plain text into a text zone. The renderer
    must HTML-escape it so ``"</div><script>alert(1)</script>"``
    cannot inject markup into the overlay."""
    malicious = '</div><script>alert("xss")</script>'
    doc = render_layout_html(
        {
            "resolution_w": 1920,
            "resolution_h": 1080,
            "zones": [
                {
                    "id": "text1",
                    "kind": "text",
                    "position_x": 0,
                    "position_y": 0,
                    "width": 100,
                    "height": 50,
                    "text": malicious,
                }
            ],
        }
    )
    # The <script> should appear *escaped* (as text), never as markup.
    assert "<script>alert" not in doc
    assert "&lt;script&gt;alert" in doc


def test_zone_id_is_attribute_escaped() -> None:
    """Zone ids flow into a ``data-zone-id`` attribute and must not
    be able to close out of the attribute quoting."""
    doc = render_layout_html(
        {
            "resolution_w": 1920,
            "resolution_h": 1080,
            "zones": [
                {
                    "id": '"><img onerror=alert(1)>',
                    "kind": "text",
                    "position_x": 0,
                    "position_y": 0,
                    "width": 10,
                    "height": 10,
                    "text": "ok",
                }
            ],
        }
    )
    assert "<img onerror=" not in doc
    assert "&quot;" in doc or "&#x22;" in doc


def test_zones_of_unknown_kind_are_rendered_as_empty() -> None:
    """Unknown zone kinds should not crash the whole layout. We emit
    the outer wrapper (so other zones keep their numbering) with no
    content, matching the documented ``empty`` fallback."""
    doc = render_layout_html(
        {
            "resolution_w": 1920,
            "resolution_h": 1080,
            "zones": [
                {
                    "id": "mystery",
                    "kind": "something-new",
                    "position_x": 0,
                    "position_y": 0,
                    "width": 10,
                    "height": 10,
                    "z_index": 0,
                }
            ],
        }
    )
    # The wrapper is there…
    assert 'data-zone-id="mystery"' in doc
    # …but no <img> / <iframe> / text inside.
    zone_block = re.search(
        r'<div class="screenview-zone" data-zone-id="mystery"[^>]*>(.*?)</div>',
        doc,
        re.DOTALL,
    )
    assert zone_block is not None
    assert zone_block.group(1).strip() == ""


# ---------------------------------------------------------------------------
# render_single_image_layout / render_placeholder_layout
# ---------------------------------------------------------------------------


def test_single_image_layout_is_a_single_zone() -> None:
    doc = render_single_image_layout("file:///srv/media/a.jpg", resolution=(1920, 1080))
    assert doc.count('class="screenview-zone"') == 1
    assert "file:///srv/media/a.jpg" in doc


def test_placeholder_layout_contains_brand_and_message() -> None:
    doc = render_placeholder_layout("Network unreachable", title="ScreenView")
    assert "ScreenView" in doc
    assert "Network unreachable" in doc
    # Placeholder is opaque so it hides any stale video underneath.
    assert "#0f1115" in doc


def test_placeholder_layout_escapes_the_message() -> None:
    doc = render_placeholder_layout("<script>oops</script>")
    assert "<script>oops" not in doc
    assert "&lt;script&gt;oops" in doc


# ---------------------------------------------------------------------------
# ensure_absolute_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "given, expected",
    [
        ("http://example.com/x", "http://example.com/x"),
        ("https://example.com/x", "https://example.com/x"),
        ("file:///C:/foo.jpg", "file:///C:/foo.jpg"),
        ("data:image/png;base64,abcd", "data:image/png;base64,abcd"),
        ("C:\\foo\\bar.jpg", "file:///C:/foo/bar.jpg"),
        ("/srv/media/a.jpg", "file:///srv/media/a.jpg"),
        ("\\\\server\\share\\clip.mp4", "file://server/share/clip.mp4"),
    ],
)
def test_ensure_absolute_url_converts_paths(given: str, expected: str) -> None:
    assert ensure_absolute_url(given) == expected


def test_ensure_absolute_url_rejects_empty_input() -> None:
    with pytest.raises(ValueError):
        ensure_absolute_url("")
    with pytest.raises(ValueError):
        ensure_absolute_url("   ")
