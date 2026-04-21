"""Static regressions on the render-path contract.

Background
==========

The Phase 2 player uses a **layered** UI: libmpv sits on Layer 0 and a
transparent ``QWebEngineView`` sits on Layer 1 permanently above it.
Either surface can be mutated independently, but mixing them from a
single render branch reintroduces the class of Windows-specific bugs
that bit us during Phase 1 ("audio plays, picture is black"):

  * Touching the web view with a new URL/load during the video or
    stream branch wakes the Chromium compositor and can reshuffle
    the native HWND z-order on Windows, occluding libmpv.
  * Reordering the child widgets at runtime (``raise_()``, ``lower()``,
    ``show()``, ``hide()``) similarly disturbs the compositor and is
    forbidden outside the dedicated ``resizeEvent`` +  ``__init__``
    paths that set the layering up **once** per surface.

These tests inspect ``player_ui.py`` textually (no Qt import needed)
and fail with an explicit message if any rule above is violated. They
are the guard-rails that let future refactors stay safe without
re-learning the bug.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


_PLAYER_UI = Path(__file__).resolve().parent.parent / "player_ui.py"


# ---------------------------------------------------------------------------
# Source extraction helpers
# ---------------------------------------------------------------------------


def _method_body(method_name: str) -> str:
    """Return the indented body of ``PlayerWindow.<method_name>``.

    Handles both single- and multi-line ``def`` signatures. We find
    the header line, then skip over any signature continuation lines
    (they share the method's own indentation level — a closing ``)``
    on its own line looks just like the next method to a naive
    parser). The body is everything strictly more indented than the
    header, starting at the line right after ``def …: …``.
    """
    src = _PLAYER_UI.read_text(encoding="utf-8")
    lines = src.splitlines()
    header_idx = -1
    base_indent = 0
    needle = f"def {method_name}("
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(needle):
            header_idx = i
            base_indent = len(line) - len(stripped)
            break
    assert header_idx >= 0, f"Could not find PlayerWindow.{method_name}"

    # Walk past the signature until we hit a line that ends the ``def``
    # with a trailing ``:`` (possibly followed by a comment). This is
    # fragile on exotic formatting but matches black / ruff output.
    sig_end = header_idx
    for i in range(header_idx, len(lines)):
        stripped_trail = lines[i].rstrip()
        # Strip a trailing inline comment if present.
        code_part = stripped_trail.split("#", 1)[0].rstrip()
        if code_part.endswith(":"):
            sig_end = i
            break
    else:
        raise AssertionError(f"Could not find end of signature for {method_name}")

    body_lines: list[str] = [lines[header_idx]]  # header line for context
    for line in lines[sig_end + 1:]:
        if line.strip() == "":
            body_lines.append(line)
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= base_indent:
            break
        body_lines.append(line)
    return "\n".join(body_lines)


def _render_branch(kind: str) -> str:
    """Extract the body of the ``if entry.kind == "<kind>":`` block
    inside ``_render``."""
    render = _method_body("_render")
    pattern = re.compile(
        r'if entry\.kind == "' + re.escape(kind) + r'":(.*?)(?=\n\s*if entry\.kind ==|\Z)',
        re.DOTALL,
    )
    match = pattern.search(render)
    assert match, f"branch for kind={kind!r} not found in _render"
    return match.group(1)


@pytest.fixture(scope="module")
def src() -> str:
    return _PLAYER_UI.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Video / stream paths must not call the widget web-view APIs
# ---------------------------------------------------------------------------


# Any *call* against ``self._web_view`` that would load a new document
# is forbidden from inside the video / stream branches. We allow
# ``_clear_overlay_for_video`` (goes through ``_paint_overlay``, which
# uses ``setHtml`` with a purely transparent document — NOT a file
# URL, and does not reshuffle the native z-order).
_FORBIDDEN_WIDGET_CALLS = (
    "_web_view.load(",
    "_web_view.setUrl(",
    "_show_widget(",
    "_show_image(",
)

# Any call that would reshuffle the native z-order at runtime. ``raise_``
# is allowed in ``__init__`` (initial setup) and ``resizeEvent``
# (defensive); forbidden everywhere else.
_Z_ORDER_CALLS = ("raise_(", "lower(", "stackUnder(")


def test_render_video_branch_does_not_load_widget_content() -> None:
    body = _render_branch("video")
    for forbidden in _FORBIDDEN_WIDGET_CALLS:
        assert forbidden not in body, (
            f"_render's video branch must not call {forbidden!r}. "
            "Loading widget content from the video path reshuffles the "
            "native HWND z-order on Windows and hides the libmpv video."
        )


def test_render_stream_branch_does_not_load_widget_content() -> None:
    body = _render_branch("stream")
    for forbidden in _FORBIDDEN_WIDGET_CALLS:
        assert forbidden not in body, (
            f"_render's stream branch must not call {forbidden!r}."
        )


def test_render_video_branch_does_not_reorder_layers() -> None:
    body = _render_branch("video")
    for forbidden in _Z_ORDER_CALLS:
        assert forbidden not in body, (
            f"_render's video branch must not call {forbidden!r}. "
            "Layer stacking is configured once in __init__ and "
            "refreshed in resizeEvent; nowhere else."
        )


def test_render_stream_branch_does_not_reorder_layers() -> None:
    body = _render_branch("stream")
    for forbidden in _Z_ORDER_CALLS:
        assert forbidden not in body, (
            f"_render's stream branch must not call {forbidden!r}."
        )


# ---------------------------------------------------------------------------
# 2. Image / widget paths must not call libmpv
# ---------------------------------------------------------------------------


_LIBMPV_CALLS = ("_mpv.loadfile(", "_mpv.pause", "_mpv.command(")


def test_render_image_branch_does_not_touch_libmpv() -> None:
    body = _render_branch("image")
    for forbidden in _LIBMPV_CALLS:
        assert forbidden not in body, (
            f"_render's image branch must not touch libmpv ({forbidden!r}). "
            "Images render via the HTML overlay; touching libmpv here "
            "would duplicate state between the two layers."
        )


def test_render_widget_branch_does_not_touch_libmpv() -> None:
    body = _render_branch("widget")
    for forbidden in _LIBMPV_CALLS:
        assert forbidden not in body, (
            f"_render's widget branch must not touch libmpv ({forbidden!r})."
        )


# ---------------------------------------------------------------------------
# 3. Layer construction invariants
# ---------------------------------------------------------------------------


def test_web_view_is_raised_once_in_init(src: str) -> None:
    """Layer 1 (web view) must be raised above Layer 0 (video) exactly
    once in ``__init__`` — any subsequent raise_() call at runtime
    would signal the compositor that the view needs to be on top,
    which is what we want to achieve statically."""
    init_body = _method_body("__init__")
    assert init_body.count("_web_view.raise_(") == 1, (
        "__init__ must call self._web_view.raise_() exactly once to "
        "place the overlay above libmpv at startup."
    )


def test_resize_refreshes_geometry(src: str) -> None:
    """The overlay pair must fill the window at all times. Geometry
    is set in resizeEvent; the test pins the presence of both calls
    so a refactor can't silently break fullscreen."""
    body = _method_body("resizeEvent")
    assert "_video_container.setGeometry(" in body, (
        "resizeEvent must resize the video container to fill the window."
    )
    assert "_web_view.setGeometry(" in body, (
        "resizeEvent must resize the web overlay to fill the window."
    )


