"""CCXT 4.5.92 contract, using recorded DEBYKO responses from 8 Nov 2026.

MCP forwards instruments/snapshots/settlements/history response envelopes;
liquidations.json was a bounded read-only REST capture (12 BTCUSDT events,
15:00–16:00 UTC). All edge-case mutations are in-memory copies of recordings.
No API credentials are recorded; tests never reach the network.
"""

import copy
from decimal import Decimal

import ccxt
import pytest
from ccxt.base.types import FundingRate, FundingRateHistory, Liquidation, OpenInterest

from debyko_ccxt import BadResponse, BadSymbol, DataUnavailable, DebykoCCXT
from debyko_ccxt import mapping as m

SYMBOL = "BTC/USDT:USDT"
START = m.milliseconds("2026-11-08T15:05:00Z")
END = m.milliseconds("2026-11-08T16:00:00Z")


def test_funding_history_exact_ccxt_shape_and_native_parser_parity(api):
    client, responses, calls = api
    rows = client.fetchFundingRateHistory(SYMBOL, params={"until": END})
    native = ccxt.binance()
    native.set_markets(
        [
            {
                "id": "BTCUSDT",
                "symbol": SYMBOL,
                "base": "BTC",
                "quote": "USDT",
                "settle": "USDT",
                "spot": False,
                "swap": True,
                "type": "swap",
            }
        ]
    )
    for row, source in zip(rows, responses["/v2/funding/settlements"]["items"], strict=True):
        assert set(row) == set(FundingRateHistory.__annotations__)
        expected = native.parse_funding_rate_history(
            {
                "symbol": "BTCUSDT",
                "fundingRate": source["rate"],
                "fundingTime": m.milliseconds(source["time"]),
            }
        )
        assert {k: v for k, v in row.items() if k != "info"} == {
            k: v for k, v in expected.items() if k != "info"
        }
        assert row["info"]["venue_ts"] == source["time"]
        assert row["info"]["status"] == source["status"]
        assert row["info"]["rate_source"] == source["rate_source"]
    assert calls[-1][0:2] == ("GET", "/v2/funding/settlements")
    assert all('symbol = "BTCUSDT"' in c[2]["query"]["where"] for c in calls[1:])


def test_current_rates_use_native_interval_and_do_not_invent_next_period(api):
    client, responses, _ = api
    row = client.fetchFundingRates([SYMBOL])[SYMBOL]
    assert set(row) == set(FundingRate.__annotations__)
    values = responses["/v2/snapshots"]["items"][0]["layers"]["funding"]["values"]
    assert row["fundingRate"] == float(values["funding"])
    assert row["fundingTimestamp"] == m.milliseconds(values["next_funding_at"])
    assert row["fundingDatetime"] == m.iso(row["fundingTimestamp"])
    assert row["nextFundingTimestamp"] is None and row["nextFundingRate"] is None
    assert row["interestRate"] is None and row["previousFundingRate"] is None
    assert row["interval"] == "8h"
    assert row["timestamp"] == m.milliseconds(row["info"]["venue_ts"])
    expected = ccxt.binance().parse_funding_rate(
        {
            "symbol": "BTCUSDT",
            "lastFundingRate": values["funding"],
            "markPrice": row["markPrice"],
            "indexPrice": row["indexPrice"],
            "time": row["timestamp"],
            "nextFundingTime": row["fundingTimestamp"],
            "fundingIntervalHours": 8,
        },
        {"symbol": SYMBOL},
    )
    assert {k: v for k, v in row.items() if k != "info"} == {
        k: v for k, v in expected.items() if k != "info"
    }


def test_oi_history_exact_shape_units_and_ccxt_parser_parity(api):
    client, responses, calls = api
    result = client.fetchOpenInterestHistory(SYMBOL, "5m", START, 12, {"until": END})
    original = responses["/v2/snapshots/history"]["items"][0]["layers"]["oi"]["rows"]
    for row, source in zip(result, original, strict=True):
        assert set(row) == set(OpenInterest.__annotations__)
        assert row["timestamp"] == m.milliseconds(source["bucket_end"])
        assert row["info"]["venue_ts"] == source["venue_ts"]
        assert row["timestamp"] != m.milliseconds(row["info"]["venue_ts"])
        expected = ccxt.binance().parse_open_interest(
            {"symbol": "BTCUSDT", "time": row["timestamp"], "openInterest": source["values"]["oi"]},
            {"id": "BTCUSDT", "symbol": SYMBOL, "inverse": False},
        )
        assert row["openInterestAmount"] == expected["openInterestAmount"]
        assert row["baseVolume"] == expected["baseVolume"]
        assert row["openInterestValue"] is None and row["quoteVolume"] is None
    assert m.instant(calls[-1][2]["body"]["from"]) < m.instant(m.iso(START))


