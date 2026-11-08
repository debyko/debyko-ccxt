# debyko-ccxt

Four synchronous, read-only methods with [CCXT unified structures](https://github.com/ccxt/ccxt/wiki/Manual), backed by [DEBYKO](https://docs.debyko.com/): funding settlements, current funding rates, sampled open interest and liquidation events. MIT. Python ≥3.10; **zero runtime dependencies**. Contract tests use CCXT 4.5.92.

## Keep CCXT for orders; swap one line for history

After the owner publishes: `pip install debyko-ccxt`. Before publication: `pip install git+https://github.com/debyko/debyko-ccxt.git@codex/python-readonly`.

```python
import os
import ccxt
from debyko_ccxt import DebykoCCXT

orders = ccxt.binance()  # retain your existing exchange/order configuration
history = DebykoCCXT(api_key=os.environ["DEBYKO_API_KEY"], venue="BINANCE-USDM")
symbol = "BTC/USDT:USDT"
since = orders.parse8601("2026-11-08T00:00:00Z")
funding = history.fetchFundingRateHistory(symbol, since, limit=100)
oi = history.fetchOpenInterestHistory(symbol, "5m", since, limit=100)
```

CCXT is needed by your order code, not by this package. There are no order, balance or withdrawal methods.

| Method (snake_case aliases also available) | DEBYKO endpoint |
| --- | --- |
| `fetchFundingRateHistory(symbol, since=None, limit=None, params=None)` | `GET /v2/funding/settlements` |
| `fetchFundingRates(symbols=None, params=None)` | Read-only `POST /v2/snapshots` |
| `fetchOpenInterestHistory(symbol, timeframe="5m", since=None, limit=None, params=None)` | Read-only `POST /v2/snapshots/history` |
| `fetchLiquidations(symbol, since=None, limit=None, params=None)` | Read-only `POST /v2/liquidations` |

Native perpetual identities come from `GET /v2/instruments`, fetched per call. Unified symbols keep quote/settlement distinctions, e.g. `BTC/USDT:USDT` versus `BTC/USDC:USDC`. An unknown settlement identity or ambiguous mapping raises `BadSymbol`; it is never guessed.

`since` is inclusive Unix milliseconds; `params={"until": ...}` sets the inclusive historical end. Default limit: 1,000 rows. Without `since`, funding uses the API's default settlement window (currently seven days), OI uses the latest requested number of buckets, liquidations use the last hour. Current funding rejects `until`. OI intervals: `1m`, `5m`, `15m`, `30m`, `1h`, `4h`, `12h`, `1d`. Pagination/chunking is automatic, capped at **100 HTTP requests per method call**, including registry reads; change via `params={"max_requests": ...}`. Budget exhaustion raises instead of returning a silent partial answer. HTTP timeout defaults to 20 seconds.

## Structure and units

- Numbers are floats; timestamps are integer milliseconds and `datetime` is UTC ISO 8601. Every result's `info` retains DEBYKO's `venue_ts` and `status`, with source metadata. Funding history uses settlement time; OI uses `bucket_end`, retaining the actual source time separately in `info`.
- Funding is the published **fraction for the native interval**, never an 8h-normalized rate. `fundingTimestamp` is the upcoming payment time; a subsequent period/rate is not invented.
- OI amount and liquidation `contracts` are contracts, not base currency. Conversion uses the registry's contract size; base and quote values stay distinct. No historical price is invented to fill `openInterestValue`. Unknown fields are `None`.
- Non-present funding/OI retain their status and have `None` values, not zero. OI is an as-of sample, not an exchange aggregate. Liquidation side is the forced order's `buy`/`sell`, not the liquidated position's side. Event gaps remain in `info`; an empty window with known gaps raises `DataUnavailable`, as `[]` would hide them.
- These are CCXT-shaped answers, not an Exchange subclass or CCXT's exception hierarchy. API refusals raise `DebykoError` with `.status` and `.response`; there are no automatic retries of 429/5xx and no exchange fallback.

## Authentication / explicit x402

`DebykoCCXT()` reads `DEBYKO_API_KEY`. Keys go only in the Authorization header, never URLs. Redirects are refused. No key means an authentication error **before any network call** by default.

Keyless access requires `DebykoCCXT(api_key="", x402=True, payment_handler=sign)`. `sign(challenge)` must validate an **x402 v2** challenge, enforce your network/asset/payee/spending policy, and return the encoded `PAYMENT-SIGNATURE` header. The package retries that request once. It contains no wallet or signer, never pays implicitly, and never falls back from an invalid key to payment. Registry/page requests can each require payment: budget these in your signer. No real payments are made by tests.

## Which exchange windows does this avoid?

The adapter reads DEBYKO's stored observations instead of the venue's historical endpoint. It avoids the **native API** constraints below only where DEBYKO actually retained the requested data and your plan permits it. It does not extend DEBYKO retention, recover pre-collection history, or guarantee more history than the exchange has today.

| Native endpoint constraint (checked 8 Nov 2026) | When using this adapter |
| --- | --- |
| [OKX funding history: up to three months](https://www.okx.com/docs-v5/en/#public-data-rest-api-get-funding-rate-history) | Read retained DEBYKO settlements, independent of OKX's rolling window. No promise of >3 months currently recorded. |
| [Binance USD-M OI statistics: latest one month, ≤500 per call](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Open-Interest-Statistics) | Read retained DEBYKO OI samples with automatic chunking; sample semantics differ from native statistics. |
| [Bybit funding history: ≤200 per call](https://bybit-exchange.github.io/docs/v5/market/history-fund-rate) | Automatic DEBYKO settlement pagination. This is a page limit, not a claimed retention limit. |

## Develop / publish

`pip install -e '.[dev]'`, then `ruff check .`, `ruff format --check .`, `pytest`, `python -m build`, `twine check dist/*`.
Fixtures are recorded public market responses from 8 Nov 2026, with no credentials. Every test blocks external connections. CI tests Python 3.10/3.12/3.14 and installs the built wheel without dependencies.

**Not published automatically.** The owner configures PyPI Trusted Publishing for this repository, workflow `publish.yml`, environment `pypi` (prefer a required reviewer), then manually dispatches **Publish to PyPI** on `main`. The workflow reruns CI and publishes its exact built artifacts, without repository API tokens. It does not run on pushes or tags. Python first; TypeScript is deferred.
