"""Display formatting — raw float repr is not a rendered number (2026-08-07).

Reading the live pages turned up a PnL column at `-0.47041707080504297`, a feed
age at `54.348713397979736` seconds, TP levels at twelve decimal places on an
instrument quoted to seven, and swept levels at `0.044309999999999995` — a value
that is pure binary-float noise and that nobody computed.
"""
from __future__ import annotations

import pytest

from app.template_filters import EMDASH, pct, price, secs


class TestPrice:
    @pytest.mark.parametrize("raw,shown", [
        # The live values, verbatim.
        (0.019803918136, "0.01980392"),
        (0.044309999999999995, "0.04431"),
        (501.6468015414258, "501.64680154"),
        (90.32841608722484, "90.32841609"),
        # Precision the instrument genuinely has is preserved, not padded.
        (0.0212386, "0.0212386"),
        (0.02062, "0.02062"),
        (64328.8, "64328.8"),
        (1.0, "1"),
        (0, "0"),
    ])
    def test_keeps_the_instruments_own_precision(self, raw, shown):
        assert price(raw) == shown

    def test_never_uses_scientific_notation(self):
        """`%g` would flip to exponent form exactly on the sub-cent movers that
        dominate the delivered book — which is why this is not `%g`."""
        for v in (0.00000123, 1e-7, 0.0000001):
            assert "e" not in price(v).lower()

    def test_missing_renders_an_em_dash_never_a_zero(self):
        # An em-dash is the repo's marker for "the engine did not report this".
        # Rendering 0 here is how a blank becomes a finding.
        for v in (None, "", "abc", float("nan"), float("inf")):
            assert price(v) == EMDASH

    def test_a_boolean_is_not_a_price(self):
        # bool is an int subclass; float(True) == 1.0 would render "1".
        assert price(True) == EMDASH


class TestPct:
    def test_fixed_places_because_a_percentage_has_no_tick_size(self):
        assert pct(-0.47041707080504297) == "-0.47"
        assert pct(4.0116218207749) == "4.01"
        assert pct(0) == "0.00"

    def test_places_are_tunable_for_columns_that_need_them(self):
        assert pct(-0.47041707080504297, 3) == "-0.470"

    def test_missing_renders_an_em_dash(self):
        assert pct(None) == EMDASH

    def test_no_percent_sign_is_appended(self):
        """The column header owns the unit; doubling it is how a ratio gets read
        as a percentage of a percentage."""
        assert "%" not in pct(1.5)


class TestSecs:
    def test_one_decimal(self):
        assert secs(54.348713397979736) == "54.3"

    def test_missing_renders_an_em_dash(self):
        assert secs(None) == EMDASH


class TestRegisteredOnTheRealEnvironment:
    def test_the_app_actually_has_them(self):
        """A filter defined and not registered is the seam this repo keeps
        paying for — pin the wiring, not just the function."""
        from app.main import templates

        for name in ("price", "pct", "secs"):
            assert name in templates.env.filters

    def test_pages_render_no_raw_float_repr(self):
        """The check that would have caught this: drive the real pages and look
        for long decimal runs in values that are not prices.

        Prices legitimately carry many decimals on sub-cent instruments, so the
        assertion is on the columns a fixed precision applies to.
        """
        import re

        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            client.post("/login", data={"password": "test-token"},
                        follow_redirects=False)
            for page in ("/signals", "/positions", "/signals/price-action"):
                body = client.get(page).text
                # >8 decimal places cannot come from a Binance price and is the
                # signature of an unformatted float.
                offenders = re.findall(r">\s*-?\d+\.\d{9,}\s*<", body)
                assert not offenders, f"{page} rendered float repr: {offenders[:3]}"