def test_okx_contracts_are_not_mislabelled_as_base_currency(api, fixture):
    client, responses, _ = api
    client.venue = "OKX-PERP"
    responses["/v2/instruments"] = fixture("okx-instruments")
    responses["/v2/snapshots/history"] = fixture("okx-oi")
    row = client.fetch_open_interest_history(SYMBOL, "5m", START, 12, {"until": END})[0]
    source = responses["/v2/snapshots/history"]["items"][0]["layers"]["oi"]
    amount = Decimal(source["rows"][0]["values"]["oi"])
    assert source["units"]["oi"] == "contracts"
    assert row["openInterestAmount"] == float(amount)
    assert row["baseVolume"] == float(amount * Decimal("0.01"))
    assert row["openInterestValue"] is None


def test_liquidations_have_exact_fields_side_units_and_ms_timestamps(api):
    client, responses, _ = api
    rows = client.fetchLiquidations(
        SYMBOL, m.milliseconds("2026-11-08T15:00:00Z"), params={"until": END}
    )
    events = responses["/v2/liquidations"]["items"][0]["events"]
    assert len(rows) == 12
    for row, source in zip(rows, events, strict=True):
        assert set(row) == set(Liquidation.__annotations__)
        assert row["timestamp"] == m.milliseconds(source["event_time"])
        assert row["datetime"] == m.iso(row["timestamp"])
        assert row["info"]["venue_ts"] == source["event_time"]
        assert row["info"]["status"] == "present"
        assert row["side"] == source["side"]  # forced-order side, never position side
        assert row["baseValue"] == float(source["size"])
        assert row["contracts"] == row["baseValue"] and row["contractSize"] == 1
        assert row["quoteValue"] == float(Decimal(source["size"]) * Decimal(source["price"]))


@pytest.mark.parametrize("status", ["stale", "missing", "off", "not_published"])
def test_non_present_funding_never_becomes_a_live_number(status, api):
    client, responses, _ = api
    layer = responses["/v2/snapshots"]["items"][0]["layers"]["funding"]
    layer.update(status=status, reason="source_error")
    row = client.fetch_funding_rates([SYMBOL])[SYMBOL]
    assert row["fundingRate"] is None and row["fundingTimestamp"] is None
    assert row["info"]["status"] == status and row["info"]["reason"] == "source_error"


def test_missing_settlement_is_not_zero(api):
    client, responses, _ = api
    responses["/v2/funding/settlements"]["items"][0].update(status="missing", rate="999")
    row = client.fetch_funding_rate_history(SYMBOL, params={"until": END})[0]
    assert row["fundingRate"] is None and row["info"]["status"] == "missing"


def test_missing_oi_retains_its_sample_slot_and_reason(api):
    client, responses, _ = api
    source = responses["/v2/snapshots/history"]["items"][0]["layers"]["oi"]["rows"][0]
    source.clear()
    source.update(bucket_end=m.iso(START), status="gap", reason="collector_gap")
    row = client.fetch_open_interest_history(SYMBOL, "5m", START, 12, {"until": END})[0]
    assert row["openInterestAmount"] is None and row["baseVolume"] is None
    assert row["info"]["venue_ts"] is None and row["info"]["status"] == "gap"


def test_unknown_contract_size_does_not_invent_contract_count(api):
    client, responses, _ = api
    responses["/v2/instruments"]["items"][0]["contract_size"] = None
    row = client.fetch_open_interest_history(SYMBOL, "5m", START, 12, {"until": END})[0]
    assert row["openInterestAmount"] is None and row["baseVolume"] is not None


def test_native_quote_and_multiplier_identity_are_preserved(api):
    client, responses, _ = api
    listing = responses["/v2/instruments"]["items"][0]
    usdc = copy.deepcopy(listing)
    usdc.update(quote="USDC", venue_symbol="BTCUSDC")
    usdc["raw"].update(quoteAsset="USDC", marginAsset="USDC")
    responses["/v2/instruments"]["items"].append(usdc)
    row = client.fetch_funding_rates([SYMBOL])[SYMBOL]
    assert row["symbol"] == SYMBOL
    listing.update(
        base="PEPE", base_venue_alias="1000PEPE", multiplier="1000", venue_symbol="1000PEPEUSDT"
    )
    listing["raw"]["baseAsset"] = "1000PEPE"
    assert m.market(listing)["symbol"] == "1000PEPE/USDT:USDT"


