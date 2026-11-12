import json
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.llm.gateway import BudgetExceeded, LLMError, LLMGateway, NoKeysAvailable
from app.models import LLMCall, Project

MSG = [{"role": "user", "content": "hi"}]


def ok(content='{"a": 1}', tokens_in=1000, cached=0, tokens_out=500):
    return httpx.Response(200, json={
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out,
                  "prompt_tokens_details": {"cached_tokens": cached}},
    })


def make(engine, responses, keys="sk-secret-A,sk-secret-B", **overrides):
    """Gateway whose provider replies with `responses` in order; records requests and sleeps."""
    seen, sleeps = [], []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    settings = Settings(llm_api_keys=keys, llm_base_url="https://llm.test/v1", **overrides)
    gw = LLMGateway(engine, settings, transport=httpx.MockTransport(handler), sleep=sleeps.append)
    return gw, seen, sleeps


@pytest.fixture()
def project(migrated_db):
    with Session(migrated_db) as s:
        p = Project(name="gw", budget_usd=Decimal("1.00"))
        s.add(p)
        s.commit()
        return p.id


def calls(engine, project_id):
    with Session(engine) as s:
        return list(s.scalars(select(LLMCall).where(LLMCall.project_id == project_id).order_by(LLMCall.id)))


def test_success_records_tokens_and_cost(migrated_db, project):
    gw, seen, _ = make(migrated_db, [ok(tokens_in=1000, cached=400, tokens_out=500)])
    r = gw.complete("translate", MSG, project_id=project)
    assert r.data == {"a": 1}
    # 600 fresh * $10 + 400 cached * $1 + 500 out * $50, per million
    assert r.cost_usd == Decimal("0.0314")
    (row,) = calls(migrated_db, project)
    assert (row.ok, row.stage, row.provider, row.key_label) == (True, "translate", "llm.test", "key1")
    assert row.cost_usd == Decimal("0.031400")
    assert json.loads(seen[0].content)["response_format"] == {"type": "json_object"}


def test_rate_limit_retries_on_another_key(migrated_db, project):
    gw, seen, sleeps = make(migrated_db, [httpx.Response(429, headers={"retry-after": "7"}), ok()])
    gw.complete("translate", MSG, project_id=project)
    assert [r.headers["authorization"] for r in seen] == ["Bearer sk-secret-A", "Bearer sk-secret-B"]
    assert sleeps == []  # the second key was free, so no waiting
    rows = calls(migrated_db, project)
    assert [r.ok for r in rows] == [False, True] and "429" in rows[0].error


def test_single_key_waits_for_retry_after(migrated_db, project):
    limited = httpx.Response(429, headers={"retry-after": "7"})
    gw, _, sleeps = make(migrated_db, [limited, ok()], keys="sk-only")
    gw.complete("translate", MSG, project_id=project)
    assert len(sleeps) == 1 and 6.5 < sleeps[0] <= 7


def test_server_error_and_timeout_are_retried(migrated_db, project):
    gw, _, _ = make(migrated_db, [httpx.Response(503), httpx.ReadTimeout("slow"), ok()])
    assert gw.complete("review", MSG, project_id=project).data == {"a": 1}
    assert [r.ok for r in calls(migrated_db, project)] == [False, False, True]


def test_rejected_keys_are_dropped(migrated_db, project):
    gw, seen, _ = make(migrated_db, [httpx.Response(401), ok(), ok()])
    gw.complete("translate", MSG, project_id=project)
    gw.complete("translate", MSG, project_id=project)
    assert [r.headers["authorization"][-1] for r in seen] == ["A", "B", "B"]

    gw, _, _ = make(migrated_db, [httpx.Response(401), httpx.Response(403)])
    with pytest.raises(NoKeysAvailable):
        gw.complete("translate", MSG, project_id=project)


def test_json_mode_fallback_for_basic_proxies(migrated_db, project):
    gw, seen, _ = make(migrated_db, [httpx.Response(400, json={"error": {"message": "unknown param"}}),
                                     ok('```json\n{"a": 2}\n```')])
    assert gw.complete("translate", MSG, project_id=project).data == {"a": 2}
    assert "response_format" not in json.loads(seen[1].content)


