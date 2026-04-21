"""Static regression: _render's non-widget branches must not touch the web view.

Context
=======
On Windows, ``QWebEngineView`` carries its own native HWND (Chromium
composites on a dedicated OS-level window). Any mutation of that view —
including something as seemingly benign as ``setUrl("about:blank")`` —
wakes up the Chromium compositor and can re-raise the web-view HWND
above the libmpv container's HWND in the native z-order. The visible
symptom is a playing video whose audio is heard but whose picture is
hidden by a transparent Chromium surface sitting on top.

That regression was introduced during Phase 1 when a
``_release_web_view()`` helper was called from the ``video``, ``stream``
and ``image`` branches of ``_render``. It has been removed; this test
exists to make sure no future cleanup-happy refactor re-introduces the
same class of mistake.

Implementation note
===================
We inspect the module's **source text** rather than importing it.
Importing ``player_ui`` drags ``PyQt6.QtGui``, which in turn requires a
system graphics stack (``libEGL.so`` on Linux) — not present in headless
CI. A purely textual check is sufficient because the question we're
asking is "does the source of _render's non-widget branches mention
the web view at all?", not "what does _render do at runtime?".
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


_PLAYER_UI = Path(__file__).resolve().parent.parent / "player_ui.py"


def _render_body() -> str:
    """Return the text of ``PlayerWindow._render`` as it appears on disk.

    We locate the method by its ``def _render(self, entry: PlaylistEntry)``
    header and read every subsequent line that sits at a deeper indentation
    level. This is stable because the method's indentation has been the
    same since it was introduced; any future refactor that changes the
    indentation would also need to update this test.
    """
    src = _PLAYER_UI.read_text(encoding="utf-8")
    lines = src.splitlines()
    header_idx = -1
    base_indent = 0
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("def _render("):
            header_idx = i
            base_indent = len(line) - len(stripped)
            break
    assert header_idx >= 0, "Could not find PlayerWindow._render"
    body_lines: list[str] = [lines[header_idx]]
    for line in lines[header_idx + 1:]:
        if line.strip() == "":
            body_lines.append(line)
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= base_indent:
            break
        body_lines.append(line)
    return "\n".join(body_lines)


def _branch(body: str, kind: str) -> str:
    """Extract the body of the ``if entry.kind == "<kind>":`` block."""
    pattern = re.compile(
        r'if entry\.kind == "' + re.escape(kind) + r'":(.*?)(?=\n\s*if entry\.kind ==|\Z)',
        re.DOTALL,
    )
    match = pattern.search(body)
    assert match, f"branch for kind={kind!r} not found"
    return match.group(1)


@pytest.fixture(scope="module")
def render_src() -> str:
    return _render_body()


def test_render_video_branch_does_not_touch_web_view(render_src):
    """Reproducer for the 'audio plays but image is black' bug."""
    body = _branch(render_src, "video")
    for forbidden in ("_web_view", "_release_web_view"):
        assert forbidden not in body, (
            f"_render's video branch must not reference {forbidden!r}. "
            "On Windows, mutating the QWebEngineView wakes its native "
            "HWND and hides the libmpv video. See the header docstring "
            "of this test file for background."
        )


def test_render_stream_branch_does_not_touch_web_view(render_src):
    """Live streams go through the same mpv HWND as recorded videos,
    so they must obey the same isolation rule."""
    body = _branch(render_src, "stream")
    for forbidden in ("_web_view", "_release_web_view"):
        assert forbidden not in body, (
            f"_render's stream branch must not reference {forbidden!r}."
        )


def test_render_image_branch_does_not_touch_web_view(render_src):
    """Images render on a QLabel in the same stack — touching the web
    view from here also disturbs the native z-order on Windows."""
    body = _branch(render_src, "image")
    for forbidden in ("_web_view", "_release_web_view"):
        assert forbidden not in body, (
            f"_render's image branch must not reference {forbidden!r}."
        )


def test_render_widget_branch_still_configures_web_view(render_src):
    """Sanity: the widget branch is where web-view work *is* allowed.
    If this ever stops touching the view, the widget kind stopped
    working at all — equally bad."""
    body = _branch(render_src, "widget")
    assert "_web_view" in body, "widget branch no longer configures _web_view"


def test_release_web_view_helper_is_gone():
    """The helper that caused the regression is intentionally deleted.
    Re-adding it is a smell (nothing should need to poke the web view
    from outside the widget branch); this test flags that attempt."""
    src = _PLAYER_UI.read_text(encoding="utf-8")
    assert "def _release_web_view(" not in src, (
        "_release_web_view has been removed because calling it from the "
        "video/stream/image branches re-raised the web-view HWND above "
        "libmpv on Windows, causing 'audio without picture'. If you truly "
        "need such a helper, make sure no caller uses it from a "
        "non-widget render branch and update this test explicitly."
    )