def test_mixed_case_native_currency_uses_ccxt_unified_case(fixture):
    listing = fixture("instruments")["items"][0]
    listing.update(
        venue="HYPERLIQUID",
        base="PEPE",
        base_venue_alias="kPEPE",
        quote="USDC",
        venue_symbol="kPEPE",
        raw={},
    )
    expected = ccxt.hyperliquid().parse_market({"name": "kPEPE", "szDecimals": 0})
    assert m.market(listing)["symbol"] == expected["symbol"] == "KPEPE/USDC:USDC"


@pytest.mark.parametrize(
    "unit,expected",
    [
        ("contracts", (Decimal("2"), None, Decimal("200"))),
        ("quote", (Decimal("0.02"), None, Decimal("2"))),
    ],
)
def test_inverse_quantities_keep_quote_contracts_separate(unit, expected):
    listing = {"inverse": True, "size": Decimal("100")}
    assert m.quantities("2", unit, listing) == expected
    assert m.quantities("2", "contracts", listing, "50000") == (
        Decimal("2"),
        Decimal("0.004"),
        Decimal("200"),
    )


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", True, "garbage"])
def test_invalid_numbers_fail_instead_of_becoming_zero(value):
    with pytest.raises(BadResponse):
        m.number(value)


def test_zero_and_negative_funding_are_preserved():
    for value in ("0", "-0.000002"):
        row = m.funding_history(
            {"time": "2026-11-08T00:00:00Z", "rate": value, "status": "present"}, SYMBOL
        )
        assert row["fundingRate"] == float(value)
    assert m.milliseconds("2026-11-08T01:02:03.456789Z") % 1000 == 456


def test_registry_pages_and_settlement_pages_preserve_query_and_order(api):
    client, responses, calls = api
    original = copy.deepcopy(responses["/v2/funding/settlements"])
    events = original["items"]

    def pages(method, path, query):
        data = copy.deepcopy(original)
        data["items"] = events[:1] if "cursor" not in query else events[1:]
        data["next_cursor"] = "page-2" if "cursor" not in query else None
        return data

    responses["/v2/funding/settlements"] = pages
    since = m.milliseconds(events[0]["time"])
    result = client.fetch_funding_rate_history(SYMBOL, since, len(events), {"until": END})
    assert len(result) == len(events) and result[0]["timestamp"] == since
    queries = [kw["query"] for _, path, kw in calls if path.endswith("settlements")]
    assert len(queries) == 2
    assert queries[0]["from"] == queries[1]["from"]
    assert queries[0]["to"] == queries[1]["to"]
    assert queries[0]["order"] == queries[1]["order"] == "asc"
    assert "cursor" not in queries[0] and queries[1]["cursor"] == "page-2"


