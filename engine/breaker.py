"""Per-provider circuit breaker.

Motivation, from an observed run: Google AI Studio started returning 503
"model is currently overloaded". All three Gemini seats then independently
tried, backed off, retried, and backed off again -- turning a 40-second run
into three minutes and still losing two seats. Every one of those attempts was
predictable from the first failure.

A breaker makes the failure cheap. After N consecutive failures a provider is
marked OPEN and calls to it fail instantly, so the caller falls back to a
working model without paying the timeout. After a cooldown one probe is allowed
through; if it succeeds the provider is healthy again.

Deliberately in-process, not Redis. This is one process serving one user; a
dict and a timestamp do the whole job, and a network round trip to ask whether
the network is working would be its own joke.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class _State:
    consecutive_failures: int = 0
    opened_at: float | None = None
    probing: bool = False


@dataclass
class CircuitBreaker:
    threshold: int = 3          # consecutive failures before opening
    cooldown: float = 60.0      # seconds to stay open
    _providers: dict[str, _State] = field(default_factory=dict)

    def _state(self, provider: str) -> _State:
        if provider not in self._providers:
            self._providers[provider] = _State()
        return self._providers[provider]

    def is_open(self, provider: str) -> bool:
        """True when calls should fail fast instead of being attempted."""
        st = self._state(provider)
        if st.opened_at is None:
            return False
        if time.monotonic() - st.opened_at >= self.cooldown:
            # Cooldown elapsed: let exactly one probe through (half-open). If it
            # fails, record_failure re-opens with a fresh cooldown.
            if not st.probing:
                st.probing = True
                return False
            return False
        return True

    def record_success(self, provider: str) -> None:
        st = self._state(provider)
        st.consecutive_failures = 0
        st.opened_at = None
        st.probing = False

    def record_failure(self, provider: str) -> None:
        st = self._state(provider)
        st.consecutive_failures += 1
        if st.probing or st.consecutive_failures >= self.threshold:
            st.opened_at = time.monotonic()
            st.probing = False

    def seconds_remaining(self, provider: str) -> float:
        st = self._state(provider)
        if st.opened_at is None:
            return 0.0
        return max(0.0, self.cooldown - (time.monotonic() - st.opened_at))

    def snapshot(self) -> dict:
        return {
            p: {
                "open": self.is_open(p),
                "failures": st.consecutive_failures,
                "cooldown_left": round(self.seconds_remaining(p), 1),
            }
            for p, st in self._providers.items()
        }


class OpenCircuit(Exception):
    """This provider is in cooldown; do not attempt the call."""
