"""LLM gateway: the only place that talks to a model provider.

Responsibilities: key pool with cooldown, retries with backoff, JSON-mode fallback,
budget enforcement, and a ledger row in `llm_calls` for every attempt (never the key).
"""
from __future__ import annotations

import json
import random
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import LLMCall, Project

MILLION = Decimal(1_000_000)
TRUNCATED = "answer cut off at the output token limit (raise LLM_MAX_OUTPUT_TOKENS or lower TRANSLATE_CHUNK_WORDS)"


class LLMError(RuntimeError):
    """Call failed for good (non-retryable, or retries exhausted)."""


class BudgetExceeded(LLMError):
    """Spending cap reached; nothing was sent."""


class NoKeysAvailable(LLMError):
    """Every API key was rejected by the provider."""


@dataclass
class LLMResult:
    text: str
    data: Any | None  # parsed JSON when json=True
    model: str
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    cost_usd: Decimal
    call_id: int


@dataclass
class _Key:
    label: str
    secret: str
    sem: threading.Semaphore
    cooldown_until: float = 0.0
    disabled: bool = False
    uses: int = 0


@dataclass
class KeyPool:
    """Round-robin over healthy keys; a rate-limited key rests, a rejected key is dropped."""

    secrets: list[str]
    concurrency: int = 4
    clock: Callable[[], float] = time.monotonic
    _keys: list[_Key] = field(init=False)
    _lock: threading.Lock = field(init=False, default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self._keys = [
            _Key(label=f"key{i + 1}", secret=s, sem=threading.Semaphore(self.concurrency))
            for i, s in enumerate(self.secrets)
        ]

    def pick(self) -> tuple[_Key, float]:
        """Return the best key and how long to wait before using it."""
        with self._lock:
            alive = [k for k in self._keys if not k.disabled]
            if not alive:
                raise NoKeysAvailable("no usable API keys (all missing or rejected)")
            now = self.clock()
            key = min(alive, key=lambda k: (max(0.0, k.cooldown_until - now), k.uses))
            key.uses += 1
            return key, max(0.0, key.cooldown_until - now)

    def cool_down(self, key: _Key, seconds: float) -> None:
        with self._lock:
            key.cooldown_until = max(key.cooldown_until, self.clock() + seconds)

    def disable(self, key: _Key) -> None:
        with self._lock:
            key.disabled = True


def parse_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])


