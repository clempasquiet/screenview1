"""Static regressions on the render-path contract.

The Phase 2 architecture (post user-directed fix) uses a
``QStackedLayout(StackingMode.StackAll)`` to compose libmpv on the
bottom layer and a transparent ``QWebEngineView`` on top. Both
children remain visible together; the overlay's CSS/Qt transparency
lets the video show through.

These tests inspect ``player_ui.py`` textually (no Qt import needed)
and fail with an explicit message if any architectural invariant is
violated. They are the guard-rails that let future refactors stay
safe without re-learning the two bugs we've already hit:

  * A non-managed free-floating overlay + ``raise_()`` clipped mpv
    on Windows DWM.
  * Hiding the overlay whenever video plays kills every Phase 2 use
    case that composites content over video.
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


@pytest.fixture(scope="module")
def src() -> str:
    return _PLAYER_UI.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def code(src: str) -> str:
    """Source with docstrings + comments stripped, for code-only
    invariants."""
    return _strip_comments_and_docstrings(src)


# ---------------------------------------------------------------------------
# 1. The QStackedLayout(StackAll) architecture is in place
# ---------------------------------------------------------------------------


def test_top_level_window_stays_opaque_black(code: str) -> None:
    """Making the top-level ``self`` translucent disables the HWND-
    level composition path that the overlay relies on. The main
    window MUST stay opaque."""
    # self.setStyleSheet("background-color: black;") is the canonical
    # line that satisfies this.
    assert re.search(
        r'self\.setStyleSheet\(\s*"background-color:\s*black;?"\s*\)',
        code,
    ), "PlayerWindow must explicitly set background-color: black on self."
    # And must NOT set WA_TranslucentBackground on itself.
    init_body = _method_body("__init__")
    assert "self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground" not in init_body, (
        "self must not carry WA_TranslucentBackground — that would break "
        "the HWND-level composition between libmpv and the overlay on Windows."
    )


def test_uses_qstackedlayout_stack_all(code: str) -> None:
    assert "QStackedLayout(" in code, (
        "PlayerWindow must build its overlay composition with "
        "QStackedLayout — the layout manager Qt provides for this case."
    )
    assert "QStackedLayout.StackingMode.StackAll" in code, (
        "QStackedLayout must run in StackingMode.StackAll so both "
        "children remain visible together."
    )


def test_video_container_is_added_before_web_view(src: str) -> None:
    """The order ``addWidget`` is called on the layout establishes
    z-order. Video container FIRST (background), web view SECOND
    (foreground). Swapping the order would put the opaque Layer 0
    on top of the overlay."""
    init = _method_body("__init__")
    # Find the two addWidget lines; assert positional order.
    video_idx = init.find("self._layout.addWidget(self._video_container)")
    web_idx = init.find("self._layout.addWidget(self._web_view)")
    assert video_idx != -1, "_video_container must be added to _layout"
    assert web_idx != -1, "_web_view must be added to _layout"
    assert video_idx < web_idx, (
        "Layer ordering violation: the video container must be added "
        "to the QStackedLayout BEFORE the web view so the overlay is "
        "drawn on top."
    )


def test_web_view_has_translucent_background(code: str) -> None:
    """Three things must agree that the overlay is transparent:
    the Qt widget attribute, the Chromium page background, and the
    CSS on the widget. Missing any one of them breaks compositing."""
    assert "WA_TranslucentBackground" in code
    assert "setBackgroundColor(Qt.GlobalColor.transparent)" in code
    assert "background: transparent" in code


def test_web_view_is_transparent_for_mouse_events(code: str) -> None:
    """The overlay is non-interactive in a kiosk. Setting
    ``WA_TransparentForMouseEvents`` lets clicks fall through to the
    window beneath and signals correctly to the compositor that the
    layer is decorative."""
    assert "WA_TransparentForMouseEvents" in code


# ---------------------------------------------------------------------------
# 2. Non-video items stop libmpv (no ghost frame through gutters)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "helper",
    ["_show_image", "_show_widget", "_show_placeholder"],
)
def test_non_video_helpers_stop_libmpv(helper: str) -> None:
    """Image / widget / placeholder must call ``_stop_video_layer``
    before painting the overlay. Otherwise a previously-playing clip
    keeps decoding behind the letterbox gutters of ``object-fit:
    contain`` and leaks through the HTML's transparent regions."""
    body = _method_body(helper)
    assert "_stop_video_layer(" in body, (
        f"{helper} must call self._stop_video_layer() before painting "
        f"the overlay, so no ghost mpv frame shows behind the new content."
    )


