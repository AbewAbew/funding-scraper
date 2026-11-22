import json
from datetime import date, timedelta

import pytest

import main
from ai_analyzer import MalformedAIResponse, parse_response
from llm import LLMClient, TemporaryAIError, _is_daily_quota, _retry_after_seconds

FUTURE = (date.today() + timedelta(days=60)).isoformat()
PAST = (date.today() - timedelta(days=30)).isoformat()


def ai_json(**overrides):
    data = {
        "eligible_locations": ["Africa"], "excluded_locations": [], "ethiopia_eligible": "yes",
        "applicant_types": ["Organizations"], "focus_areas": ["Health", "Women and Girls", "Health"],
        "funder": "Example Foundation", "funding_amount": "Up to $20,000", "deadline": FUTURE,
        "summary": "Funds community health projects run by local NGOs.",
    }
    data.update(overrides)
    return json.dumps(data)


class FakeLLM:
    """Stands in for LLMClient: returns canned responses in order (thread-safe)."""

    def __init__(self, *responses):
        import threading
        self._lock = threading.Lock()
        self.responses = list(responses)
        self.calls_made = 0
        self.providers = []
        self.available = True

    def complete(self, prompt, skip=None):
        with self._lock:
            self.calls_made += 1
            r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r, "fake:model"


def opp(text_extra="", **kw):
    base = {"link": "https://x.org/a", "title": "Health grant", "source": "Test",
            "full_text": "This grant supports health NGOs. " * 20 + text_extra,
            "published_at": "2026-11-01T00:00:00+00:00"}
    base.update(kw)
    return base


# --- parse_response ---------------------------------------------------------
def test_parse_response_cleans_and_validates():
    r = parse_response("```json\n" + ai_json(focus_areas=["health", "Made Up", "Women and Girls", "Education",
                                                          "Research"]) + "\n```")
    assert r["focus_areas"] == ["Health", "Women & Girls", "Education"]  # canonicalised, unknown dropped, max 3
    assert r["applicant_types"] == ["Organizations"]


def test_parse_response_accepts_string_lists():
    r = parse_response(ai_json(eligible_locations="Kenya, Ethiopia"))
    assert r["eligible_locations"] == ["Kenya", "Ethiopia"]


@pytest.mark.parametrize("raw", ["", "no json here", "{not valid", '{"summary": ""}', "[1,2]"])
def test_parse_response_rejects_garbage(raw):
    with pytest.raises(MalformedAIResponse):
        parse_response(raw)


# --- evaluate -----------------------------------------------------------------
def test_relevant_opportunity_is_kept():
    status, record = main.evaluate(opp(), FakeLLM(ai_json()))
    assert status == "processed_relevant"
    assert record["deadline"] == FUTURE
    assert record["focus_areas"] == "Health, Women & Girls"
    assert record["ai_model"] == "fake:model"


def test_expired_post_skips_ai_entirely():
    llm = FakeLLM()
    status, record = main.evaluate(opp(" Deadline: 1 July 2025."), llm)
    assert (status, record, llm.calls_made) == ("processed_expired", None, 0)


def test_expired_deadline_from_ai_is_dropped():
    assert main.evaluate(opp(), FakeLLM(ai_json(deadline=PAST)))[0] == "processed_expired"


def test_irrelevant_geography_is_dropped():
    assert main.evaluate(opp(), FakeLLM(ai_json(eligible_locations=["Canada"])))[0] == "processed_irrelevant"


def test_temporary_ai_failure_propagates_so_item_stays_pending():
    with pytest.raises(TemporaryAIError):
        main.evaluate(opp(), FakeLLM(TemporaryAIError("down")))


def test_malformed_twice_marks_ai_error():
    assert main.evaluate(opp(), FakeLLM("garbage", "still garbage"))[0] == "processed_ai_error"


def test_malformed_then_good_recovers():
    assert main.evaluate(opp(), FakeLLM("garbage", ai_json()))[0] == "processed_relevant"


def test_empty_content_skipped_without_ai():
    llm = FakeLLM()
    assert main.evaluate(opp(full_text="tiny"), llm)[0] == "processed_irrelevant"
    assert llm.calls_made == 0


