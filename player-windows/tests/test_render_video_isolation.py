"""Static regressions on the render-path contract (post-hotfix).

Background
==========

On Windows the DWM compositor does NOT alpha-blend sibling HWNDs in
the same top-level window. A ``QWebEngineView`` sitting above a
``QWidget``-hosting-libmpv HWND, even with ``WA_TranslucentBackground``
and a CSS-transparent body, occludes mpv completely. That's why the
first cut of Phase 2 Step 3 produced audio-only playback on real
Windows kiosks.

The revised architecture uses **exactly one visible surface at a
time**. Three siblings cover the whole window; two are hidden at any
moment. Visibility transitions go through three named helpers and
``_render`` calls exactly one per entry:

  * ``_switch_to_video()``   — video / stream
  * ``_switch_to_image()``   — image
  * ``_switch_to_overlay()`` — widget + placeholder

These tests lock that contract in source.
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


def _render_branch(kind: str) -> str:
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
# 1. Each _render branch must call exactly one _switch_to_*
# ---------------------------------------------------------------------------


_ALL_SWITCHES = ("_switch_to_video(", "_switch_to_image(", "_switch_to_overlay(")


def _switch_calls(body: str) -> list[str]:
    """Return the names of _switch_to_* calls appearing in body."""
    return [s for s in _ALL_SWITCHES if s in body]


def test_render_video_branch_uses_video_switch() -> None:
    body = _render_branch("video")
    assert "_switch_to_video(" in body, (
        "video branch must call _switch_to_video() to hide the "
        "WebEngine HWND before loadfile()."
    )
    assert "_switch_to_overlay(" not in body
    assert "_switch_to_image(" not in body


def test_render_stream_branch_uses_video_switch() -> None:
    body = _render_branch("stream")
    assert "_switch_to_video(" in body
    assert "_switch_to_overlay(" not in body
    assert "_switch_to_image(" not in body


def test_render_image_branch_uses_image_path() -> None:
    """The image branch goes through ``_show_image`` which internally
    hides competing surfaces and paints the label. Its own body must
    not talk to libmpv or the WebEngine."""
    body = _render_branch("image")
    assert "_show_image(" in body
    # Ensure it's NOT calling other destinations.
    assert "_show_widget(" not in body
    assert "_show_placeholder(" not in body


def test_render_widget_branch_uses_widget_path() -> None:
    body = _render_branch("widget")
    assert "_show_widget(" in body
    assert "_show_image(" not in body


def test_show_image_helper_hides_web_view() -> None:
    """``_show_image`` must route through ``_switch_to_image`` which
    hides the WebEngine overlay — otherwise a visible opaque web
    view would sit on top of the label."""
    body = _method_body("_show_image")
    assert "_switch_to_image(" in body


def test_show_widget_helper_routes_through_overlay() -> None:
    body = _method_body("_show_widget")
    assert "_switch_to_overlay(" in body


def test_show_placeholder_helper_routes_through_overlay() -> None:
    body = _method_body("_show_placeholder")
    assert "_switch_to_overlay(" in body


def test_switch_to_video_hides_competing_surfaces() -> None:
    """The whole point: make mpv visible by getting the other two
    HWNDs out of the compositor's way."""
    body = _method_body("_switch_to_video")
    assert "_web_view" in body
    assert "hide()" in body
    # And ensures video container is on.
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


def test_render_image_branch_does_not_touch_libmpv_or_web_view() -> None:
    """Images render on the QLabel layer exclusively. Any reference
    to libmpv or the web view from here would add cross-surface
    coupling that this architecture exists to avoid."""
    body = _render_branch("image")
    for forbidden in ("_mpv.", "_web_view.", "_paint_overlay(", "_show_widget("):
        assert forbidden not in body, (
            f"_render's image branch must not reference {forbidden!r}."
        )


def test_render_widget_branch_does_not_touch_libmpv_or_image_label() -> None:
    body = _render_branch("widget")
    for forbidden in ("_mpv.", "_image_label."):
        assert forbidden not in body, (
            f"_render's widget branch must not reference {forbidden!r}."
        )


