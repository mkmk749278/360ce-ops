"""The Control pages keep their explanations behind an ⓘ, not above the switch.

Owner, 2026-09-24: *"Don't keep all that brief there, keep i icon over there to
know that's it, keep everything simple."* The sentences were not cut — they are
the guardrails, and other tests pin them in the HTML — they moved into
``_info.html``'s popover so the state and the button lead each page.

Two things are guarded here, both derived from the templates rather than
listed:

* **Every Control page head leads with its title and an ⓘ, not a paragraph.**
  A brief that creeps back above the switches is the "messy and unclear like
  raw" the owner reported twice.
* **No ⓘ sits inside a ``<p>``.** The ⓘ is a ``<details>``, which is flow
  content; a ``<p>`` may hold only phrasing content, so the HTML parser closes
  the paragraph at the ``<details>``. The icon then drops onto a line of its
  own under the sentence it explains and a stray ``</p>`` follows — which is
  exactly how five of them rendered on Promotions before this guard existed.
"""

from __future__ import annotations

import pathlib
import re

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "app" / "templates"

#: The Control group, derived from base.html's NAV literal so a new Control
#: sub-tab is covered without editing this file.
_NAV = (TEMPLATES / "base.html").read_text()
_CONTROL_URLS = re.findall(
    r"\('(/[^']*)',\s*'[^']*',\s*'[^']*'\)",
    _NAV[_NAV.index("('control', 'Control'"):_NAV.index("('diagnostics'")],
)

_URL_TO_TEMPLATE = {
    "/control": "control.html",
    "/control/routing": "control_routing.html",
    "/control/promotions": "control_promotions.html",
    "/control/users": "users.html",
    "/control/referrals": "referrals.html",
    "/trials": "trials.html",
    "/control/access": "control_access.html",
}

_COMMENT = re.compile(r"\{#.*?#\}", re.S)
_CALL = "{% call info"


def _source(name: str) -> str:
    return _COMMENT.sub("", (TEMPLATES / name).read_text())


def test_every_control_tab_is_mapped_to_its_template():
    """A new Control sub-tab must be added here, or it escapes both guards."""
    assert _CONTROL_URLS, "could not parse the Control group out of base.html"
    missing = [u for u in _CONTROL_URLS if u not in _URL_TO_TEMPLATE]
    assert not missing, f"Control tab(s) with no template mapping: {missing}"


@pytest.mark.parametrize("url", _CONTROL_URLS)
def test_the_page_head_is_a_title_and_an_info_icon(url):
    src = _source(_URL_TO_TEMPLATE[url])
    assert '{% from "_info.html" import info %}' in src
    head_at = src.index('class="page-head"')
    call_at = src.find(_CALL, head_at)
    assert call_at != -1, f"{url}: the page head has no ⓘ"
    before = src[head_at:call_at]
    assert "<p" not in before, (
        f"{url}: a paragraph sits in the page head above the ⓘ — the brief "
        "belongs inside it"
    )
    assert 'class="lede"' not in src and 'class="explain"' not in src


@pytest.mark.parametrize("url", _CONTROL_URLS)
def test_no_info_icon_is_nested_in_a_paragraph(url):
    src = _source(_URL_TO_TEMPLATE[url])
    bad = []
    for m in re.finditer(r"<p(\s[^>]*)?>", src):
        close = src.find("</p>", m.end())
        call = src.find(_CALL, m.end())
        if call != -1 and (close == -1 or call < close):
            line = src.count("\n", 0, m.start()) + 1
            bad.append(line)
    assert not bad, (
        f"{url}: <p> wraps an ⓘ at line(s) {bad} (comments stripped) — use a "
        "<div>, since a <p> cannot contain a <details>"
    )


def test_the_guard_fails_on_the_shape_it_exists_to_catch():
    """Verify the pin by feeding it the defect."""
    src = '<p class="small">Read this. {% call info("x") %}<p>why</p>{% endcall %}</p>'
    m = re.search(r"<p(\s[^>]*)?>", src)
    assert src.find(_CALL, m.end()) < src.find("</p>", m.end())
