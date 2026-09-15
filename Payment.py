"""Payment capture against a Stripe-style provider.

The important property here is that a retry must never charge a customer
twice. Every attempt for a given payment reuses one idempotency key, so the
provider collapses duplicates on its side rather than us trying to detect them
on ours after the fact.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from RetryPolicy import PAYMENTS_POLICY, PermanentError, RetryableError, RetryPolicy

log = logging.getLogger(__name__)

API_BASE = os.environ.get("PAYMENTS_API_BASE", "https://api.provider.test/v2")

# Provider status codes we know how to interpret. Anything else is treated as
# permanent, because guessing at an unknown failure is how double charges start.
TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}
DECLINE_CODES = {
    "card_declined",
    "insufficient_funds",
    "expired_card",
    "incorrect_cvc",
}


class PaymentDeclined(PermanentError):
    """The issuer said no. Retrying will not change that."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass
class PaymentRequest:
    order_id: str
    amount: Decimal
    currency: str = "USD"
    customer_id: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.amount <= 0:
            raise ValueError("amount must be positive")
        if len(self.currency) != 3:
            raise ValueError("currency must be a 3-letter ISO code")
        self.currency = self.currency.upper()

    def minor_units(self) -> int:
        """Convert to the integer minor unit the provider expects.

        Float arithmetic is avoided deliberately: 19.99 * 100 is 1998.9999...
        in binary floating point, and a silent off-by-one cent in a payments
        path is the kind of bug that is found by an auditor, not a test.
        """
        quantized = self.amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return int(quantized * 100)

    def idempotency_key(self) -> str:
        """Stable per order and amount, so a retry is recognised as the same
        payment while a genuinely different amount is not."""
        digest = hashlib.sha256(
            f"{self.order_id}:{self.minor_units()}:{self.currency}".encode()
        ).hexdigest()
        return f"pay_{digest[:32]}"


@dataclass
class PaymentResult:
    payment_id: str
    status: str
    amount_minor: int
    currency: str
    captured_at: datetime
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status in {"succeeded", "captured"}


class PaymentClient:
    def __init__(
        self,
        api_key: str | None = None,
        policy: RetryPolicy = PAYMENTS_POLICY,
        session: Any | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("PAYMENTS_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("PAYMENTS_API_KEY is not set")
        self.policy = policy
        self.session = session or self._default_session()

    @staticmethod
    def _default_session() -> Any:
        import requests  # imported lazily so tests can inject a fake session

        session = requests.Session()
        session.headers.update({"User-Agent": "payments-worker/1.4"})
        return session

    def capture(self, request: PaymentRequest) -> PaymentResult:
        """Capture a payment, retrying only what is safe to retry."""
        key = request.idempotency_key()
        log.info(
            "capturing order=%s amount=%s %s key=%s",
            request.order_id, request.amount, request.currency, key,
        )
        return self.policy.call(
            lambda: self._attempt_capture(request, key),
            describe=f"capture order={request.order_id}",
        )

    def _attempt_capture(self, request: PaymentRequest, key: str) -> PaymentResult:
        payload = {
            "amount": request.minor_units(),
            "currency": request.currency,
            "customer": request.customer_id,
            "capture": True,
            "metadata": {**request.metadata, "order_id": request.order_id},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Idempotency-Key": key,
            "X-Request-Id": str(uuid.uuid4()),
        }

        try:
            response = self.session.post(
                f"{API_BASE}/charges", json=payload, headers=headers, timeout=15
            )
        except Exception as exc:  # network-level: connection reset, DNS, timeout
            raise RetryableError(f"transport failure: {exc}") from exc

        if response.status_code in TRANSIENT_STATUS:
            raise RetryableError(f"provider returned {response.status_code}")

        body = self._decode(response)

        if response.status_code >= 400:
            code = str(body.get("error", {}).get("code", "unknown"))
            message = str(body.get("error", {}).get("message", response.text[:200]))
            if code in DECLINE_CODES:
                raise PaymentDeclined(code, message)
            raise PermanentError(f"provider rejected request ({code}): {message}")

        return PaymentResult(
            payment_id=str(body.get("id", "")),
            status=str(body.get("status", "unknown")),
            amount_minor=int(body.get("amount", request.minor_units())),
            currency=str(body.get("currency", request.currency)).upper(),
            captured_at=datetime.now(timezone.utc),
            raw=body,
        )

    @staticmethod
    def _decode(response: Any) -> dict[str, Any]:
        try:
            parsed = response.json()
        except ValueError as exc:
            # A 200 with an unparseable body means we cannot tell whether the
            # charge landed. Retrying is safe only because of the idempotency
            # key above; without it this branch would be a double-charge risk.
            raise RetryableError(f"unparseable response body: {exc}") from exc
        return parsed if isinstance(parsed, dict) else {"data": parsed}


def capture_order(order_id: str, amount: str, currency: str = "USD") -> PaymentResult:
    """Convenience entry point for the queue worker."""
    client = PaymentClient()
    return client.capture(
        PaymentRequest(order_id=order_id, amount=Decimal(amount), currency=currency)
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    result = capture_order("ord_10432", "149.99")
    print(f"{result.payment_id} {result.status} {result.amount_minor} {result.currency}")
