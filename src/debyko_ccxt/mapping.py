"""Exact field shapes, decimal unit conversions, and native listing identity."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from .transport import BadResponse, BadSymbol

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def instant(value):
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            raise ValueError
        return instant
    except (ValueError, TypeError, AttributeError):
        raise BadResponse("Invalid UTC timestamp") from None


def milliseconds(value):
    return None if value is None else (instant(value) - EPOCH) // timedelta(milliseconds=1)


def iso(value):
    return (
        None
        if value is None
        else (EPOCH + timedelta(milliseconds=value))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def decimal(value):
    if value is None:
        return None
    try:
        if isinstance(value, bool):
            raise ValueError
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError
        return result
    except (InvalidOperation, ValueError):
        raise BadResponse("Invalid finite decimal") from None


def number(value):
    result = decimal(value)
    if result is None:
        return None
    as_float = float(result)
    if not decimal(as_float).is_finite():
        raise BadResponse("Number is outside the CCXT float range")
    return as_float


def market(row):
    """Use native base/settlement facts; do not collapse different quote listings."""
    raw = row.get("raw") or {}
    base = (
        raw.get("baseAsset")
        or raw.get("baseCoin")
        or row.get("base_venue_alias")
        or row.get("base")
    )
    base = base.upper() if isinstance(base, str) else None
    base = {"XBT": "BTC"}.get(base, base)
    quote = row.get("quote")
    quote = quote.upper() if isinstance(quote, str) else None
    settle = raw.get("marginAsset") or raw.get("settleCoin") or raw.get("settleCcy")
    if not settle and row["venue"] in {
        "BINANCE-USDM",
        "HYPERLIQUID",
        "COINBASE-PERP",
        "LIGHTER-PERP",
    }:
        settle = quote  # these venues' linear perpetuals settle in their quote unit
    settle = settle.upper() if isinstance(settle, str) else None
    settle = {"XBT": "BTC"}.get(settle, settle)
    if (
        not base
        or not quote
        or settle not in (base, quote)
        or not row.get("instrument")
        or row.get("kind") != "perpetual"
    ):
        raise BadSymbol("Listing has no supported perpetual base/quote/settlement identity")
    size = decimal(row.get("contract_size"))
    if size is not None and size <= 0:
        raise BadResponse("contract_size must be positive")
    return {
        "symbol": f"{base}/{quote}:{settle}",
        "id": row["venue_symbol"],
        "inverse": settle == base and base != quote,
        "size": size,
        "record": row,
    }


def info(row, *, venue_ts=None, **extra):
    return {
        **row,
        "venue_ts": row.get("venue_ts", venue_ts),
        "status": row.get("status", "present"),
        **extra,
    }


def funding_history(row, symbol):
    timestamp = milliseconds(row["time"])
    return {
        "symbol": symbol,
        "fundingRate": number(row.get("rate")) if row["status"] == "present" else None,
        "timestamp": timestamp,
        "datetime": iso(timestamp),
        "info": info(row, venue_ts=row["time"]),
    }


def funding_rate(row, listing):
    funding = row.get("layers", {}).get("funding", {"status": "missing"})
    mark = row.get("layers", {}).get("mark", {"status": "missing"})
    values = funding.get("values", {}) if funding.get("status") == "present" else {}
    marks = mark.get("values", {}) if mark.get("status") == "present" else {}
    if values.get("funding") is not None and funding.get("units", {}).get("funding") != "fraction":
        raise BadResponse("Funding rate is not a fraction")
    timestamp = milliseconds(funding.get("venue_ts"))
    funding_at = milliseconds(values.get("next_funding_at"))
    return {
        "symbol": listing["symbol"],
        "info": info(funding, listing=row, mark=mark),
        "timestamp": timestamp,
        "datetime": iso(timestamp),
        "fundingRate": number(values.get("funding")),
        "markPrice": number(marks.get("mark")),
        "indexPrice": number(marks.get("index")),
        "interestRate": None,
        "estimatedSettlePrice": None,
        "fundingTimestamp": funding_at,
        "fundingDatetime": iso(funding_at),
        "nextFundingRate": None,
        "nextFundingTimestamp": None,
        "nextFundingDatetime": None,
        "previousFundingRate": None,
        "previousFundingTimestamp": None,
        "previousFundingDatetime": None,
        "interval": values.get("funding_interval"),
    }


def quantities(value, unit, listing, price=None):
    """(contracts, base, quote); a missing conversion input remains None."""
    value, price, size = decimal(value), decimal(price), listing["size"]
    contracts = base = quote = None
    if value is not None:
        if unit == "contracts":
            contracts = value
            if size is not None:
                if listing["inverse"]:
                    quote = value * size
                else:
                    base = value * size
        elif unit == "base":
            base = value
        elif unit == "quote":
            quote = value
        else:
            raise BadResponse("Unknown size/OI unit; refusing to guess")
        if price is not None and price > 0:
            if base is not None:
                quote = base * price
            elif quote is not None:
                base = quote / price
        if contracts is None and size is not None:
            numerator = quote if listing["inverse"] else base
            contracts = None if numerator is None else numerator / size
    return contracts, base, quote


def open_interest(row, unit, listing):
    value = row.get("values", {}).get("oi") if row.get("status") == "present" else None
    contracts, base, quote = quantities(value, unit, listing)
    timestamp = milliseconds(row["bucket_end"])
    return {
        "symbol": listing["symbol"],
        "timestamp": timestamp,
        "datetime": iso(timestamp),
        "openInterestAmount": number(contracts),
        "openInterestValue": number(quote),
        "baseVolume": number(base),
        "quoteVolume": number(quote),
        "info": info(row, unit=unit),
    }


def liquidation(event, item, listing):
    if item.get("units", {}).get("price") != "quote":
        raise BadResponse("Unknown liquidation price unit")
    price = event.get("price")
    contracts, base, quote = quantities(
        event.get("size"), item["units"].get("size"), listing, price
    )
    timestamp = milliseconds(event["event_time"])
    side = event.get("side")
    if side not in ("buy", "sell", None):
        raise BadResponse("Unknown forced-order side")
    return {
        "symbol": listing["symbol"],
        "timestamp": timestamp,
        "datetime": iso(timestamp),
        "price": number(price),
        "baseValue": number(base),
        "quoteValue": number(quote),
        "contracts": number(contracts),
        "contractSize": number(listing["size"]),
        "side": side,
        "info": info(
            event,
            venue_ts=event["event_time"],
            status=item["status"],
            gaps=item.get("gaps", []),
            units=item["units"],
            venue=item["venue"],
            venue_symbol=item["venue_symbol"],
            retention_from=item.get("retention_from"),
        ),
    }
