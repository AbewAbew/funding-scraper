"""
Provider-agnostic LLM client with client-side rate limiting and automatic fallback.

Free tiers change often, so nothing here is tied to one vendor:
  * Providers are tried in the order given in config.AI_PROVIDERS (skipping ones with no API key).
  * Per-minute limits are respected by pacing requests (RPM + estimated TPM).
  * A provider that hits its *daily* quota, or keeps failing, is benched for the rest of the run.
"""
import logging
import os
import re
import threading
import time
from collections import deque

import requests

import config

logger = logging.getLogger(__name__)


class TemporaryAIError(Exception):
    """Every provider failed for transient reasons; the item should be retried on a later run."""


class _QuotaExhausted(Exception):
    pass


class _Overloaded(Exception):
    """Provider-side 5xx ("high demand", "unavailable"): better to try another provider than to wait."""


class _RateLimited(Exception):
    def __init__(self, wait: float):
        super().__init__(f"rate limited, wait {wait:.0f}s")
        self.wait = wait


class RateLimiter:
    """Sliding 60-second window over request count and estimated tokens."""

    def __init__(self, rpm: int, tpm: int):
        self.rpm, self.tpm = max(rpm, 1), max(tpm, 1)
        self.events: deque[tuple[float, int]] = deque()
        self.lock = threading.Lock()

    def acquire(self, tokens: int):
        tokens = min(tokens, self.tpm)
        while True:
            with self.lock:
                now = time.monotonic()
                while self.events and now - self.events[0][0] >= 60:
                    self.events.popleft()
                used = sum(t for _, t in self.events)
                if len(self.events) < self.rpm and used + tokens <= self.tpm:
                    self.events.append((now, tokens))
                    return
                wait = 60 - (now - self.events[0][0]) + 0.1
            time.sleep(max(wait, 0.5))


def estimate_tokens(text: str) -> int:
    return len(text) // 4 + 600  # prompt chars/4 plus room for the answer


def _retry_after_seconds(message: str, default: float = 30) -> float:
    for pattern in (r"retry_?delay\W+(?:seconds:\s*)?(\d+(?:\.\d+)?)", r"try again in (?:(\d+)m)?(\d+(?:\.\d+)?)s",
                    r"retry after (\d+)"):
        m = re.search(pattern, message, re.IGNORECASE)
        if m:
            nums = [float(g) for g in m.groups() if g]
            return (nums[0] * 60 + nums[1]) if len(nums) == 2 else nums[0]
    return default


def _is_daily_quota(message: str) -> bool:
    return bool(re.search(r"per ?day|daily|RPD|TPD|quota exceeded for metric.*day", message, re.IGNORECASE))


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
class _Provider:
    def __init__(self, spec: dict, api_key: str):
        self.name, self.model, self.api_key = spec["name"], spec["model"], api_key
        self.spec = spec
        self.limiter = RateLimiter(spec.get("rpm", 10), spec.get("tpm", 10000))
        self.disabled_reason: str | None = None
        self.consecutive_failures = 0

    @property
    def label(self) -> str:
        return f"{self.name}:{self.model}"

    def complete(self, prompt: str) -> str:  # pragma: no cover - network
        raise NotImplementedError


class _GeminiProvider(_Provider):
    def __init__(self, spec, api_key):
        super().__init__(spec, api_key)
        from google import genai  # imported lazily so tests don't need the SDK configured

        self._client = genai.Client(api_key=api_key)

    def complete(self, prompt: str) -> str:
        from google.genai import errors, types

        try:
            resp = self._client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.1, max_output_tokens=1200),
            )
        except errors.APIError as e:
            msg = f"{e.code} {e.message} {getattr(e, 'details', '')}"
            if e.code == 429:
                if _is_daily_quota(msg):
                    raise _QuotaExhausted(msg) from e
                raise _RateLimited(_retry_after_seconds(msg)) from e
            if e.code in (400, 401, 403, 404):
                raise _QuotaExhausted(f"unusable ({msg})") from e  # bad key/model: bench provider
            if e.code and e.code >= 500:
                raise _Overloaded(msg[:160]) from e
            raise
        return resp.text or ""