def test_oi_windows_split_at_the_api_2000_row_limit(api):
    client, responses, calls = api
    template = copy.deepcopy(responses["/v2/snapshots/history"])
    source = template["items"][0]["layers"]["oi"]["rows"][0]

    def windows(method, path, body):
        data = copy.deepcopy(template)
        start = (m.milliseconds(body["from"]) // 300000 + 1) * 300000
        end = m.milliseconds(body["to"])
        data["items"][0]["layers"]["oi"]["rows"] = [
            dict(source, bucket_end=m.iso(at)) for at in range(start, end + 1, 300000)
        ]
        return data

    responses["/v2/snapshots/history"] = windows
    since = END - 2000 * 300000
    assert (
        len(client.fetch_open_interest_history(SYMBOL, "5m", since, 2001, {"until": END})) == 2001
    )
    windows_called = [kw["body"] for _, path, kw in calls if path.endswith("/history")]
    assert len(windows_called) == 2
    assert all(
        (m.milliseconds(b["to"]) - m.milliseconds(b["from"])) // 300000 < 2001
        for b in windows_called
    )


def test_repeated_cursor_and_request_budget_never_return_silent_partial_history(api):
    client, responses, _ = api
    responses["/v2/funding/settlements"].update(items=[], next_cursor="loop")
    with pytest.raises(BadResponse, match="repeated"):
        client.fetch_funding_rate_history(SYMBOL)
    with pytest.raises(DataUnavailable, match="budget"):
        client.fetch_funding_rates([SYMBOL], {"max_requests": 1})


def test_unsupported_or_ambiguous_symbols_do_not_fall_back_to_btc_perp(api):
    client, responses, _ = api
    with pytest.raises(BadSymbol):
        client.fetch_funding_rates(["BTC/USDC:USDC"])
    duplicate = copy.deepcopy(responses["/v2/instruments"]["items"][0])
    duplicate["venue_symbol"] = "OTHER_BTCUSDT"
    responses["/v2/instruments"]["items"].append(duplicate)
    with pytest.raises(BadSymbol, match="Ambiguous"):
        client.fetch_funding_rates([SYMBOL])


def test_gap_empty_liquidation_window_is_not_reported_as_zero(api):
    client, responses, _ = api
    item = responses["/v2/liquidations"]["items"][0]
    item.update(events=[], gaps=[{"reason": "collector_gap"}])
    with pytest.raises(DataUnavailable, match="gaps"):
        client.fetch_liquidations(SYMBOL, START, params={"until": END})
    item["gaps"] = []
    assert client.fetch_liquidations(SYMBOL, START, params={"until": END}) == []


def test_identical_events_without_venue_ids_are_not_lost(api):
    client, responses, _ = api
    item = responses["/v2/liquidations"]["items"][0]
    event = item["events"][0]
    item["events"] = [copy.deepcopy(event), copy.deepcopy(event)]
    since = m.milliseconds(event["event_time"])
    rows = client.fetch_liquidations(SYMBOL, since, 10, {"until": END})
    assert len(rows) == 2


def test_liquidation_truncation_splits_instead_of_skipping_older_events(api):
    client, responses, calls = api
    original = copy.deepcopy(responses["/v2/liquidations"])
    event = original["items"][0]["events"][0]
    since = m.milliseconds(event["event_time"])

    def window(method, path, body):
        data = copy.deepcopy(original)
        start, end = m.milliseconds(body["from"]), m.milliseconds(body["to"])
        truncated = end - start > 60_000
        item = data["items"][0]
        item.update(truncated=truncated, events=[event] if start < since <= end else [])
        return data

    responses["/v2/liquidations"] = window
    rows = client.fetch_liquidations(SYMBOL, since, 1, {"until": since + 600_000})
    assert len(rows) == 1 and rows[0]["timestamp"] == since
    assert sum(path == "/v2/liquidations" for _, path, _ in calls) > 1


def test_same_ms_distinct_microsecond_events_survive(api):
    client, responses, _ = api
    item = responses["/v2/liquidations"]["items"][0]
    event = copy.deepcopy(item["events"][0])
    event["event_time"] = "2026-11-08T15:00:50.053500Z"
    item["events"] = [event]
    rows = client.fetch_liquidations(SYMBOL, m.milliseconds(event["event_time"]), 1, {"until": END})
    assert len(rows) == 1


@pytest.mark.parametrize("bad", [True, -1, 1.5, "1000"])
def test_invalid_since_and_limit_fail_before_api(bad, api):
    client, _, calls = api
    with pytest.raises(ValueError):
        client.fetch_funding_rate_history(SYMBOL, since=bad)
    assert not calls


def test_no_trading_methods_and_both_naming_conventions():
    client = DebykoCCXT("fake")
    for name in ("createOrder", "create_order", "cancelOrder", "withdraw", "fetchBalance"):
        assert not hasattr(client, name)
    assert client.fetchFundingRates == client.fetch_funding_rates
    assert client.fetchOpenInterestHistory == client.fetch_open_interest_history


def test_current_funding_cannot_silently_answer_a_historical_request(api):
    client, _, calls = api
    with pytest.raises(ValueError, match="historical"):
        client.fetch_funding_rates([SYMBOL], {"until": END})
    assert not calls


def test_registry_pagination_retains_selection(api):
    client, responses, calls = api
    original = copy.deepcopy(responses["/v2/instruments"])

    def pages(method, path, query):
        return original if "cursor" in query else {"items": [], "next_cursor": "registry-2"}

    responses["/v2/instruments"] = pages
    assert client.fetch_funding_rates([SYMBOL])[SYMBOL]["symbol"] == SYMBOL
    first, second = calls[:2]
    assert first[2]["query"]["where"] == second[2]["query"]["where"]
    assert second[2]["query"]["cursor"] == "registry-2"


def test_wrong_units_are_rejected_not_guessed(api):
    client, responses, _ = api
    funding = responses["/v2/snapshots"]["items"][0]["layers"]["funding"]
    funding["units"]["funding"] = "percent"
    with pytest.raises(BadResponse, match="fraction"):
        client.fetch_funding_rates([SYMBOL])
    oi = responses["/v2/snapshots/history"]["items"][0]["layers"]["oi"]
    oi["units"]["oi"] = "guess"
    with pytest.raises(BadResponse, match="unit"):
        client.fetch_open_interest_history(SYMBOL, "5m", START, 12, {"until": END})


def test_oi_omitted_grid_slot_is_not_silently_a_shorter_history(api):
    client, responses, _ = api
    rows = responses["/v2/snapshots/history"]["items"][0]["layers"]["oi"]["rows"]
    rows.pop()
    with pytest.raises(BadResponse, match="omitted"):
        client.fetch_open_interest_history(SYMBOL, "5m", START, 12, {"until": END})
