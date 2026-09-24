"""Every class a template uses must be styled somewhere (2026-09-24).

Owner, reading the Control tab: *"everything feels messy and unclear like
raw"*. Part of that was design, but the larger part was not a design choice at
all: `.panel`, `.tbl`, `badge-live` / `badge-off` / `badge-err` /
`badge-good`, `btn-primary`, `btn-active`, `row-sel`, `.kpi*`, `.pager` and
more were used by templates and defined nowhere, so each one rendered as a
browser default. That meant a white-bordered fieldset on the Promotions page, a
bare table under a styled heading, and a "LIVE" badge on Users with no colour at
all, because `badge-good` did not exist.

Nothing failed. A missing class does not raise, it does not blank a page, and
no page test can see it — a rendered page that *looks* unfinished passes every
assertion about its copy. This is the seam shape this repo keeps naming: one
half written (the template), the other half never written (the rule), both
halves individually "fine".

So the guard is **derived**: collect every class token the templates use, and
require each one to appear as a selector in the stylesheet (or in an inline
`<style>` block). Tomorrow's template is covered without anyone editing a list.

What it deliberately does not check:

* **Tokens built by Jinja** — `badge-{{ state }}`, `side-{{ dir }}`. The static
  scan sees only the prefix (`badge-`), which is skipped: the suffix is data,
  and which values it takes is not knowable from the template.
* **Classes used only as JS hooks** would fail here, and that is intended —
  give the hook a real rule (even `min-width: 0` on a grid child is a real
  rule) or reconsider whether it needs a class at all.
"""
from __future__ import annotations

import re
from pathlib import Path

_APP = Path(__file__).resolve().parents[1] / "app"
_TEMPLATES = _APP / "templates"
_CSS = _APP / "static" / "style.css"

_CLASS_ATTR = re.compile(r'class="([^"]*)"')
_JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S)
_TOKEN = re.compile(r"[A-Za-z_][\w-]*")
_SELECTOR_CLASS = re.compile(r"\.([A-Za-z_][\w-]*)")
_STYLE_BLOCK = re.compile(r"<style[^>]*>(.*?)</style>", re.S)


def _defined_classes() -> set[str]:
    css = _CSS.read_text(encoding="utf-8")
    for path in _TEMPLATES.rglob("*.html"):
        for block in _STYLE_BLOCK.findall(path.read_text(encoding="utf-8")):
            css += "\n" + block
    # Strip comments so a class NAMED in a comment does not count as styled.
    css = re.sub(r"/\*.*?\*/", " ", css, flags=re.S)
    return set(_SELECTOR_CLASS.findall(css))


def _used_classes() -> dict[str, set[str]]:
    used: dict[str, set[str]] = {}
    for path in sorted(_TEMPLATES.rglob("*.html")):
        body = path.read_text(encoding="utf-8")
        # Jinja comments can hold example markup; they never render.
        body = re.sub(r"\{#.*?#\}", " ", body, flags=re.S)
        for attr in _CLASS_ATTR.findall(body):
            static = _JINJA.sub(" ", attr)
            for tok in static.split():
                if not _TOKEN.fullmatch(tok) or tok.endswith("-"):
                    continue  # a Jinja-built token's prefix — see docstring
                used.setdefault(tok, set()).add(path.name)
    return used


def test_the_scan_sees_the_templates():
    """A guard that silently checks nothing is worse than no guard."""
    used = _used_classes()
    assert len(used) > 50, f"only {len(used)} classes found — the scan is broken"
    assert "card" in used and "card" in _defined_classes()


def test_every_class_a_template_uses_is_styled():
    defined = _defined_classes()
    missing = {
        cls: sorted(files) for cls, files in _used_classes().items()
        if cls not in defined
    }
    assert not missing, (
        "classes used by templates but defined nowhere — each renders as a "
        "browser default (a bare table, an uncoloured badge):\n"
        + "\n".join(f"  .{c}: {', '.join(f)}" for c, f in sorted(missing.items()))
    )


def test_the_canvas_carries_the_base_colour():
    """Below the first viewport the page fell through to white: the only
    background was a `fixed` gradient on <body>, which a full-page render and
    browsers that ignore `background-attachment: fixed` do not extend."""
    css = _CSS.read_text(encoding="utf-8")
    html_rule = re.search(r"(?m)^html\s*\{([^}]*)\}", css)
    assert html_rule and "background" in html_rule.group(1)