def test_render_video_branch_does_not_paint_overlay_or_image() -> None:
    """Video playback must not queue a WebEngine paint or a label
    update. The switch helper hides the competing surfaces; the
    branch then only talks to libmpv."""
    body = _render_branch("video")
    for forbidden in (
        "_paint_overlay(",
        "_show_widget(",
        "_show_image(",
        "_load_image_on_label(",
        "_image_label.setPixmap(",
    ):
        assert forbidden not in body, (
            f"_render's video branch must not reference {forbidden!r}."
        )


def test_render_stream_branch_does_not_paint_overlay_or_image() -> None:
    body = _render_branch("stream")
    for forbidden in (
        "_paint_overlay(",
        "_show_widget(",
        "_show_image(",
        "_load_image_on_label(",
        "_image_label.setPixmap(",
    ):
        assert forbidden not in body, (
            f"_render's stream branch must not reference {forbidden!r}."
        )


# ---------------------------------------------------------------------------
# 2. Only the _switch_to_* helpers toggle child visibility
# ---------------------------------------------------------------------------


_VISIBILITY_METHOD_NAMES = (
    "_switch_to_video",
    "_switch_to_image",
    "_switch_to_overlay",
    "__init__",  # initial hide() calls are legitimate there
)


def _all_visibility_mutations(src: str) -> list[tuple[int, str]]:
    """Return the line number + trimmed text of every ``.show()`` /
    ``.hide()`` / ``.raise_()`` call in the source."""
    found: list[tuple[int, str]] = []
    pattern = re.compile(r"self\._\w+\.(?:show|hide|raise_)\(\s*\)")
    for i, line in enumerate(src.splitlines(), start=1):
        if pattern.search(line):
            found.append((i, line.strip()))
    return found


def test_visibility_mutations_are_inside_whitelisted_methods(src: str) -> None:
    """Any ``self._something.show()`` / ``hide()`` / ``raise_()`` call
    must live inside one of the whitelisted methods. This is the hard
    invariant that guarantees render branches can't accidentally
    reshuffle the compositor state."""
    mutations = _all_visibility_mutations(src)
    assert mutations, "expected at least one visibility call in player_ui.py"

    # Build a line-range for each whitelisted method body.
    ranges: list[tuple[str, int, int]] = []
    lines = src.splitlines()
    for method in _VISIBILITY_METHOD_NAMES:
        needle = f"def {method}("
        start = next(
            (i + 1 for i, ln in enumerate(lines) if ln.lstrip().startswith(needle)),
            None,
        )
        if start is None:
            continue
        # find end of method by walking until the next top-level def /
        # class at or below the def's own indent
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

    # resizeEvent also re-raises the overlay defensively; whitelist it.
    needle = "def resizeEvent("
    start = next(
        (i + 1 for i, ln in enumerate(lines) if ln.lstrip().startswith(needle)),
        None,
    )
    if start is not None:
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
        ranges.append(("resizeEvent", start, end))

    for line_num, text in mutations:
        allowed = any(start <= line_num < end for _, start, end in ranges)
        assert allowed, (
            f"visibility mutation on line {line_num} escapes the whitelist:\n"
            f"  {text}\n"
            f"Only {_VISIBILITY_METHOD_NAMES + ('resizeEvent',)} may call "
            f"show/hide/raise_."
        )


# ---------------------------------------------------------------------------
# 3. Structural invariants
# ---------------------------------------------------------------------------


def test_stack_widget_is_gone(src: str) -> None:
    """Phase 1 used QStackedWidget and we keep not using it."""
    stripped = _strip_comments_and_docstrings(src)
    assert "QStackedWidget" not in stripped


def test_image_label_exists(src: str) -> None:
    """Images render on a dedicated QLabel on Windows — routing them
    through the WebEngine view would pull that HWND into the
    compositor again."""
    assert "self._image_label = QLabel" in src


def test_close_event_drains_web_engine(src: str) -> None:
    body = _method_body("closeEvent")
    assert "clearHttpCache()" in body
    assert "deleteLater()" in body
    assert "gc.collect()" in body


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