# --- processor budget / persistence -------------------------------------------
def test_processor_respects_ai_budget_and_flushes(monkeypatch):
    written, statuses = [], []
    monkeypatch.setattr(main.db, "upsert_processed_opportunity", written.append)
    monkeypatch.setattr(main.db, "bulk_update_statuses", lambda u: statuses.extend(u))
    items = [opp(link=f"https://x.org/{i}") for i in range(5)]
    main.run_processor(FakeLLM(*[ai_json()] * 5), max_ai_calls=3, items=items)
    assert len(written) == 3
    assert len(statuses) == 3 and all(s["status"] == "processed_relevant" for s in statuses)


def test_processor_leaves_temporary_failures_pending(monkeypatch):
    statuses = []
    monkeypatch.setattr(main.db, "upsert_processed_opportunity", lambda r: None)
    monkeypatch.setattr(main.db, "bulk_update_statuses", lambda u: statuses.extend(u))
    items = [opp(link="https://x.org/1"), opp(link="https://x.org/2")]
    monkeypatch.setattr(main.config, "MAX_CONCURRENT_AI_CALLS", 1)  # deterministic order for canned responses
    main.run_processor(FakeLLM(TemporaryAIError("x"), ai_json()), max_ai_calls=10, items=items)
    assert statuses == [{"link": "https://x.org/2", "status": "processed_relevant"}]


def test_processor_runs_in_parallel_without_losing_items(monkeypatch):
    statuses = []
    monkeypatch.setattr(main.db, "upsert_processed_opportunity", lambda r: None)
    monkeypatch.setattr(main.db, "bulk_update_statuses", lambda u: statuses.extend(u))
    monkeypatch.setattr(main.config, "MAX_CONCURRENT_AI_CALLS", 4)
    items = [opp(link=f"https://x.org/{i}") for i in range(30)]
    main.run_processor(FakeLLM(*[ai_json()] * 30), max_ai_calls=100, items=items)
    assert sorted(s["link"] for s in statuses) == sorted(i["link"] for i in items)


# --- llm helpers ----------------------------------------------------------------
def test_quota_message_parsing():
    assert _is_daily_quota("Quota exceeded for metric: GenerateRequestsPerDayPerProjectPerModel-FreeTier")
    assert not _is_daily_quota("Quota exceeded for metric: GenerateRequestsPerMinutePerProjectPerModel")
    assert _retry_after_seconds("'retryDelay': '34s'") == 34
    assert _retry_after_seconds("Please try again in 1m30.5s") == 90.5


def test_llm_client_without_keys_is_unavailable(monkeypatch):
    for var in ("GOOGLE_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    client = LLMClient()
    assert not client.available
    with pytest.raises(TemporaryAIError):
        client.complete("hi")


# --- provider failover ------------------------------------------------------------
class _ScriptedProvider:
    """Minimal stand-in for llm._Provider with a scripted sequence of outcomes."""

    def __init__(self, label, outcomes):
        import llm
        self.label, self.outcomes, self.calls = label, list(outcomes), 0
        self.disabled_reason, self.consecutive_failures = None, 0
        self.limiter = llm.RateLimiter(1000, 10**9)

    def complete(self, prompt):
        self.calls += 1
        r = self.outcomes.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _client_with(*providers, monkeypatch):
    for var in ("GOOGLE_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    c = LLMClient()
    c.providers = list(providers)
    return c


def test_overloaded_primary_hands_off_immediately(monkeypatch):
    import llm
    gem = _ScriptedProvider("gemini", [llm._Overloaded("503 high demand")])
    groq = _ScriptedProvider("groq", ["ok"])
    c = _client_with(gem, groq, monkeypatch=monkeypatch)
    assert c.complete("p") == ("ok", "groq")
    assert gem.calls == 1 and gem.disabled_reason is None   # not benched, used again for the next post
    assert c.calls_made == 1                                # failed call didn't eat the budget


def test_daily_quota_benches_provider(monkeypatch):
    import llm
    gem = _ScriptedProvider("gemini", [llm._QuotaExhausted("PerDay")])
    groq = _ScriptedProvider("groq", ["a", "b"])
    c = _client_with(gem, groq, monkeypatch=monkeypatch)
    assert c.complete("p")[1] == "groq"
    assert c.complete("p")[1] == "groq"
    assert gem.calls == 1 and gem.disabled_reason


def test_overloaded_without_fallback_retries_then_gives_up(monkeypatch):
    import llm
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    gem = _ScriptedProvider("gemini", [llm._Overloaded("503")] * 3)
    c = _client_with(gem, monkeypatch=monkeypatch)
    with pytest.raises(TemporaryAIError):
        c.complete("p")
    assert gem.calls == 3
