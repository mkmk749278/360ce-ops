"""The VPS pull must be able to authenticate, and must say so when it cannot.

Runs 206, 207 and 208 (2026-09-03) built and pushed their images, passed every
test, and never reached the box: the unauthenticated `git pull --ff-only origin
main` in `/opt/360ce-ops` was refused. `set -e` then skipped `docker compose
pull` entirely, so three merges deployed nothing while CI stayed green and
production kept serving the previous image. **A deploy that cannot reach the box
is indistinguishable from one that did**, which is the class this repo keeps
paying for one layer up.

**Two different refusals, and the second corrected the diagnosis.** Runs 206/207
died on `could not read Username for 'https://github.com'`, which reads as a
missing credential on a private repo — and that was written down here as the
cause. Run 208, with the error path from this change in place, printed what
GitHub actually says:

    fatal: remote error: GitHub is temporarily limiting some unauthenticated
    downloads to protect the stability of the platform. Please retry later or
    authenticate.

That is a **throttle on anonymous access**, not a permission error. Nothing
about this repository changed; the pull was working unauthenticated until it
was not, which is why a deploy that had run green for months failed three times
in a row with no diff to blame. The remedy is the one the vendor names, and the
one `360-v2`'s deploy has used for months: authenticate. This workflow never
did.
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