def test_video_branch_clears_overlay_after_loading(src: str) -> None:
    """The video branch must paint a zero-zone transparent document
    on the overlay AFTER handing the file to libmpv. Otherwise a
    previously-shown image or widget would remain on Layer 1 and
    occlude the new video."""
    body = _render_branch("video")
    assert "_play_video_on_mpv(" in body
    assert "_clear_overlay_for_video(" in body


def test_stream_branch_clears_overlay_after_loading() -> None:
    body = _render_branch("stream")
    assert "_play_video_on_mpv(" in body
    assert "_clear_overlay_for_video(" in body


# ---------------------------------------------------------------------------
# 3. Render dispatch contract
# ---------------------------------------------------------------------------


def test_render_image_branch_routes_through_show_image() -> None:
    body = _render_branch("image")
    assert "_show_image(" in body
    for forbidden in ("_mpv.loadfile", "_play_video_on_mpv(", "_show_widget("):
        assert forbidden not in body


def test_render_widget_branch_routes_through_show_widget() -> None:
    body = _render_branch("widget")
    assert "_show_widget(" in body
    for forbidden in ("_mpv.loadfile", "_play_video_on_mpv(", "_show_image("):
        assert forbidden not in body


def test_render_video_branch_does_not_call_show_helpers() -> None:
    """Video must not route through ``_show_image`` /
    ``_show_widget`` / ``_show_placeholder`` — those paint the
    overlay opaquely and would occlude the video."""
    body = _render_branch("video")
    for forbidden in ("_show_image(", "_show_widget(", "_show_placeholder("):
        assert forbidden not in body


def test_render_stream_branch_does_not_call_show_helpers() -> None:
    body = _render_branch("stream")
    for forbidden in ("_show_image(", "_show_widget(", "_show_placeholder("):
        assert forbidden not in body


# ---------------------------------------------------------------------------
# 4. No manual z-order mutation at runtime
# ---------------------------------------------------------------------------


_Z_ORDER_CALLS = ("raise_(", "lower(", "stackUnder(")


def test_no_manual_z_order_mutation_in_render(src: str) -> None:
    """Z-order is set by the ``QStackedLayout`` add order in __init__
    and by nothing else. Calling ``raise_()`` / ``lower()`` /
    ``stackUnder()`` anywhere in ``_render`` or its callees would
    signal the compositor to reshuffle — the bug that bit us in
    Phase 1 before the permanent-layered design."""
    body = _method_body("_render")
    for call in _Z_ORDER_CALLS:
        assert call not in body, (
            f"_render must not call {call!r}. Z-order is owned by the "
            "QStackedLayout configured in __init__."
        )


def test_no_manual_z_order_mutation_in_show_helpers() -> None:
    for helper in ("_show_image", "_show_widget", "_show_placeholder", "_paint_overlay"):
        body = _method_body(helper)
        for call in _Z_ORDER_CALLS:
            assert call not in body, (
                f"{helper} must not call {call!r}. Layer ordering is "
                "established once in __init__ via QStackedLayout."
            )


# ---------------------------------------------------------------------------
# 5. Structural cleanups from previous attempts
# ---------------------------------------------------------------------------


def test_no_qstackedwidget_anywhere(code: str) -> None:
    """Phase 1 used QStackedWidget. Phase 2 uses QStackedLayout —
    similar name, very different semantics (``QStackedWidget`` shows
    one child at a time; ``QStackedLayout(StackAll)`` shows them all).
    The wrong one must not reappear."""
    assert "QStackedWidget" not in code


def test_no_image_label_anywhere(code: str) -> None:
    """An earlier hotfix used a dedicated QLabel for images. That
    design kept mpv occluded even when no video was playing. Images
    now render in the HTML overlay like everything else."""
    assert "_image_label" not in code
    assert "_load_image_on_label" not in code


def test_no_switch_helpers_anywhere(code: str) -> None:
    """The intermediate hotfix introduced ``_switch_to_*`` helpers
    that toggled visibility on individual children. The current
    architecture doesn't need them — ``QStackedLayout`` owns
    visibility. Any reappearance suggests someone brought back the
    visibility-toggling model that this PR intentionally retired."""
    for needle in ("_switch_to_video", "_switch_to_image", "_switch_to_overlay"):
        assert needle not in code, (
            f"{needle!r} has been removed. The QStackedLayout(StackAll) "
            "architecture keeps both children visible together; there is "
            "nothing to switch."
        )


def test_close_event_drains_web_engine() -> None:
    body = _method_body("closeEvent")
    assert "clearHttpCache()" in body
    assert "deleteLater()" in body
    assert "gc.collect()" in body
