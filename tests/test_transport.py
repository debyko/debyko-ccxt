import base64
import io
import json
import urllib.error

import pytest

from debyko_ccxt import AuthenticationError, BadResponse, DebykoCCXT, DebykoError
from debyko_ccxt.transport import Rest, _NoRedirect


class Reply(io.BytesIO):
    status = 200
    headers = {}


def install(monkeypatch, rest, *responses):
    calls = []
    replies = iter(responses)

    def open_request(request, timeout):
        calls.append((request, timeout))
        response = next(replies)
        if isinstance(response, Exception):
            raise response
        return Reply(json.dumps(response).encode())

    monkeypatch.setattr(rest.opener, "open", open_request)
    return calls


def refusal(status, payload, headers=None):
    return urllib.error.HTTPError(
        "https://api.debyko.com/v2/instruments",
        status,
        "refused",
        headers or {},
        io.BytesIO(json.dumps(payload).encode()),
    )


def test_no_key_never_implicitly_pays_or_connects(monkeypatch):
    rest = Rest(None)
    calls = install(monkeypatch, rest, {"items": []})
    with pytest.raises(AuthenticationError, match="explicitly"):
        rest.request("GET", "/v2/instruments")
    assert not calls


def test_environment_key_is_sent_only_in_authorization(monkeypatch):
    monkeypatch.setenv("DEBYKO_API_KEY", "fake-env-key")
    rest = Rest(None)
    calls = install(monkeypatch, rest, {"items": []})
    rest.request("GET", "/v2/instruments", query={"where": 'symbol = "BTCUSDT"'})
    request, timeout = calls[0]
    assert request.get_header("Authorization") == "Bearer fake-env-key"
    assert "fake-env-key" not in request.full_url
    assert "%22BTCUSDT%22" in request.full_url
    assert timeout == 20


def test_explicit_key_overrides_environment_and_post_remains_read_only(monkeypatch):
    monkeypatch.setenv("DEBYKO_API_KEY", "fake-env-key")
    rest = Rest("fake-explicit-key")
    calls = install(monkeypatch, rest, {"items": []})
    body = {"layers": ["funding"], "where": "venue = BINANCE-USDM"}
    rest.request("POST", "/v2/snapshots", body=body)
    request, _ = calls[0]
    assert request.method == "POST" and json.loads(request.data) == body
    assert request.get_header("Authorization") == "Bearer fake-explicit-key"


@pytest.mark.parametrize("header", [True, False])
def test_x402_only_pays_after_explicit_opt_in_with_caller_signature(monkeypatch, header):
    challenge = {"x402Version": 2, "accepts": [{"scheme": "exact", "network": "eip155:8453"}]}
    headers = (
        {"PAYMENT-REQUIRED": base64.b64encode(json.dumps(challenge).encode()).decode()}
        if header
        else {}
    )
    signed = []

    def sign(payment):
        signed.append(payment)
        return "fake-signed-payment"

    rest = Rest("", x402=True, payment_handler=sign)
    calls = install(monkeypatch, rest, refusal(402, challenge, headers), {"items": []})
    assert rest.request("POST", "/v2/snapshots", body={"layers": ["funding"]}) == {"items": []}
    assert signed == [challenge] and len(calls) == 2
    first, second = calls[0][0], calls[1][0]
    assert first.get_header("Payment-signature") is None
    assert second.get_header("Payment-signature") == "fake-signed-payment"
    assert second.get_header("Authorization") is None
    assert first.full_url == second.full_url and first.data == second.data


def test_x402_requires_signer_and_rejects_unknown_version(monkeypatch):
    rest = Rest("", x402=True)
    install(monkeypatch, rest, refusal(402, {"x402Version": 2}))
    with pytest.raises(AuthenticationError, match="payment_handler"):
        rest.request("GET", "/v2/instruments")
    rest.payment_handler = lambda _: "unused"
    install(monkeypatch, rest, refusal(402, {"x402Version": 1}))
    with pytest.raises(BadResponse, match="v2"):
        rest.request("GET", "/v2/instruments")


def test_payment_retry_is_bounded_and_invalid_key_never_falls_back_to_payment(monkeypatch):
    signs = []
    rest = Rest("", x402=True, payment_handler=lambda c: signs.append(c) or "fake-signature")
    calls = install(monkeypatch, rest, refusal(402, {"x402Version": 2}), refusal(402, {}))
    with pytest.raises(DebykoError) as caught:
        rest.request("GET", "/v2/instruments")
    assert caught.value.status == 402 and len(calls) == 2 and len(signs) == 1
    rest.key = "fake-invalid-key"
    calls = install(monkeypatch, rest, refusal(401, {"error": "invalid key"}))
    with pytest.raises(DebykoError) as caught:
        rest.request("GET", "/v2/instruments")
    assert caught.value.status == 401 and len(calls) == len(signs) == 1


@pytest.mark.parametrize("signature", [None, "", "bad\rheader", "bad\nheader"])
def test_payment_header_injection_is_refused(monkeypatch, signature):
    rest = Rest("", x402=True, payment_handler=lambda _: signature)
    calls = install(monkeypatch, rest, refusal(402, {"x402Version": 2}))
    with pytest.raises(AuthenticationError):
        rest.request("GET", "/v2/instruments")
    assert len(calls) == 1


def test_redirects_cannot_forward_credentials():
    assert _NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid") is None


@pytest.mark.parametrize("payload", [None, [], {}, {"items": {}}])
def test_malformed_response_envelopes_are_refused(monkeypatch, payload):
    rest = Rest("fake-key")
    install(monkeypatch, rest, payload)
    with pytest.raises(BadResponse):
        rest.request("GET", "/v2/instruments")


def test_status_is_preserved_but_secrets_are_not_in_exception_message(monkeypatch):
    rest = Rest("fake-key")
    install(monkeypatch, rest, refusal(429, {"error": "echo fake-key"}))
    with pytest.raises(DebykoError) as caught:
        rest.request("GET", "/v2/instruments")
    assert str(caught.value) == "DEBYKO returned HTTP 429"
    assert caught.value.status == 429 and caught.value.response["error"] == "echo fake-key"


def test_connection_errors_do_not_echo_request_details(monkeypatch):
    rest = Rest("fake-key")
    install(monkeypatch, rest, urllib.error.URLError("details fake-key"))
    with pytest.raises(DebykoError) as caught:
        rest.request("GET", "/v2/instruments")
    assert "fake-key" not in str(caught.value)


def test_empty_key_explicitly_ignores_environment(monkeypatch):
    monkeypatch.setenv("DEBYKO_API_KEY", "fake-env-key")
    assert DebykoCCXT("")._rest.key == ""
