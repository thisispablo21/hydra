"""Per-model usage pricing."""

import pytest

from server import pricing


def test_fable_5_1_cache_reads_use_reduced_multiplier():
    parts = pricing.cost_components("claude-fable-5-1", cache_read_tokens=1_000_000)

    assert parts is not None
    assert parts["cache_read"] == pytest.approx(0.25)


def test_opus_5_5_rates_and_reduced_cache_reads():
    parts = pricing.cost_components(
        "claude-opus-5-5",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_5m_tokens=1_000_000,
        cache_write_1h_tokens=1_000_000,
    )

    assert parts is not None
    assert parts["input"] == pytest.approx(4.0)
    assert parts["output"] == pytest.approx(20.0)
    assert parts["cache_read"] == pytest.approx(0.20)
    assert parts["cache_write_5m"] == pytest.approx(5.0)
    assert parts["cache_write_1h"] == pytest.approx(8.0)


def test_sonnet_5_uses_permanent_rates():
    parts = pricing.cost_components(
        "claude-sonnet-5", input_tokens=1_000_000, output_tokens=1_000_000
    )

    assert parts is not None
    assert parts["input"] == pytest.approx(2.0)
    assert parts["output"] == pytest.approx(10.0)


def test_sol_cache_reads_use_standard_multiplier():
    parts = pricing.cost_components("gpt-5.6-sol", cache_read_tokens=1_000_000)

    assert parts is not None
    assert parts["cache_read"] == pytest.approx(0.4)


def test_unknown_model_stays_unpriced():
    assert pricing.rate_for("model-from-the-future") is None
    assert pricing.cost_components("model-from-the-future") is None


def test_model_suffix_normalisation_is_unchanged():
    assert pricing.rate_for("claude-fable-5-1[1m]") == pricing.RATES["claude-fable-5-1"]
    assert pricing.rate_for("claude-haiku-4-5-20251001") == pricing.RATES["claude-haiku-4-5"]


def test_astra_prices_at_short_context_rates():
    parts = pricing.cost_components(
        "gpt-6-astra",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
    )

    assert parts is not None
    assert parts["input"] == pytest.approx(10.0)
    assert parts["output"] == pytest.approx(50.0)
    assert parts["cache_read"] == pytest.approx(1.0)


def test_fast_mode_doubles_every_component():
    base = dict(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_5m_tokens=1_000_000,
    )
    std = pricing.cost_components("gpt-6-astra", service_tier="default", **base)
    fast = pricing.cost_components("gpt-6-astra", service_tier="priority", **base)

    assert std is not None and fast is not None
    for part, value in std.items():
        assert fast[part] == pytest.approx(value * 2)


def test_priority_and_fast_are_the_same_tier():
    # OpenAI renamed priority -> fast on 2026-07-30 and accepts both spellings.
    assert pricing.tier_mult("priority") == pricing.tier_mult("fast") == 2.0


def test_absent_tier_prices_as_default():
    # Codex only records a tier when a thread applies settings; no record means
    # nothing moved the thread off the standard rate.
    assert pricing.tier_mult(None) == 1.0
    assert pricing.tier_mult("") == 1.0
    assert pricing.tier_mult("default") == 1.0
    assert pricing.tier_mult("standard") == 1.0


def test_unknown_tier_is_unpriced_not_assumed_base():
    assert pricing.tier_mult("scale") is None
    assert pricing.cost_components(
        "gpt-6-astra", service_tier="scale", input_tokens=1_000_000
    ) is None


def test_tier_is_case_and_space_insensitive():
    assert pricing.tier_mult(" Priority ") == 2.0