def test_invalid_json_is_billed_then_retried(migrated_db, project):
    gw, _, _ = make(migrated_db, [ok("not json at all"), ok()])
    gw.complete("translate", MSG, project_id=project)
    rows = calls(migrated_db, project)
    assert [r.ok for r in rows] == [False, True]
    assert rows[0].cost_usd > 0


def test_other_client_errors_fail_fast(migrated_db, project):
    gw, seen, _ = make(migrated_db, [httpx.Response(404, json={"error": {"message": "no such model"}})],
                       llm_prices='{"default": {"input": 1, "output": 1}}')
    with pytest.raises(LLMError, match="no such model"):
        gw.complete("translate", MSG, project_id=project, model="nope")
    assert len(seen) == 1


def test_provider_cannot_echo_a_secret_into_errors_or_the_cost_ledger(migrated_db, project):
    gw, _, _ = make(migrated_db, [httpx.Response(404, json={
        "error": {"message": "rejected credential sk-secret-A and sk-another-secret"}})])
    with pytest.raises(LLMError) as error:
        gw.complete("translate", MSG, project_id=project)
    assert "sk-secret" not in str(error.value)
    (row,) = calls(migrated_db, project)
    assert "sk-" not in row.error and "[redacted]" in row.error


def test_gives_up_after_max_attempts(migrated_db, project):
    gw, seen, _ = make(migrated_db, [httpx.Response(500)] * 3, llm_max_attempts=3)
    with pytest.raises(LLMError, match="gave up"):
        gw.complete("translate", MSG, project_id=project)
    assert len(seen) == 3


def test_project_budget_stops_before_sending(migrated_db, project):
    # 20k output tokens = $1.00, exactly the project budget
    gw, seen, _ = make(migrated_db, [ok(tokens_in=0, tokens_out=20_000), ok()])
    gw.complete("translate", MSG, project_id=project)
    with pytest.raises(BudgetExceeded):
        gw.complete("translate", MSG, project_id=project)
    assert len(seen) == 1
    assert gw.spent(project) == Decimal("1.000000")


def test_global_budget(migrated_db, project):
    gw, seen, _ = make(migrated_db, [ok()], llm_budget_usd=0.0)
    with pytest.raises(BudgetExceeded):
        gw.complete("translate", MSG)
    assert seen == []


def test_unpriced_model_is_refused(migrated_db):
    gw, seen, _ = make(migrated_db, [ok()])
    with pytest.raises(LLMError, match="no price configured"):
        gw.complete("translate", MSG, model="mystery-model")
    assert seen == []


def test_keys_never_reach_the_ledger(migrated_db, project):
    gw, _, _ = make(migrated_db, [httpx.Response(429), httpx.Response(500), ok()])
    gw.complete("translate", MSG, project_id=project)
    dump = " ".join(f"{r.key_label} {r.error} {r.provider}" for r in calls(migrated_db, project))
    assert "sk-secret" not in dump


def test_output_cap_is_sent_and_a_truncated_answer_is_not_retried(migrated_db, project):
    cut = httpx.Response(200, json={
        "choices": [{"message": {"content": '{"a": '}, "finish_reason": "length"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50}})
    gw, seen, _ = make(migrated_db, [cut, ok()], llm_max_output_tokens=50)
    with pytest.raises(LLMError, match="cut off"):
        gw.complete("translate", MSG, project_id=project)
    assert len(seen) == 1                                       # not sent again: it would be billed again
    assert json.loads(seen[0].content)["max_completion_tokens"] == 50
    rows = calls(migrated_db, project)
    assert len(rows) == 1 and not rows[0].ok and rows[0].cost_usd > 0


def test_no_output_cap_by_default(migrated_db, project):
    gw, seen, _ = make(migrated_db, [ok()])
    gw.complete("translate", MSG, project_id=project)
    assert "max_completion_tokens" not in json.loads(seen[0].content)
