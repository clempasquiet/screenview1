"""HTML layout renderer for the transparent WebEngine overlay.

The Phase 2 player architecture layers two surfaces:

  1. **Layer 0** — ``QWidget`` hosting libmpv's hardware video output.
     Plays at most one video or stream at a time, rendered natively by
     the GPU.
  2. **Layer 1** — ``QWebEngineView`` with a **transparent** background,
     rendering an HTML document with absolutely-positioned ``<div>``
     boxes for every image / widget / text / clock / weather zone.
     Vacant areas of the HTML are see-through, letting the video on
     Layer 0 show through.

This module generates the HTML document that lives on Layer 1.
Kept dependency-free (no PyQt, no requests) so it's trivially unit-
testable in headless CI.

Contract
========

``render_layout_html(layout, base_url=None)`` takes a plain Python
dict describing the current frame and returns a ``str`` HTML document.
The expected dict shape is:

    {
        "resolution_w": int,   # authoring width in pixels
        "resolution_h": int,   # authoring height in pixels
        "zones": [
            {
                "id": str|int,       # stable across refreshes
                "kind": "image" | "widget" | "text" | "empty",
                "position_x": int,
                "position_y": int,
                "width": int,
                "height": int,
                "z_index": int,
                "src": str | None,   # file:// or data: URL for image/widget
                "html": str | None,  # inline HTML fragment for widget
                "text": str | None,  # inline text for text-zone
            },
            …
        ],
    }

The renderer handles the ``object-fit: contain`` scaling math entirely
in CSS — the caller doesn't have to know the player's actual screen
resolution. The HTML document declares a logical 1920×1080 canvas (or
whatever ``resolution_w × resolution_h`` specifies) and transforms it
to fit the viewport.
"""
from __future__ import annotations

import html
import json
from typing import Any, Iterable

__all__ = [
    "render_layout_html",
    "render_single_image_layout",
    "render_placeholder_layout",
    "ensure_absolute_url",
]


# ---------------------------------------------------------------------------
# Top-level entry points
# ---------------------------------------------------------------------------


def render_layout_html(layout: dict[str, Any]) -> str:
    """Return a complete HTML document for the given layout.

    See the module docstring for the expected layout dict shape.
    Produces a self-contained document — no external CSS / JS — so
    ``QWebEngineView.setHtml(...)`` can load it without needing a base
    URL or a local file round-trip.
    """
    raw_w = layout.get("resolution_w")
    raw_h = layout.get("resolution_h")
    canvas_w = 1920 if raw_w is None else int(raw_w)
    canvas_h = 1080 if raw_h is None else int(raw_h)
    if canvas_w <= 0 or canvas_h <= 0:
        raise ValueError(f"invalid canvas dimensions: {canvas_w}×{canvas_h}")

    zones = list(layout.get("zones") or [])
    zone_markup = "\n    ".join(_render_zone(z, canvas_w, canvas_h) for z in zones)

    return _TEMPLATE.format(
        canvas_w=canvas_w,
        canvas_h=canvas_h,
        zones_markup=zone_markup,
    )


def render_single_image_layout(src: str, resolution: tuple[int, int] = (1920, 1080)) -> str:
    """Convenience wrapper for the "one full-screen image" case.

    This is what the pre-Phase-2 player used to do natively via
    ``QLabel + QPixmap``. Expressing it as a one-zone layout lets the
    legacy single-media playlist path route through the new layered
    pipeline with zero special-casing, at the cost of a trivial HTML
    round-trip.

    ``src`` must be an absolute URL (``file:///...`` on Windows,
    ``http(s)://...``, or a ``data:`` URI). Use
    :func:`ensure_absolute_url` if you only have a ``pathlib.Path``.
    """
    width, height = resolution
    return render_layout_html(
        {
            "resolution_w": width,
            "resolution_h": height,
            "zones": [
                {
                    "id": "only",
                    "kind": "image",
                    "position_x": 0,
                    "position_y": 0,
                    "width": width,
                    "height": height,
                    "z_index": 0,
                    "src": src,
                }
            ],
        }
    )


