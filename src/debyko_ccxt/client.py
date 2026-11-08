"""Four read-only methods; snake_case and CCXT's camelCase aliases."""

import json
import re
import time

from . import mapping as m
from .transport import BadResponse, BadSymbol, DataUnavailable, Rest

TIMEFRAMES = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}
SYMBOL = re.compile(r"^[A-Z0-9._-]+/[A-Z0-9._-]+:[A-Z0-9._-]+$")
DAY = 86_400_000


def integer(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _symbol(value):
    if not isinstance(value, str) or not SYMBOL.fullmatch(value):
        raise BadSymbol("Use a unified perpetual symbol, e.g. BTC/USDT:USDT")
    return value


class DebykoCCXT:
    def __init__(
        self, api_key=None, venue="BINANCE-USDM", *, x402=False, payment_handler=None, timeout=20
    ):
        if not isinstance(venue, str) or not re.fullmatch(r"[A-Z0-9-]+", venue):
            raise ValueError("venue must be a DEBYKO venue code")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0 < timeout <= 120
        ):
            raise ValueError("timeout must be in (0, 120] seconds")
        self.venue = venue
        self._rest = Rest(api_key, x402=x402, payment_handler=payment_handler, timeout=timeout)

    @staticmethod
    def _now():
        return time.time_ns() // 1_000_000

    def _call(self, params):
        params = {} if params is None else dict(params)
        if set(params) - {"until", "max_requests"}:
            raise ValueError("Supported params: until (inclusive ms), max_requests")
        until = integer(params.get("until", self._now()), "until")
        maximum = integer(params.get("max_requests", 100), "max_requests", 1)
        count = 0

        def call(method, path, **kwargs):
            nonlocal count
            count += 1
            if count > maximum:
                raise DataUnavailable(
                    "Request budget reached; narrow the window or increase max_requests"
                )
            return self._rest.request(method, path, **kwargs)

        return call, until

    def _markets(self, call, requested):
        """Paginated registry, fetched per call; no cached data or cross-key access."""
        query = {
            "where": "venue = " + self.venue,
            "kind": "perpetual",
            "include_raw": "true",
            "number_format": "string",
            "limit": 500,
        }
        result, cursors, unsupported = {}, set(), False
        while True:
            page = call("GET", "/v2/instruments", query=query.copy())
            for row in page["items"]:
                if row.get("venue") != self.venue or not row.get("instrument"):
                    continue
                try:
                    listing = m.market(row)
                except BadSymbol:
                    unsupported = True
                    continue
                key = listing["symbol"]
                if key in result and result[key]["id"] != listing["id"]:
                    raise BadSymbol("Ambiguous unified symbol: " + key)
                result[key] = listing
            cursor = page.get("next_cursor")
            if not cursor:
                break
            if cursor in cursors:
                raise BadResponse("Registry cursor repeated")
            cursors.add(cursor)
            query["cursor"] = cursor
        if requested is None:
            if unsupported:
                raise BadSymbol(
                    "Some listings lack settlement identity; request explicit supported symbols"
                )
            return result
        for key in requested:
            if key not in result:
                raise BadSymbol(f"No unambiguous supported {key} listing on {self.venue}")
        return {key: result[key] for key in requested}

    def _where(self, listing):
        return f"venue = {self.venue} and symbol = {json.dumps(listing['id'])}"

    @staticmethod
    def _limit(limit):
        return 1000 if limit is None else integer(limit, "limit", 1)

    @staticmethod
    def _item(payload, listing, venue):
        rows = [
            row
            for row in payload["items"]
            if row.get("venue") == venue and row.get("venue_symbol") == listing["id"]
        ]
        if len(rows) != 1:
            raise DataUnavailable("Expected exactly one selected listing", response=payload)
        return rows[0]

    def fetch_funding_rates(self, symbols=None, params=None):
        if params and "until" in params:
            raise ValueError("Current funding rates do not accept a historical until")
        if symbols is not None:
            if isinstance(symbols, str):
                raise BadSymbol("symbols must be a sequence of unified symbols")
            symbols = [_symbol(s) for s in symbols]
            if not symbols:
                return {}
        call, _ = self._call(params)
        markets = self._markets(call, symbols)
        result = {}
        # Bounded snapshot selections, including when a venue lists >500 markets.
        listings = list(markets.values())
        for offset in range(0, len(listings), 100):
            batch = listings[offset : offset + 100]
            ids = ", ".join(json.dumps(x["id"]) for x in batch)
            data = call(
                "POST",
                "/v2/snapshots",
                body={
                    "where": f"venue = {self.venue} and symbol in ({ids})",
                    "kind": "perpetual",
                    "layers": ["funding", "mark"],
                    "units": "published",
                    "number_format": "string",
                },
            )
            for listing in batch:
                row = self._item(data, listing, self.venue)
                result[listing["symbol"]] = m.funding_rate(row, listing)
        return result

    def fetch_funding_rate_history(self, symbol, since=None, limit=None, params=None):
        key, limit = _symbol(symbol), self._limit(limit)
        if since is not None:
            since = integer(since, "since")
        call, until = self._call(params)
        if since is not None and since > until:
            raise ValueError("since must be <= until")
        listing = self._markets(call, [key])[key]
        query = {
            "where": self._where(listing),
            "to": m.iso(until),
            "order": "asc" if since is not None else "desc",
            "number_format": "string",
        }
        if since is not None:
            query["from"] = m.iso(since - 1)  # API (from,to]; CCXT since is inclusive
        rows, cursors = [], set()
        while len(rows) < limit:
            query["limit"] = min(1000, limit - len(rows))
            data = call("GET", "/v2/funding/settlements", query=query.copy())
            if any(r.get("venue") == self.venue for r in data.get("not_recorded", [])):
                raise DataUnavailable("Settlements are not recorded for this venue", response=data)
            for row in data["items"]:
                if row.get("venue") != self.venue or row.get("venue_symbol") != listing["id"]:
                    raise BadResponse("Settlement page includes an unexpected listing")
                item = m.funding_history(row, key)
                if (since is None or since <= item["timestamp"]) and item["timestamp"] <= until:
                    rows.append(item)
            cursor = data.get("next_cursor")
            if not cursor:
                break
            if cursor in cursors:
                raise BadResponse("Settlement cursor repeated")
            cursors.add(cursor)
            query["cursor"] = cursor
        return sorted(rows[:limit], key=lambda r: r["timestamp"])

    def fetch_open_interest_history(
        self, symbol, timeframe="5m", since=None, limit=None, params=None
    ):
        key, limit = _symbol(symbol), self._limit(limit)
        if timeframe not in TIMEFRAMES:
            raise ValueError("Unsupported DEBYKO sample interval")
        step = TIMEFRAMES[timeframe]
        call, until = self._call(params)
        end = until // step * step
        first = end - (limit - 1) * step if since is None else integer(since, "since")
        if first > until:
            raise ValueError("since must be <= until")
        start = ((first + step - 1) // step) * step
        listing = self._markets(call, [key])[key]
        result = []
        while start <= end and len(result) < limit:
            last = min(end, start + (min(2000, limit - len(result)) - 1) * step)
            data = call(
                "POST",
                "/v2/snapshots/history",
                body={
                    "where": self._where(listing),
                    "from": m.iso(start - 1),
                    "to": m.iso(last),
                    "interval": timeframe,
                    "layers": ["oi"],
                    "units": "published",
                    "number_format": "string",
                    "include_gaps": True,
                },
            )
            layer = self._item(data, listing, self.venue).get("layers", {}).get("oi")
            if not isinstance(layer, dict) or not isinstance(layer.get("rows"), list):
                raise BadResponse("Missing OI sample rows")
            rows = layer["rows"]
            expected = (last - start) // step + 1
            if len(rows) != expected:
                raise BadResponse(
                    "OI response omitted expected buckets; refusing an incomplete history"
                )
            for index, row in enumerate(rows):
                if m.milliseconds(row.get("bucket_end")) != start + index * step:
                    raise BadResponse("OI sample grid changed")
                # Historical units belong to the returned layer, not today's instrument.
                result.append(m.open_interest(row, layer.get("units", {}).get("oi"), listing))
            start = last + step
        return result

    def fetch_liquidations(self, symbol, since=None, limit=None, params=None):
        key, limit = _symbol(symbol), self._limit(limit)
        call, until = self._call(params)
        start = until - 3_600_000 if since is None else integer(since, "since") - 1
        if start >= until:
            raise ValueError("since must be <= until")
        listing = self._markets(call, [key])[key]
        result, seen = [], set()
        window = DAY
        while start < until and (since is None or len(result) < limit):
            end = min(until, start + window)
            data = call(
                "POST",
                "/v2/liquidations",
                body={
                    "where": self._where(listing),
                    "from": m.iso(start),
                    "to": m.iso(end),
                    "limit": 1000,
                    "units": "published",
                    "number_format": "string",
                    "include_gaps": True,
                },
            )
            item = self._item(data, listing, self.venue)
            if item.get("status") != "present":
                raise DataUnavailable(
                    "Liquidations are unavailable for this listing", response=data
                )
            if item.get("truncated"):
                if end - start <= 1:
                    raise DataUnavailable(
                        "Liquidations cannot be paged without losing tied events", response=data
                    )
                window = max(1, (end - start) // 2)
                continue
            if item.get("gaps") and not item.get("events"):
                raise DataUnavailable("Empty liquidation window has data gaps", response=data)
            for event in item.get("events", []):
                ts = m.milliseconds(event.get("event_time"))
                if (
                    not m.instant(m.iso(start))
                    < m.instant(event["event_time"])
                    <= m.instant(m.iso(end))
                ):
                    raise BadResponse("Liquidation event is outside the requested window")
                if since is not None and ts < since:
                    continue
                # Only venue IDs are identities. Distinct equal-size/time events
                # without IDs must not be deduplicated by their values.
                uid = event.get("venue_uid")
                if uid is not None and uid in seen:
                    continue
                if uid is not None:
                    seen.add(uid)
                result.append(m.liquidation(event, item, listing))
            start = end
            window = DAY
        result.sort(key=lambda r: r["timestamp"])
        return result[:limit] if since is not None else result[-limit:]

    fetchFundingRateHistory = fetch_funding_rate_history
    fetchFundingRates = fetch_funding_rates
    fetchOpenInterestHistory = fetch_open_interest_history
    fetchLiquidations = fetch_liquidations
