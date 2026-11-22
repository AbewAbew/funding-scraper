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
        # Overload ("high demand") is temporary: pause the provider instead of benching it.
        self.consecutive_overloads = 0
        self.cooldown_until = 0.0
        self._state_lock = threading.Lock()

    def note_overload(self, max_cooldown: float = 300) -> float:
        with self._state_lock:
            now = time.monotonic()
            if now < self.cooldown_until:
                # Parallel requests failing in the same spike: don't escalate the pause again.
                return self.cooldown_until - now
            self.consecutive_overloads += 1
            pause = min(15 * 2 ** (self.consecutive_overloads - 1), max_cooldown)
            self.cooldown_until = now + pause
            return pause

    def note_success(self):
        with self._state_lock:
            self.consecutive_failures = 0
            self.consecutive_overloads = 0

    def wait_for_cooldown(self):
        remaining = self.cooldown_until - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)

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
                # Gemma 4 always "thinks" first (often ~1k tokens, and it can't be disabled), so leave
                # plenty of room; JSON mode stops it wrapping the answer in prose or markdown.
                config=types.GenerateContentConfig(
                    temperature=0.1, max_output_tokens=4096, response_mime_type="application/json"
                ),
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
    OVERLOAD_RETRIES_BEFORE_HANDOFF = 1

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
            overloads = generic_failures = 0
            for attempt in range(1, self.MAX_ATTEMPTS_PER_PROVIDER + 1):
                if p.disabled_reason:
                    break
                p.wait_for_cooldown()
                p.limiter.acquire(tokens)
                try:
                    text = p.complete(prompt)
                    with self._count_lock:
                        self.calls_made += 1  # only successful calls count towards the run budget
                    p.note_success()
                    return text, p.label
                except _QuotaExhausted as e:
                    p.disabled_reason = str(e)[:200]
                    logger.warning("AI provider %s benched for this run: %s", p.label, p.disabled_reason)
                    break
                except _Overloaded as e:
                    overloads += 1
                    pause = p.note_overload()
                    # Retry the primary once before spending a fallback's (smaller) daily quota.
                    if has_fallback and overloads > self.OVERLOAD_RETRIES_BEFORE_HANDOFF:
                        logger.info("%s still overloaded; handing this post to the next provider", p.label)
                        break
                    logger.warning("%s overloaded (%s); pausing it %.0fs (attempt %d)", p.label, str(e)[:60],
                                   pause, attempt)
                except _RateLimited as e:
                    wait = min(e.wait + 1, 120)
                    logger.warning("%s rate-limited; waiting %.0fs (attempt %d)", p.label, wait, attempt)
                    time.sleep(wait)
                except Exception as e:
                    generic_failures += 1
                    wait = 5 * 2 ** (attempt - 1)
                    logger.warning("%s call failed (%s); retrying in %ds (attempt %d)", p.label, e, wait, attempt)
                    time.sleep(wait)
            # Bench only for repeated *real* errors, never for temporary overload.
            if generic_failures >= self.MAX_ATTEMPTS_PER_PROVIDER:
                with p._state_lock:
                    p.consecutive_failures += 1
                    bench = p.consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES
                if bench:
                    p.disabled_reason = "too many consecutive failures"
                    logger.warning("AI provider %s benched: %s", p.label, p.disabled_reason)
        raise TemporaryAIError("All AI providers failed or are out of quota")