def test_stack_widget_is_gone(src: str) -> None:
    """The Phase 1 architecture used ``QStackedWidget`` to swap between
    children; Phase 2 replaces that with a permanent layered design.
    If ``QStackedWidget`` reappears in code (not comments) we've
    regressed.

    Strategy: strip docstrings + ``#`` comments from the source and
    check the stripped text. Cheap approximation — anything mentioning
    the symbol in executable Python should trip this.
    """
    stripped = _strip_comments_and_docstrings(src)
    assert "QStackedWidget" not in stripped, (
        "QStackedWidget must not reappear as real code in player_ui.py "
        "— the Phase 2 layered design relies on both surfaces being "
        "visible together."
    )


def _strip_comments_and_docstrings(src: str) -> str:
    """Rudimentary stripper: drop triple-quoted blocks and # comments.

    Good enough for the architectural-invariants tests. We don't try
    to handle edge cases like strings that happen to contain ``#`` —
    those would at worst produce a false positive, which a maintainer
    reading the failure message can then dismiss.
    """
    out: list[str] = []
    in_triple: str | None = None
    for line in src.splitlines():
        stripped = line
        if in_triple is None:
            # Start of a docstring?
            for delim in ('"""', "'''"):
                idx = stripped.find(delim)
                if idx != -1:
                    # Check if it also closes on the same line
                    rest = stripped[idx + 3:]
                    if delim in rest:
                        # Inline triple-quoted string; drop it.
                        stripped = stripped[:idx] + stripped[idx + 3 + rest.find(delim) + 3:]
                    else:
                        # Multi-line; enter triple mode.
                        stripped = stripped[:idx]
                        in_triple = delim
                    break
        else:
            idx = stripped.find(in_triple)
            if idx != -1:
                stripped = stripped[idx + 3:]
                in_triple = None
            else:
                continue
        # Strip inline ``#`` comments (naive: we ignore ``#`` inside
        # strings, which is fine for our source).
        hash_pos = stripped.find("#")
        if hash_pos != -1:
            stripped = stripped[:hash_pos]
        out.append(stripped)
    return "\n".join(out)


def test_web_view_has_translucent_background(src: str) -> None:
    """The whole point of the overlay is its transparency. If the
    attribute disappears, the overlay would paint opaque and hide
    the video entirely."""
    assert "WA_TranslucentBackground" in src, (
        "QWebEngineView must have WA_TranslucentBackground set so the "
        "overlay is actually see-through above the video surface."
    )


# ---------------------------------------------------------------------------
# 4. Final teardown still clears Chromium state
# ---------------------------------------------------------------------------


def test_close_event_drains_web_engine(src: str) -> None:
    """The closeEvent cleanup added in Phase 1 Fix 3 must remain in
    place — it's what frees Chromium's persistent state on a 24/7
    kiosk at process exit."""
    body = _method_body("closeEvent")
    assert "clearHttpCache()" in body
    assert "deleteLater()" in body
    assert "gc.collect()" in body