def render_placeholder_layout(
    message: str = "Waiting for schedule…",
    *,
    title: str = "ScreenView",
    resolution: tuple[int, int] = (1920, 1080),
) -> str:
    """The branded "no content / content unavailable" fallback frame."""
    width, height = resolution
    safe_message = html.escape(message)
    safe_title = html.escape(title)
    # Use a single full-canvas HTML zone so the placeholder can evolve
    # stylistically without changing the layout contract.
    return render_layout_html(
        {
            "resolution_w": width,
            "resolution_h": height,
            "zones": [
                {
                    "id": "placeholder",
                    "kind": "text",
                    "position_x": 0,
                    "position_y": 0,
                    "width": width,
                    "height": height,
                    "z_index": 0,
                    # Build the inline HTML through the ``html`` kind so
                    # the placeholder can be visually distinct from
                    # operator-supplied text zones.
                    "html": (
                        '<div class="screenview-placeholder">'
                        f'<div class="screenview-brand">{safe_title}</div>'
                        f'<div class="screenview-msg">{safe_message}</div>'
                        "</div>"
                    ),
                }
            ],
        }
    )


# ---------------------------------------------------------------------------
# URL helper
# ---------------------------------------------------------------------------


def ensure_absolute_url(path_or_url: str) -> str:
    """Return *path_or_url* as something a browser can resolve.

    Accepts anything already URL-ish (``http://``, ``https://``,
    ``file://``, ``data:``) as-is. For local-looking strings (Windows
    ``C:\\foo`` or POSIX ``/foo``) we return a ``file:///`` URL.

    Avoids importing ``QUrl`` so this module stays PyQt-free.
    """
    s = str(path_or_url).strip()
    if not s:
        raise ValueError("empty path/URL")
    lower = s.lower()
    if (
        lower.startswith("http://")
        or lower.startswith("https://")
        or lower.startswith("file://")
        or lower.startswith("data:")
    ):
        return s
    # Windows absolute path (``C:\…`` or ``\\server\share``): produce a
    # canonical file:/// URL. ``pathlib`` is overkill here since we
    # don't want OS-specific behaviour sneaking in during tests.
    path = s.replace("\\", "/")
    if len(path) >= 2 and path[1] == ":":
        return "file:///" + path
    if path.startswith("//"):
        return "file:" + path
    if path.startswith("/"):
        return "file://" + path
    # Relative path: assume it's already something the document base
    # can resolve; pass through unchanged.
    return s


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>ScreenView layout</title>
<style>
  /* Make the document fully transparent so Layer 0 (libmpv video) is
     visible through every part of the HTML that we don't explicitly
     paint. ``background: transparent`` on html+body + an rgba clear
     colour on the WebEngine profile is what makes the overlay work. */
  html, body {{
    margin: 0;
    padding: 0;
    background: transparent;
    overflow: hidden;
    width: 100vw;
    height: 100vh;
    /* Disable text selection + long-press handlers; this is a display,
       nobody should be clicking it. */
    -webkit-user-select: none;
    user-select: none;
    cursor: none;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 Oxygen, Ubuntu, Cantarell, sans-serif;
  }}

  /* The canvas is the authoring-resolution plane. All zones position
     themselves inside it in pixels. We scale + centre it in the
     viewport with object-fit-contain semantics so a 1920×1080 layout
     looks identical on a 4K display (letter-boxed) or a 1366×768
     laptop (pillar-boxed). */
  .screenview-canvas {{
    position: absolute;
    top: 50%;
    left: 50%;
    width: {canvas_w}px;
    height: {canvas_h}px;
    transform: translate(-50%, -50%) scale(var(--screenview-scale));
    transform-origin: center center;
  }}

  .screenview-zone {{
    position: absolute;
    box-sizing: border-box;
    overflow: hidden;
    background: transparent;
  }}

  .screenview-zone img.zone-image {{
    width: 100%;
    height: 100%;
    object-fit: contain;
    display: block;
  }}

  .screenview-zone iframe.zone-widget {{
    width: 100%;
    height: 100%;
    border: 0;
    background: transparent;
  }}

  .screenview-zone .zone-text {{
    width: 100%;
    height: 100%;
    display: flex;
    align-items: center;
    justify-content: center;
    color: #e6e8ef;
    font-size: 24px;
    text-align: center;
    padding: 1rem;
    box-sizing: border-box;
  }}

  /* Placeholder styling (principal colours mirror the main CMS theme
     so a kiosk without a schedule still looks on-brand). */
  .screenview-placeholder {{
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    width: 100%;
    height: 100%;
    background: #0f1115;  /* opaque so placeholder hides any stale video */
  }}
  .screenview-brand {{
    font-size: 72px;
    font-weight: 700;
    color: #4f8cff;
    letter-spacing: 1px;
  }}
  .screenview-msg {{
    font-size: 28px;
    color: #9ba1b0;
    margin-top: 1rem;
  }}
