"""Every Jinja template must compile — in the environment the APP actually uses.

Guards the render-time template-error class that the deploy pipeline would
otherwise only surface *in production* — an unclosed ``{% %}`` block, a
mistyped tag, an unknown filter. Because ops is the engine **control plane**
(kill switch / auto-mode / LIVE switch), a template that crashes on render
takes down the owner's only control surface, so this is a real safety gate,
not cosmetics.

**This test used to build its own environment**, described as "mirrors the
default ``Jinja2Templates`` environment the app builds in ``app/main.py``
(plain FileSystemLoader, autoescape on, no custom filters/globals), so a
template that compiles here compiles in the app". That parenthesis stopped
being true the moment ``app/main.py`` registered its first global, and the
sentence claiming parity is what made the drift invisible. A hand-built mirror
of a collaborator is the shape this repo keeps paying for: it asserts your
assumption back at you, and it can only ever diverge in the direction of
*passing over a template the app cannot render*.

So it imports the real environment. Compile-only: ``get_template`` parses and
code-generates each template, which raises on syntax/parse errors and unknown
filters without needing a full render context — and because the environment is
the app's own, an unknown filter here is an unknown filter in production.
"""
from __future__ import annotations

from pathlib import Path

import jinja2

# The app's environment, with every filter and global main.py registers on it.
from app.main import templates as _app_templates

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "app" / "templates"


def test_all_templates_compile() -> None:
    env = _app_templates.env
    names = env.list_templates(extensions=["html"])
    assert names, f"no templates found under {_TEMPLATES_DIR}"
    failures: list[str] = []
    for name in names:
        try:
            env.get_template(name)  # parse + compile; raises on bad markup
        except jinja2.TemplateError as exc:  # noqa: PERF203 — collect all, report once
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    assert not failures, "templates failed to compile:\n" + "\n".join(failures)


def test_the_environment_under_test_is_the_apps_own() -> None:
    """Pin the wiring, not the import.

    Without this, a future refactor that rebuilds a local environment here
    would go green while templates using a registered filter or global fail to
    render in production — which is exactly the state this file was in before
    2026-08-07.
    """
    env = _app_templates.env
    assert env.loader is not None
    # Every filter/global main.py attaches must be reachable from the env this
    # test compiles against, or the compile step is not testing production.
    for name in ("price", "pct", "secs"):
        assert name in env.filters, f"{name} filter missing from the app environment"
    for name in ("GUEST_READ_ROUTES", "may_use"):
        assert name in env.globals, f"{name} global missing from the app environment"