class LLMGateway:
    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.sleep = sleep
        self.clock = clock
        self.pool = KeyPool(settings.llm_key_list, settings.llm_concurrency_per_key, clock)
        self.prices: dict[str, dict[str, float]] = json.loads(settings.llm_prices)
        self.provider = httpx.URL(settings.llm_base_url).host
        self.client = httpx.Client(
            base_url=settings.llm_base_url.rstrip("/") + "/",
            timeout=settings.llm_timeout_seconds,
            transport=transport,
        )

    # ------------------------------------------------------------------ public

    def complete(
        self,
        stage: str,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        json_mode: bool = True,
        project_id: uuid.UUID | None = None,
        job_id: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> LLMResult:
        model = model or self.settings.llm_model
        price = self._price(model)
        use_response_format = json_mode
        last_error = "no attempt made"

        for attempt in range(self.settings.llm_max_attempts):
            self._check_budget(project_id)
            key, wait = self.pool.pick()
            if wait:
                self.sleep(wait)

            body: dict[str, Any] = {"model": model, "messages": messages, **(extra or {})}
            if use_response_format:
                body["response_format"] = {"type": "json_object"}
            if self.settings.llm_max_output_tokens and "max_completion_tokens" not in body:
                body["max_completion_tokens"] = self.settings.llm_max_output_tokens
            log = {"project_id": project_id, "job_id": job_id, "stage": stage, "model": model,
                   "key_label": key.label}
            started = self.clock()
            try:
                with key.sem:
                    resp = self.client.post(
                        "chat/completions", json=body, headers={"Authorization": f"Bearer {key.secret}"}
                    )
            except httpx.HTTPError as exc:
                last_error = f"transport: {type(exc).__name__}"
                self._log(**log, ok=False, error=last_error, started=started)
                self.sleep(self._backoff(attempt))
                continue

            status = resp.status_code
            if status == 200:
                result, problem = self._read_success(resp, model, price, json_mode, log, started)
                if result is not None:
                    return result
                last_error = problem
                if problem == TRUNCATED:      # the same request would be cut off, and billed, again
                    raise LLMError(f"{stage}: {problem}")
                continue
            last_error = f"http {status}: {self._error_message(resp)}"
            self._log(**log, ok=False, error=last_error, started=started)
            if status in (401, 403):
                self.pool.disable(key)
            elif status == 400 and use_response_format:
                use_response_format = False  # endpoint without JSON mode; rely on the prompt
            elif status == 429 or status >= 500:
                pause = self._retry_after(resp) or self._backoff(attempt)
                self.pool.cool_down(key, pause)
            else:
                raise LLMError(f"{stage}: {last_error}")

        raise LLMError(f"{stage}: gave up after {self.settings.llm_max_attempts} attempts ({last_error})")

    def spent(self, project_id: uuid.UUID | None = None) -> Decimal:
        with Session(self.engine) as s:
            q = select(func.coalesce(func.sum(LLMCall.cost_usd), 0))
            if project_id is not None:
                q = q.where(LLMCall.project_id == project_id)
            return Decimal(s.execute(q).scalar_one())

    # ------------------------------------------------------------------ internals

    def _read_success(self, resp, model, price, json_mode, log, started):
        try:
            payload = resp.json()
            text = payload["choices"][0]["message"]["content"] or ""
            truncated = payload["choices"][0].get("finish_reason") == "length"
        except (ValueError, KeyError, IndexError, TypeError):
            problem = "malformed provider response"
            self._log(**log, ok=False, error=problem, started=started)
            return None, problem
        usage = payload.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens") or 0)
        cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        tokens_out = int(usage.get("completion_tokens") or 0)
        cost = (
            Decimal(tokens_in - cached) * Decimal(str(price["input"]))
            + Decimal(cached) * Decimal(str(price.get("cached_input", price["input"])))
            + Decimal(tokens_out) * Decimal(str(price["output"]))
        ) / MILLION
        data, problem = None, None
        if truncated:
            problem = TRUNCATED
        elif json_mode:
            try:
                data = parse_json(text)
            except json.JSONDecodeError:
                problem = "model returned invalid JSON"
        # The request was billed either way, so the cost is always recorded.
        call_id = self._log(**log, ok=problem is None, error=problem, started=started,
                            input_tokens=tokens_in, cached_tokens=cached, output_tokens=tokens_out,
                            cost_usd=cost)
        if problem:
            return None, problem
        return LLMResult(text, data, model, tokens_in, cached, tokens_out, cost, call_id), None

    def _price(self, model: str) -> dict[str, float]:
        price = self.prices.get(model) or self.prices.get("default")
        if not price:
            raise LLMError(f"no price configured for model {model!r}; add it to LLM_PRICES")
        return price

    def _check_budget(self, project_id: uuid.UUID | None) -> None:
        cap = self.settings.llm_budget_usd
        if cap is not None and self.spent() >= Decimal(str(cap)):
            raise BudgetExceeded(f"global budget ${cap} reached")
        if project_id is None:
            return
        with Session(self.engine) as s:
            query = select(Project.budget_usd).where(Project.id == project_id)
            budget = s.execute(query).scalar_one_or_none()
        if budget is not None and self.spent(project_id) >= budget:
            raise BudgetExceeded(f"project budget ${budget} reached")

    def _log(self, *, started: float, ok: bool, error: str | None = None, **fields: Any) -> int:
        row = LLMCall(
            provider=self.provider, ok=ok, error=error,
            latency_ms=int((self.clock() - started) * 1000), **fields,
        )
        with Session(self.engine) as s:
            s.add(row)
            s.commit()
            return row.id

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(60.0, 2.0 ** attempt) * (0.5 + random.random() / 2)

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float | None:
        try:
            return min(300.0, float(resp.headers["retry-after"]))
        except (KeyError, ValueError):
            return None

    @staticmethod
    def _error_message(resp: httpx.Response) -> str:
        try:
            err = resp.json().get("error")
            msg = err.get("message") if isinstance(err, dict) else err
        except (ValueError, AttributeError):
            msg = resp.text
        return str(msg or "")[:300]
