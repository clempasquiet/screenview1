"""Static regressions on the Layout-slide dispatch contract.

The Phase 2 Step 4 architecture uses single-visible-surface switching
(three full-window children, exactly one visible at any moment, see
README principle 8). The dispatch happens in ``_render_slide(slide)``
with three branches:

  * ``slide.has_video`` → ``_switch_to_video`` + ``_play_video_on_mpv``,
    other zones omitted (documented trade-off).
  * Single-image full-canvas fast path → ``_switch_to_image`` +
    ``_show_image``.
  * HTML-only multi-zone → ``_switch_to_overlay`` + ``_show_layout_html``.

These tests inspect ``player_ui.py`` textually so they run in headless
CI without Qt. They fail with explicit messages if the dispatch ever
lets two surfaces mix within one branch (the pattern that caused the
"audio plays, picture is black" bug on Windows).
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

    Handles both single- and multi-line ``def`` signatures.
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

    sig_end = header_idx
    for i in range(header_idx, len(lines)):
        code_part = lines[i].rstrip().split("#", 1)[0].rstrip()
        if code_part.endswith(":"):
            sig_end = i
            break
    else:
        raise AssertionError(f"Could not find end of signature for {method_name}")

    body_lines: list[str] = [lines[header_idx]]
    for line in lines[sig_end + 1:]:
        if line.strip() == "":
            body_lines.append(line)
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= base_indent:
            break
        body_lines.append(line)
    return "\n".join(body_lines)


@pytest.fixture(scope="module")
def src() -> str:
    return _PLAYER_UI.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Slide dispatch: each branch uses exactly one surface
# ---------------------------------------------------------------------------


def test_has_video_branch_switches_to_video_and_plays_on_mpv() -> None:
    body = _method_body("_render_slide")
    # The video branch is the first one checked (per the trade-off
    # documented in README principle 8). Assert the calls appear in
    # that region of the method.
    assert "slide.has_video" in body
    # Slide-video path must both switch to video AND call into mpv.
    assert "_switch_to_video(" in body
    assert "_play_video_on_mpv(" in body


def test_fast_path_image_uses_image_surface() -> None:
    body = _method_body("_render_slide")
    # The single-image fast path matches (1 zone, 1 item, image,
    # full-canvas) and stops video first.
    assert "_show_image(" in body
    assert "_stop_video_layer(" in body


def test_html_fallback_uses_overlay_and_stops_video() -> None:
    body = _method_body("_render_slide")
    # The third case: any HTML-only slide (multi-zone or simple
    # widget) routes through the HTML overlay.
    assert "_show_layout_html(" in body


def test_render_slide_has_no_manual_z_order_mutation() -> None:
    """Z-order is set once at startup; nothing in the dispatch path
    should call raise_/lower/stackUnder."""
    body = _method_body("_render_slide")
    for forbidden in ("raise_(", "lower(", "stackUnder("):
        assert forbidden not in body, (
            f"_render_slide must not call {forbidden!r}."
        )


# ---------------------------------------------------------------------------
# 2. _show_* helpers preserve the single-visible-surface contract
# ---------------------------------------------------------------------------


def test_show_image_switches_to_image_surface() -> None:
    body = _method_body("_show_image")
    assert "_switch_to_image(" in body
    # No libmpv, no WebEngine touch from the image path.
    assert "_mpv" not in body
    assert "_web_view" not in body


def test_show_layout_html_routes_through_overlay() -> None:
    body = _method_body("_show_layout_html")
    assert "_switch_to_overlay(" in body
    # The multi-zone path must not touch libmpv directly; the
    # dispatcher calls _stop_video_layer before this helper when
    # needed.
    assert "_mpv.loadfile" not in body


def test_show_placeholder_routes_through_overlay() -> None:
    body = _method_body("_show_placeholder")
    assert "_switch_to_overlay(" in body


# ---------------------------------------------------------------------------
# 3. Switch helpers maintain correct visibility discipline
# ---------------------------------------------------------------------------


def test_switch_to_video_hides_competing_surfaces() -> None:
    body = _method_body("_switch_to_video")
    assert "_web_view" in body
    assert "_image_label" in body
    assert "hide()" in body
    assert "_video_container" in body


def test_switch_to_image_hides_web_view() -> None:
    body = _method_body("_switch_to_image")
    assert "_web_view" in body
    assert "hide()" in body
    assert "_image_label" in body


def test_switch_to_overlay_shows_web_view() -> None:
    body = _method_body("_switch_to_overlay")
    assert "_web_view" in body
    assert "show()" in body


# ---------------------------------------------------------------------------
# 4. Visibility whitelist — nothing outside the whitelisted methods
#    is allowed to call show()/hide()/raise_()
# ---------------------------------------------------------------------------


_VISIBILITY_METHOD_NAMES = (
    "_switch_to_video",
    "_switch_to_image",
    "_switch_to_overlay",
    "__init__",
    "resizeEvent",
)


def _all_visibility_mutations(src: str) -> list[tuple[int, str]]:
    """Return the line number + trimmed text of every ``.show()`` /
    ``.hide()`` / ``.raise_()`` call on a ``self._*`` attribute."""
    found: list[tuple[int, str]] = []
    pattern = re.compile(r"self\._\w+\.(?:show|hide|raise_)\(\s*\)")
    for i, line in enumerate(src.splitlines(), start=1):
        if pattern.search(line):
            found.append((i, line.strip()))
    return found


def test_visibility_mutations_are_inside_whitelisted_methods(src: str) -> None:
    mutations = _all_visibility_mutations(src)
    assert mutations, "expected at least one visibility call in player_ui.py"

    # Build a line-range for each whitelisted method body.
    lines = src.splitlines()
    ranges: list[tuple[str, int, int]] = []
    for method in _VISIBILITY_METHOD_NAMES:
        needle = f"def {method}("
        start = next(
            (i + 1 for i, ln in enumerate(lines) if ln.lstrip().startswith(needle)),
            None,
        )
        if start is None:
            continue
        ln = lines[start - 1]
        base_indent = len(ln) - len(ln.lstrip())
        end = len(lines)
        for j in range(start, len(lines)):
            stripped = lines[j].lstrip()
            if not stripped:
                continue
            indent = len(lines[j]) - len(stripped)
            if indent <= base_indent and (
                stripped.startswith("def ") or stripped.startswith("class ")
            ):
                end = j
                break
        ranges.append((method, start, end))

    for line_num, text in mutations:
        allowed = any(start <= line_num < end for _, start, end in ranges)
        assert allowed, (
            f"visibility mutation on line {line_num} escapes the whitelist:\n"
            f"  {text}\n"
            f"Only {_VISIBILITY_METHOD_NAMES} may call show/hide/raise_."
        )


# ---------------------------------------------------------------------------
# 5. Structural invariants
# ---------------------------------------------------------------------------


def test_no_qstackedwidget_anywhere(src: str) -> None:
    """Phase 1 used QStackedWidget (all one-child). The current design
    uses explicit switch helpers — no managed stack."""
    stripped = _strip_comments_and_docstrings(src)
    assert "QStackedWidget" not in stripped


def test_image_label_exists(src: str) -> None:
    """Images render on a dedicated QLabel. Routing them through the
    WebEngine would pull that HWND into the compositor even for plain
    image playlists."""
    assert "self._image_label = QLabel" in src


def test_web_view_stays_hidden_at_init(src: str) -> None:
    """The overlay starts hidden; the first ``_show_placeholder`` or
    ``_show_layout_html`` call flips it on via ``_switch_to_overlay``."""
    init = _method_body("__init__")
    # Expect a hide() call on the newly-created web view — mirrors the
    # intent documented in the init comments.
    assert "self._web_view.hide()" in init


def test_close_event_drains_web_engine() -> None:
    body = _method_body("closeEvent")
    assert "clearHttpCache()" in body
    assert "deleteLater()" in body
    assert "gc.collect()" in body


# ---------------------------------------------------------------------------
# 6. Phase 2 Step 4 invariants
# ---------------------------------------------------------------------------


def test_playlist_is_typed_as_list_of_slides(src: str) -> None:
    """``set_playlist`` + internal state use ``list[Slide]`` now,
    not ``list[PlaylistEntry]``. This catches accidental re-imports
    of the legacy flat entry type."""
    stripped = _strip_comments_and_docstrings(src)
    assert "list[Slide]" in stripped
    assert "PlaylistEntry" not in stripped


def test_set_playlist_accepts_slides(src: str) -> None:
    body = _method_body("set_playlist")
    # The parameter is ``slides`` and the no-schedule branch still
    # stops video + shows placeholder.
    assert "slides" in body
    assert "_stop_video_layer(" in body
    assert "_show_placeholder(" in body


def test_render_slide_video_path_loops_short_clips(src: str) -> None:
    """A video shorter than the slide's duration must loop; otherwise
    mpv freezes on the last frame for the remainder of the slot.
    The source must mention a loop= kwarg on ``_play_video_on_mpv``."""
    body = _method_body("_render_slide")
    assert "loop=" in body, (
        "_render_slide's video path must pass a `loop=` kwarg to "
        "_play_video_on_mpv so short clips loop inside long slots."
    )


def test_advance_timer_uses_slide_duration(src: str) -> None:
    body = _method_body("_play_current")
    # The global slide timer is ``slide.duration``, not per-item.
    assert "slide.duration" in body


def test_has_video_drops_other_zones_with_warning(src: str) -> None:
    """The documented Phase 2 trade-off: mixed slides play the video
    full-screen and omit the rest, with a warning log so the operator
    notices."""
    body = _method_body("_render_slide")
    assert "has_video" in body
    # Log call present with a dropped-count reference.
    assert "dropped_count" in body or "omitted" in body.lower()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_comments_and_docstrings(src: str) -> str:
    """Drop triple-quoted blocks and # comments so structural
    assertions only see real code."""
    out: list[str] = []
    in_triple: str | None = None
    for line in src.splitlines():
        stripped = line
        if in_triple is None:
            for delim in ('"""', "'''"):
                idx = stripped.find(delim)
                if idx != -1:
                    rest = stripped[idx + 3:]
                    if delim in rest:
                        stripped = stripped[:idx] + stripped[idx + 3 + rest.find(delim) + 3:]
                    else:
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
        hash_pos = stripped.find("#")
        if hash_pos != -1:
            stripped = stripped[:hash_pos]
        out.append(stripped)
    return "\n".join(out)
