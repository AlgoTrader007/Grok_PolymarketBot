"""Entry rules: fees, price cap, full stake, stale data, and one side per window."""
from decimal import Decimal

import gates


def _kw(**overrides):
    base = dict(
        fair=0.60,
        asks=[(0.50, 100)],
        fee_rate=0.07,
        fee_exp=1,
        max_price=0.70,
        edge_threshold=0.05,
        size_usd=15,
        min_shares=5,
        tick="0.01",
        min_order_size=5,
        book_age_s=1,
        book_exchange_age_s=1,
        max_book_age_s=30,
        feed_fresh=True,
        ptb_fresh=True,
        feed_gap=False,
        secs_left=60,
        min_secs_left=10,
        elapsed=30,
        min_secs_elapsed=3,
        side="Up",
        occupied_side=None,
        kill=False,
        rules_ok=True,
        accepting=True,
        live=False,
        live_entry_allowed=False,
        open_cost_usd=0,
        reserved_usd=0,
        max_open_cost_usd=75,
    )
    base.update(overrides)
    return base


def test_fee_matches_published_crypto_formula_and_model():
    assert gates.reference_fee_matches_model()
    assert gates.fee_usd(Decimal("100"), Decimal("0.50"), Decimal("0.07"), Decimal(1)) == Decimal("1.75000")
    assert gates.fee_usd(Decimal("100"), Decimal("0.70"), Decimal("0.07"), Decimal(1)) == Decimal("1.47000")


def test_full_stake_enters_when_edge_clears_fees():
    plan = gates.evaluate_entry(**_kw())
    assert plan["action"] == "enter"
    assert plan["cost_usd"] >= 15
    assert plan["limit_price"] <= 0.70
    assert plan["edge"] >= 0.05


def test_edge_below_threshold_after_fees_is_rejected():
    plan = gates.evaluate_entry(**_kw(fair=0.55))
    assert plan["action"] == "skip"
    assert "edge" in plan["reasons"]
    assert plan["shares"] == 0


def test_price_above_cap_is_rejected():
    plan = gates.evaluate_entry(**_kw(fair=0.95, asks=[(0.71, 100)]))
    assert plan["action"] == "skip"
    assert "price_cap" in plan["reasons"]


def test_thin_book_is_rejected_instead_of_a_smaller_stake():
    plan = gates.evaluate_entry(**_kw(asks=[(0.50, 10)]))
    assert plan["action"] == "skip"
    assert "insufficient_depth" in plan["reasons"]
    assert plan["shares"] == 0
    assert plan["cost_usd"] == 0


def test_stale_feed_book_and_price_to_beat_block_entry():
    assert "feed_stale" in gates.evaluate_entry(**_kw(feed_fresh=False))["reasons"]
    assert "feed_stale" in gates.evaluate_entry(**_kw(feed_gap=True))["reasons"]
    assert "book_stale" in gates.evaluate_entry(**_kw(book_age_s=31))["reasons"]
    assert "book_stale" in gates.evaluate_entry(**_kw(book_exchange_age_s=31))["reasons"]
    assert "ptb_stale" in gates.evaluate_entry(**_kw(ptb_fresh=False))["reasons"]


def test_one_direction_blocks_the_other_side_and_a_second_entry():
    other = gates.evaluate_entry(**_kw(side="Down", fair=0.60, occupied_side="Up"))
    same = gates.evaluate_entry(**_kw(occupied_side="Up"))
    assert "opposite_side" in other["reasons"]
    assert "already_entered" in same["reasons"]


def test_live_constraints_and_exposure_cap():
    missing = gates.evaluate_entry(**_kw(live=True, live_entry_allowed=True, tick=None))
    assert "constraints_unknown" in missing["reasons"]
    capped = gates.evaluate_entry(**_kw(
        live=True, live_entry_allowed=True, open_cost_usd=70, max_open_cost_usd=75,
    ))
    assert "exposure_cap" in capped["reasons"]
    allowed = gates.evaluate_entry(**_kw(live=True, live_entry_allowed=True, open_cost_usd=0))
    assert allowed["action"] == "enter"


def test_kill_switch_and_geoblock_and_closed_market_block_live_entries():
    assert "kill_switch" in gates.evaluate_entry(**_kw(kill=True))["reasons"]
    assert "not_accepting" in gates.evaluate_entry(**_kw(accepting=False))["reasons"]
    assert "live_not_allowed" in gates.evaluate_entry(**_kw(live=True, live_entry_allowed=False))["reasons"]
    assert "market_rules" in gates.evaluate_entry(**_kw(rules_ok=False))["reasons"]


def test_tick_conflict_and_compatible_finer_tick():
    assert gates.effective_tick("0.01", "0.001", 0.01) == Decimal("0.01")
    assert gates.effective_tick("0.01", "0.003") is None
    off = gates.evaluate_entry(**_kw(asks=[(0.505, 100)], tick="0.01"))
    assert "off_tick" in off["reasons"]


def test_btc_5m_rules_match_the_live_market_shape():
    ok, reason = gates.market_rules_ok({
        "start": 1794335400,
        "end": 1794335700,
        "slug": "btc-updown-5m-1794335400",
        "version": "v1",
        "resolution_source": "https://data.chain.link/streams/btc-usd-twap-60s-streams",
        "crypto_config": {"asset": "btc", "duration": "5m", "twapEnabled": True, "twapLookbackSeconds": 60},
        "up": "1",
        "down": "2",
    })
    assert ok, reason
    bad, _ = gates.market_rules_ok({
        "start": 1794335400, "end": 1794335700, "slug": "btc-updown-5m-1794335400",
        "version": "v2", "up": "1", "down": "2",
    })
    assert not bad


def test_freshness_helpers_reject_backfill_and_holes():
    assert gates.ptb_is_fresh(1_000, 1_003, 6)
    assert not gates.ptb_is_fresh(1_000, 1_020, 6)
    assert gates.window_has_feed_gap([0, 1, 2, 10], 0, 10, 6)
    assert gates.window_has_feed_gap([40, 41, 42], 0, 42, 6)
    assert not gates.window_has_feed_gap(range(0, 11), 0, 10, 6)
    assert not gates.window_has_feed_gap([1, 2, 3], 0, 3, 6)
    assert gates.point_is_fresh(100, 101, 104, 6)
    assert not gates.point_is_fresh(100, 130, 131, 6)


def test_geoblock_fails_closed():
    assert gates.geoblock_allows({"blocked": False, "country": "NL", "region": "NH"})[0]
    assert gates.geoblock_allows({"blocked": True, "country": "US", "region": "VA"})[1] == "geoblock_blocked"
    assert gates.geoblock_allows({"blocked": False, "country": "US", "region": "VA"})[1] == "geoblock_us"
    assert not gates.geoblock_allows(None, error="down")[0]
    assert not gates.geoblock_allows({"country": "NL"})[0]
    assert gates.proxy_env_set({"HTTP_PROXY": "http://proxy.example"}) == ["HTTP_PROXY"]
    assert gates.OFFICIAL_GEOBLOCK_URL == "https://polymarket.com/api/geoblock"
