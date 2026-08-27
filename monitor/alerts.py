"""Alerting.  PLAN.md 6.6 and 10.1-10.6.

An alert is the only part of this system that reaches a human, so it has one hard
requirement above all others: **it must never carry key material or secrets**.
Credentials live behind a path in config (core/config.py) precisely so that no
value in the process is ever the key itself, and a webhook body assembled from a
`rationale` dict or an exception string is the most likely place for that
discipline to leak. Redaction therefore happens in the `Alert` constructor, not at
the sink -- every sink, present and future, gets scrubbed input by construction.

What is alerted on, and why each one is here rather than in a log file:

  FILL           a live fill changes real exposure (I4)
  LIMIT_BREACH   the risk engine denied something; a repeated denial is a bug
  DISCONNECT     a dropped stream means the book state is stale, not empty
  RATE_LIMIT     a 429 means orders are queueing behind a backoff
  STATE_DRIFT    local position != venue position; 6.6 halts the venue and
                 requires human acknowledgement, so it MUST reach a human
  DRAWDOWN       a rung of the section-9 ladder engaged
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

from core.models import now_us

REDACTED: Final[str] = "[redacted]"

#: Env var holding a Telegram / Discord (or any) incoming-webhook URL.  The URL is
#: itself a bearer credential -- it is never echoed, only its presence is.
WEBHOOK_ENV: Final[str] = "PM_ALERT_WEBHOOK"

# Substring match on the KEY name, deliberately over-broad.  Over-redaction costs
# a debugging round trip; under-redaction posts a private key to a chat server.
_SECRET_KEY_PARTS: Final[tuple[str, ...]] = (
    "key", "secret", "token", "password", "passwd", "credential", "cookie",
    "authorization", "auth", "signature", "sign", "private", "session", "bearer",
)

# Value-level scrubbing, for secrets that arrive inside free text (an exception
# message, a rationale note) where no key name gives them away.
_PEM_RE: Final[re.Pattern[str]] = re.compile(
    r"-----BEGIN[^-]*-----.*?-----END[^-]*-----", re.DOTALL
)
_BEARER_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(Bearer|Basic|Token)\s+[A-Za-z0-9._\-+/=]{8,}", re.IGNORECASE
)
_ASSIGNMENT_RE: Final[re.Pattern[str]] = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*(?:key|secret|token|password|signature)"
    r"[A-Za-z0-9_]*)\s*[:=]\s*(\S+)",
    re.IGNORECASE,
)


class Severity(StrEnum):
    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"


class AlertKind(StrEnum):
    FILL = "fill"
    LIMIT_BREACH = "limit_breach"
    DISCONNECT = "disconnect"
    RATE_LIMIT = "rate_limit"
    STATE_DRIFT = "state_drift"
    DRAWDOWN = "drawdown"


def scrub_text(text: str) -> str:
    """Remove secret-shaped material from free text."""
    out = _PEM_RE.sub(REDACTED, text)
    out = _BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", out)
    return _ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", out)


def _is_secret_key(key: str) -> bool:
    low = key.lower()
    return any(part in low for part in _SECRET_KEY_PARTS)


def redact(value: Any, *, key: str | None = None) -> Any:
    """Recursively scrub a payload.

    Redaction is by KEY NAME first (a field called `api_key` is a secret whatever
    it holds) and by value shape second (a PEM block is a secret whatever it is
    called).  Containers are walked so that a secret nested three dicts deep in a
    rationale cannot ride out.
    """
    if key is not None and _is_secret_key(key):
        return REDACTED
    if isinstance(value, dict):
        return {str(k): redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, int | float | bool) or value is None:
        return value
    return scrub_text(str(value))


@dataclass(frozen=True, slots=True)
class Alert:
    """One thing a human may need to know.  Redacted at construction."""

    kind: AlertKind
    severity: Severity
    message: str
    context: dict[str, Any] = field(default_factory=dict)
    at_us: int = field(default_factory=now_us)

    def __post_init__(self) -> None:
        # Frozen for callers, scrubbed once here: no code path can hand a sink an
        # unredacted alert, including code written later against this class.
        object.__setattr__(self, "message", scrub_text(self.message))
        object.__setattr__(self, "context", redact(dict(self.context)))

    def render(self) -> str:
        head = f"[{self.severity.value.upper()}] {self.kind.value}: {self.message}"
        if not self.context:
            return head
        body = " ".join(f"{k}={v}" for k, v in sorted(self.context.items()))
        return f"{head} | {body}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "severity": self.severity.value,
            "message": self.message,
            "context": self.context,
            "at_us": self.at_us,
        }


@runtime_checkable
class Alerter(Protocol):
    """Anything that can deliver an alert.  Sinks must never raise."""

    def send(self, alert: Alert) -> bool: ...


@dataclass
class ConsoleAlerter:
    """stdout for INFO/WARN, stderr for CRITICAL, so a pipe still shows the bad news."""

    stream: Any = None
    error_stream: Any = None

    def send(self, alert: Alert) -> bool:
        target = (self.error_stream or sys.stderr) if alert.severity is Severity.CRITICAL \
            else (self.stream or sys.stdout)
        print(alert.render(), file=target, flush=True)
        return True


@dataclass
class WebhookAlerter:
    """POSTs to a Telegram/Discord-style incoming webhook read from the environment.

    A no-op when the variable is unset, which is the normal state in backtest,
    shadow and CI.  It must be a no-op rather than an error: monitoring that
    crashes the process it monitors is worse than no monitoring, and tests must
    never make a network call.
    """

    env_var: str = WEBHOOK_ENV
    timeout_s: float = 5.0
    field_name: str = "content"          # Discord uses `content`, Telegram `text`
    sent: int = 0
    failures: int = 0

    @property
    def url(self) -> str | None:
        return os.environ.get(self.env_var) or None

    @property
    def enabled(self) -> bool:
        return self.url is not None

    def describe(self) -> str:
        """Status without ever echoing the URL -- it is a bearer credential."""
        return f"webhook {'configured' if self.enabled else 'unset'} ({self.env_var})"

    def send(self, alert: Alert) -> bool:
        url = self.url
        if url is None:
            return False
        import httpx    # imported here so an unconfigured alerter costs nothing

        payload = {self.field_name: alert.render(), "alert": alert.to_dict()}
        try:
            resp = httpx.post(url, json=payload, timeout=self.timeout_s)
            ok = resp.status_code < 400
        except Exception:
            # Never let the alert transport take down the trader.  The failure
            # count is what the digest reports; the exception may carry the URL.
            self.failures += 1
            return False
        if ok:
            self.sent += 1
        else:
            self.failures += 1
        return ok


@dataclass
class MemoryAlerter:
    """Collects alerts in memory.  For tests and for the digest's own summary."""

    alerts: list[Alert] = field(default_factory=list)

    def send(self, alert: Alert) -> bool:
        self.alerts.append(alert)
        return True


