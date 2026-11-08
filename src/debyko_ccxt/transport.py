"""Header-only authentication and explicitly opted-in, caller-signed x402 v2."""

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request


class DebykoError(Exception):
    def __init__(self, message, *, status=None, response=None):
        super().__init__(message)
        self.status = status
        self.response = response


class AuthenticationError(DebykoError):
    pass


class BadResponse(DebykoError):
    pass


class BadSymbol(DebykoError):
    pass


class DataUnavailable(DebykoError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Rest:
    def __init__(self, api_key, *, x402=False, payment_handler=None, timeout=20):
        self.key = os.getenv("DEBYKO_API_KEY", "") if api_key is None else api_key
        self.x402 = x402 is True
        self.payment_handler = payment_handler
        self.timeout = timeout
        self.opener = urllib.request.build_opener(_NoRedirect())

    def request(self, method, path, *, body=None, query=None):
        if not self.key and not self.x402:
            raise AuthenticationError("Set DEBYKO_API_KEY or explicitly opt in to x402")
        url = "https://api.debyko.com" + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        data = None if body is None else json.dumps(body).encode()
        for attempt in range(2):
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            error = None
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    status, response_headers, raw = (
                        response.status,
                        response.headers,
                        response.read(),
                    )
            except urllib.error.HTTPError as exc:
                status, response_headers, raw = exc.code, exc.headers, exc.read()
                exc.close()
            except (OSError, urllib.error.URLError):
                error = DebykoError("DEBYKO request failed (connection/timeout)")
            if error:
                raise error
            try:
                payload = json.loads(raw)
            except (ValueError, UnicodeError):
                raise BadResponse("DEBYKO returned invalid JSON", status=status) from None
            if status == 402 and not self.key and self.x402 and attempt == 0:
                if self.payment_handler is None:
                    raise AuthenticationError(
                        "x402 needs a caller-supplied payment_handler", status=402
                    )
                required = response_headers.get("PAYMENT-REQUIRED")
                try:
                    challenge = (
                        json.loads(base64.b64decode(required, validate=True))
                        if required
                        else payload
                    )
                except (ValueError, TypeError):
                    raise BadResponse("Invalid x402 payment challenge") from None
                if not isinstance(challenge, dict) or challenge.get("x402Version") != 2:
                    raise BadResponse("Only x402 v2 payment challenges are supported")
                # The caller's signer decides network/asset/payee/spending limits.
                # Never load a wallet, sign, purchase a plan, or pay by default.
                signature = self.payment_handler(challenge)
                if (
                    not isinstance(signature, str)
                    or not signature
                    or "\n" in signature
                    or "\r" in signature
                ):
                    raise AuthenticationError(
                        "payment_handler must return a PAYMENT-SIGNATURE value"
                    )
                headers["PAYMENT-SIGNATURE"] = signature
                continue
            if status != 200:
                # No response-body interpolation: it may echo a key or signature.
                raise DebykoError(f"DEBYKO returned HTTP {status}", status=status, response=payload)
            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                raise BadResponse("DEBYKO response has no items array")
            return payload
        raise AuthenticationError("x402 payment was refused")