</style>
</head>
<body>
<div class="screenview-canvas">
    {zones_markup}
</div>
<script>
  // Recompute the scale factor so the authoring canvas fits the real
  // window while preserving aspect ratio (object-fit: contain).
  // Runs on load + every resize; extremely cheap.
  (function() {{
    var CANVAS_W = {canvas_w};
    var CANVAS_H = {canvas_h};
    function rescale() {{
      var sx = window.innerWidth / CANVAS_W;
      var sy = window.innerHeight / CANVAS_H;
      var scale = Math.min(sx, sy);
      document.documentElement.style.setProperty(
        "--screenview-scale", scale.toString()
      );
    }}
    window.addEventListener("resize", rescale);
    window.addEventListener("load", rescale);
    rescale();
  }})();
</script>
</body>
</html>
"""


def _render_zone(zone: dict[str, Any], canvas_w: int, canvas_h: int) -> str:
    """Render one ``<div class="screenview-zone">`` element."""
    kind = str(zone.get("kind") or "empty").lower()
    x = int(zone.get("position_x") or 0)
    y = int(zone.get("position_y") or 0)
    w = int(zone.get("width") or canvas_w)
    h = int(zone.get("height") or canvas_h)
    z = int(zone.get("z_index") or 0)
    zone_id = str(zone.get("id") or "zone")
    data_id = html.escape(zone_id, quote=True)

    # Every attribute uses double-quoted values so the {style} / {inner}
    # substitutions below cannot escape into tag-syntax territory.
    style = (
        f"left:{x}px;top:{y}px;"
        f"width:{w}px;height:{h}px;"
        f"z-index:{z};"
    )
    inner = _render_zone_contents(kind, zone)
    return (
        f'<div class="screenview-zone" data-zone-id="{data_id}" '
        f'data-zone-kind="{html.escape(kind, quote=True)}" '
        f'style="{style}">{inner}</div>'
    )


def _render_zone_contents(kind: str, zone: dict[str, Any]) -> str:
    """Return the HTML fragment that goes inside a zone's outer ``<div>``."""
    if kind == "image":
        src = zone.get("src")
        if not src:
            return ""
        safe_src = html.escape(str(src), quote=True)
        alt = html.escape(str(zone.get("alt") or ""), quote=True)
        return f'<img class="zone-image" src="{safe_src}" alt="{alt}">'

    if kind == "widget":
        # An HTML widget can be provided two ways:
        #   * ``src`` — a URL (local file, ``data:``, or remote). Loaded
        #     in an <iframe sandbox="..."> for isolation.
        #   * ``html`` — an inline HTML fragment. Embedded directly.
        src = zone.get("src")
        inline = zone.get("html")
        if src:
            safe_src = html.escape(str(src), quote=True)
            # Sandbox blocks top-navigation, popups, forms, pointer-lock
            # and storage. ``allow-scripts`` + ``allow-same-origin`` are
            # the minimum for widgets that do live data fetching
            # (clocks, weather, RSS tickers).
            return (
                f'<iframe class="zone-widget" src="{safe_src}" '
                'sandbox="allow-scripts allow-same-origin" '
                'referrerpolicy="no-referrer" loading="eager"></iframe>'
            )
        if inline:
            # Caller is responsible for having already sanitised this.
            return str(inline)
        return ""

    if kind == "text":
        # Text zones accept pre-built inline HTML (placeholder) or a
        # plain-text string which we escape safely.
        inline = zone.get("html")
        if inline:
            return str(inline)
        text = zone.get("text") or ""
        return f'<div class="zone-text">{html.escape(str(text))}</div>'

    # Unknown or "empty" kind: a transparent placeholder so the caller
    # can reserve a region for later without breaking layout.
    return ""


def _debug_dump(layout: dict[str, Any]) -> str:
    """Non-public helper used by tests to print a compact layout summary."""
    return json.dumps(
        {
            "resolution": [layout.get("resolution_w"), layout.get("resolution_h")],
            "zones": [
                {
                    "id": z.get("id"),
                    "kind": z.get("kind"),
                    "box": [
                        z.get("position_x"),
                        z.get("position_y"),
                        z.get("width"),
                        z.get("height"),
                    ],
                    "z": z.get("z_index"),
                }
                for z in (layout.get("zones") or [])
            ],
        },
        separators=(",", ":"),
    )