@dataclass
class Alerts:
    """Fan-out facade with one named method per alertable event (PLAN.md 6.6)."""

    sinks: list[Alerter] = field(default_factory=list)

    def emit(self, alert: Alert) -> int:
        """Deliver to every sink.  Returns how many accepted it."""
        return sum(1 for s in self.sinks if s.send(alert))

    # ------------------------------------------------------------------ events
    def fill(self, *, sleeve_id: str, ticker: str, side: str, price_cents: int,
             size: int, is_maker: bool) -> int:
        return self.emit(Alert(
            AlertKind.FILL, Severity.INFO,
            f"{sleeve_id} filled {size} {ticker} {side} @ {price_cents}c",
            {"sleeve_id": sleeve_id, "ticker": ticker, "side": side,
             "price_cents": price_cents, "size": size, "is_maker": is_maker},
        ))

    def limit_breach(self, *, sleeve_id: str, reason: str, detail: str = "") -> int:
        return self.emit(Alert(
            AlertKind.LIMIT_BREACH, Severity.WARN,
            f"{sleeve_id} denied by {reason}",
            {"sleeve_id": sleeve_id, "reason": reason, "detail": detail},
        ))

    def disconnect(self, *, venue: str, detail: str = "") -> int:
        # A dropped stream leaves a STALE book, not an empty one -- quoting off it
        # is how a maker gets picked off during an outage.
        return self.emit(Alert(
            AlertKind.DISCONNECT, Severity.CRITICAL,
            f"{venue} stream disconnected",
            {"venue": venue, "detail": detail},
        ))

    def rate_limited(self, *, venue: str, endpoint: str = "",
                     retry_after_s: float | None = None) -> int:
        return self.emit(Alert(
            AlertKind.RATE_LIMIT, Severity.WARN,
            f"{venue} returned HTTP 429",
            {"venue": venue, "endpoint": endpoint, "status": 429,
             "retry_after_s": retry_after_s},
        ))

    def state_drift(self, *, venue: str, ticker: str, local: int, remote: int) -> int:
        # 6.6: drift halts the venue and requires human acknowledgement, so this is
        # CRITICAL by definition -- the position of record disagrees with reality.
        return self.emit(Alert(
            AlertKind.STATE_DRIFT, Severity.CRITICAL,
            f"{venue} {ticker}: local {local} != venue {remote}",
            {"venue": venue, "ticker": ticker, "local": local, "remote": remote,
             "delta": remote - local},
        ))

    def drawdown_rung(self, *, drawdown: float, action: str) -> int:
        # Section 9's ladder: 0.40 is full stop.  R9a -- reaching it is itself
        # evidence the edges are not real, so it outranks a warning.
        severity = Severity.CRITICAL if drawdown >= 0.30 else Severity.WARN
        return self.emit(Alert(
            AlertKind.DRAWDOWN, severity,
            f"drawdown {drawdown:.1%} -> {action}",
            {"drawdown": drawdown, "action": action},
        ))


def default_alerts(*, console: bool = True, webhook: bool = True) -> Alerts:
    """Console always; webhook only when the environment actually configures one."""
    sinks: list[Alerter] = []
    if console:
        sinks.append(ConsoleAlerter())
    if webhook:
        hook = WebhookAlerter()
        if hook.enabled:
            sinks.append(hook)
    return Alerts(sinks)


def alert_json(alert: Alert) -> str:
    """Serialized form for a log line.  Already redacted by construction."""
    return json.dumps(alert.to_dict(), sort_keys=True, default=str)