class _OpenAICompatibleProvider(_Provider):
    def complete(self, prompt: str) -> str:
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 1500,
            "response_format": {"type": "json_object"},
            **self.spec.get("extra_body", {}),
        }
        r = requests.post(
            f"{self.spec['base_url']}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=120,
        )
        if r.status_code == 429:
            msg = r.text
            if _is_daily_quota(msg):
                raise _QuotaExhausted(msg[:300])
            raise _RateLimited(float(r.headers.get("retry-after") or _retry_after_seconds(msg)))
        if r.status_code in (401, 403, 404):
            raise _QuotaExhausted(f"unusable ({r.status_code} {r.text[:200]})")
        if r.status_code >= 500:
            raise _Overloaded(f"{r.status_code} {r.text[:160]}")
        r.raise_for_status()
        return r.json()["choices"][0]["message"].get("content") or ""


_KINDS = {"gemini": _GeminiProvider, "openai_compatible": _OpenAICompatibleProvider}


# ---------------------------------------------------------------------------
# Public client
# ---------------------------------------------------------------------------
class LLMClient:
    MAX_ATTEMPTS_PER_PROVIDER = 3
    MAX_CONSECUTIVE_FAILURES = 5

    def __init__(self, provider_specs: list[dict] | None = None):
        self.providers: list[_Provider] = []
        for spec in provider_specs or config.AI_PROVIDERS:
            key = os.getenv(spec["api_key_env"])
            if not key:
                continue
            try:
                self.providers.append(_KINDS[spec["kind"]](spec, key))
            except Exception:
                logger.exception("Could not initialise AI provider %s", spec["name"])
        self.calls_made = 0
        self._count_lock = threading.Lock()
        if self.providers:
            logger.info("AI providers (in order): %s", ", ".join(p.label for p in self.providers))
        else:
            logger.critical("No AI provider configured. Set GOOGLE_API_KEY and/or GROQ_API_KEY.")

    @property
    def available(self) -> bool:
        return any(p.disabled_reason is None for p in self.providers)

    def complete(self, prompt: str, skip: set[str] | None = None) -> tuple[str, str]:
        """Return (response_text, provider_label). Raises TemporaryAIError if nobody could answer."""
        tokens = estimate_tokens(prompt)
        usable = [p for p in self.providers if not p.disabled_reason and not (skip and p.label in skip)]
        for idx, p in enumerate(usable):
            has_fallback = any(not q.disabled_reason for q in usable[idx + 1:])
            for attempt in range(1, self.MAX_ATTEMPTS_PER_PROVIDER + 1):
                if p.disabled_reason:
                    break
                p.limiter.acquire(tokens)
                try:
                    text = p.complete(prompt)
                    with self._count_lock:
                        self.calls_made += 1  # only successful calls count towards the run budget
                    p.consecutive_failures = 0
                    return text, p.label
                except _QuotaExhausted as e:
                    p.disabled_reason = str(e)[:200]
                    logger.warning("AI provider %s benched for this run: %s", p.label, p.disabled_reason)
                    break
                except _Overloaded as e:
                    if has_fallback:
                        logger.info("%s overloaded (%s); handing this post to the next provider", p.label, e)
                        break
                    wait = 10 * 2 ** (attempt - 1)
                    logger.warning("%s overloaded; retrying in %ds (attempt %d)", p.label, wait, attempt)
                    time.sleep(wait)
                except _RateLimited as e:
                    wait = min(e.wait + 1, 120)
                    logger.warning("%s rate-limited; waiting %.0fs (attempt %d)", p.label, wait, attempt)
                    time.sleep(wait)
                except Exception as e:
                    wait = 5 * 2 ** (attempt - 1)
                    logger.warning("%s call failed (%s); retrying in %ds (attempt %d)", p.label, e, wait, attempt)
                    time.sleep(wait)
            else:
                p.consecutive_failures += 1
                if p.consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                    p.disabled_reason = "too many consecutive failures"
                    logger.warning("AI provider %s benched: %s", p.label, p.disabled_reason)
        raise TemporaryAIError("All AI providers failed or are out of quota")
