"""Rendered attention never reads as advice, and states plainly what it is."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

import pytest

from dhruva.contexts.intelligence.domain.attention import AttentionBand, ResearchAttention
from dhruva.contexts.intelligence.interfaces.attention_presentation import (
    ATTENTION_DISCLAIMER,
    render_attention,
)
from dhruva.shared.identity import InstrumentId

pytestmark = pytest.mark.unit

AS_OF: Final = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)

#: The same forbidden vocabulary test_digest_cli.py already checks its own
#: rendering against -- reused rather than reinvented, so the two disclaimers
#: are held to one standard.
_ADVICE = ("buy", "sell", "hold", "target price", "recommend", "should invest", "outperform")


def _entry(
    symbol: str,
    *,
    score: int = 0,
    band: AttentionBand = AttentionBand.LOW,
    reasons: tuple[str, ...] = (),
    market_context_available: bool = True,
) -> ResearchAttention:
    return ResearchAttention(
        instrument_id=InstrumentId.deterministic("reference", symbol.lower()),
        canonical_symbol=symbol,
        company_name=f"{symbol} Limited",
        as_of=AS_OF,
        score=score,
        band=band,
        reasons=reasons,
        market_context_available=market_context_available,
    )


def test_the_disclaimer_states_what_the_ranking_is_and_is_not() -> None:
    """Both halves matter: what it is, and what it must never be read as."""
    assert "not advice" in ATTENTION_DISCLAIMER.lower()
    assert "not a recommendation" in ATTENTION_DISCLAIMER.lower()
    assert "expected return" in ATTENTION_DISCLAIMER.lower()


def test_rendered_output_contains_no_advice_language() -> None:
    """The one failure mode every ranking-and-highlighting tool is a sentence from.

    Scoped to DHRUVA's own framing, exactly as ``test_digest_cli.py`` scopes
    its identical check: the disclaimer's entire job is to use these words in
    the negative ("not a signal to buy, sell or hold"), and that it does so is
    asserted separately in ``test_the_disclaimer_states_what_the_ranking_is_and_is_not``.
    """
    move_reason = ("1-day absolute move 5.84%",)
    entries = (
        _entry("SBIN", score=5, band=AttentionBand.ELEVATED, reasons=move_reason),
        _entry("HAL", score=0, band=AttentionBand.LOW),
    )

    rendered = render_attention(entries).replace(ATTENTION_DISCLAIMER, "").lower()

    for verb in _ADVICE:
        assert verb not in rendered, f"attention output must not say {verb!r}"


def test_the_disclaimer_is_always_present() -> None:
    """Printed whether the ranking has entries or the watchlist is empty."""
    assert ATTENTION_DISCLAIMER in render_attention((_entry("SBIN"),))
    assert ATTENTION_DISCLAIMER in render_attention(())


def test_every_instrument_is_named_with_its_band_and_score() -> None:
    """A reader must be able to see the verdict and why, in one line each."""
    move_reason = ("1-day absolute move 5.84%",)
    rendered = render_attention(
        (_entry("SBIN", score=5, band=AttentionBand.ELEVATED, reasons=move_reason),)
    )

    assert "SBIN" in rendered
    assert "ELEVATED" in rendered
    assert "score 5" in rendered
    assert "1-day absolute move 5.84%" in rendered


def test_zero_reasons_states_nothing_was_noteworthy_rather_than_a_blank() -> None:
    """A blank line would be indistinguishable from an omission."""
    rendered = render_attention((_entry("HAL", score=0, band=AttentionBand.LOW),))

    assert "nothing observed was noteworthy" in rendered


def test_unavailable_market_context_is_flagged_in_the_rendering() -> None:
    """Not knowing must read differently from nothing having moved."""
    rendered = render_attention((_entry("HAL", market_context_available=False),))

    assert "market context unavailable" in rendered


def test_available_market_context_carries_no_unavailable_flag() -> None:
    """The flag is only ever shown when it is true."""
    rendered = render_attention((_entry("SBIN", market_context_available=True),))

    assert "market context unavailable" not in rendered


def test_an_empty_ranking_states_the_watchlist_is_empty_rather_than_a_blank_page() -> None:
    """A blank screen is indistinguishable from a broken command."""
    rendered = render_attention(())

    assert "No instruments are on the watchlist" in rendered


def test_rendering_preserves_the_order_it_is_given() -> None:
    """Sorting is rank_watchlist's job; rendering must not silently re-sort."""
    entries = (
        _entry("ZZZ", score=9, band=AttentionBand.HIGH),
        _entry("AAA", score=0, band=AttentionBand.LOW),
    )

    rendered = render_attention(entries)

    assert rendered.index("ZZZ") < rendered.index("AAA")
