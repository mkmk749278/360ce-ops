"""Every Jinja template must compile.

Guards the render-time template-error class that the deploy pipeline would
otherwise only surface *in production* — an unclosed ``{% %}`` block, a
mistyped tag, an unknown filter. Because ops is the engine **control plane**
(kill switch / auto-mode / LIVE switch), a template that crashes on render
takes down the owner's only control surface, so this is a real safety gate,
not cosmetics.

Compile-only: ``get_template`` parses + code-generates each template, which
raises on syntax/parse errors and unknown filters without needing a full
render context. Mirrors the default ``Jinja2Templates`` environment the app
builds in ``app/main.py`` (plain FileSystemLoader, autoescape on, no custom
filters/globals), so a template that compiles here compiles in the app.
"""
from __future__ import annotations

from pathlib import Path

import jinja2

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "app" / "templates"


def test_all_templates_compile() -> None:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=jinja2.select_autoescape(),
    )
    names = env.list_templates(extensions=["html"])
    assert names, f"no templates found under {_TEMPLATES_DIR}"
    failures: list[str] = []
    for name in names:
        try:
            env.get_template(name)  # parse + compile; raises on bad markup
        except jinja2.TemplateError as exc:  # noqa: PERF203 — collect all, report once
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    assert not failures, "templates failed to compile:\n" + "\n".join(failures)
