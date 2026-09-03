"""The VPS pull must be able to authenticate, and must say so when it cannot.

Runs 206 and 207 (2026-09-03) built and pushed their images, passed every test,
and never reached the box: `git pull --ff-only origin main` in `/opt/360ce-ops`
died on

    fatal: could not read Username for 'https://github.com': No such device or address

— a private repo, an HTTPS remote with no credential, and no TTY to prompt on.
`set -e` then skipped `docker compose pull` entirely, so two merges deployed
nothing while CI stayed green and production kept serving the previous image.
**A deploy that cannot reach the box is indistinguishable from one that did**,
which is the class this repo keeps paying for one layer up.

`360-v2`'s deploy has carried the `git remote set-url` repair since those repos
went private. This one never got it.
"""
from __future__ import annotations

import pathlib

WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / ".github/workflows/deploy.yml"


def _script() -> str:
    return WORKFLOW.read_text()


def test_the_remote_is_re_authenticated_before_the_pull():
    text = _script()
    assert "git remote set-url origin" in text, (
        "the VPS clone loses its credential and cannot prompt; without this the "
        "pull fails and the image never deploys"
    )
    assert text.index("git remote set-url origin") < text.index("git pull --ff-only"), (
        "re-authenticating after the pull fixes nothing"
    )


def test_an_absent_secret_leaves_todays_behaviour_alone():
    """Unguarded, an empty secret rewrites the remote to a URL with no token in
    it — a different and more confusing failure than the one being fixed."""
    text = _script()
    assert 'if [ -n "${{ secrets.GH_PAT }}" ]; then' in text


def test_the_failure_names_its_own_remedy():
    """`could not read Username` reads like a network fault. The next person to
    hit this must not have to diff two repos' workflows to find the fix."""
    text = _script()
    assert "cannot authenticate to GitHub" in text
    assert "GH_PAT" in text
    assert "never reaches production" in text


def test_the_pull_still_fails_the_job():
    """Deploying a new image against a stale compose file is the silent
    degradation this repo forbids — the pull failing must stop the deploy."""
    text = _script()
    assert "if ! git pull --ff-only origin main; then" in text
    assert "exit 1" in text.split("if ! git pull")[1]
