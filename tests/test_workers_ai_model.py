# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""Tests for Workers AI text model update and enrichment JSON parsing compatibility."""

import asyncio
import json
import logging

import pytest

from keepfor.consumer.processor import SUMMARY_MODEL, generate_triage_summary
from keepfor.defaults import (
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_TEXT_MODEL,
    WorkersAIError,
    WorkersAINonRetryableError,
    WorkersAIProvider,
    WorkersAIRetryableError,
    _extract_text_response,
    clean_json_text,
)
from tests.fakes import FakeAI, FakeEnv


def test_text_model_constants():
    """Verify that text model constants point to supported Gemma 4 model."""
    assert DEFAULT_TEXT_MODEL == "@cf/google/gemma-4-26b-a4b-it"
    assert SUMMARY_MODEL == DEFAULT_TEXT_MODEL
    # Embedding model must remain untouched
    assert DEFAULT_EMBEDDING_MODEL == "@cf/baai/bge-base-en-v1.5"


def test_workers_ai_error_types():
    """Verify WorkersAIError hierarchy and retryable flags."""
    retryable_err = WorkersAIRetryableError("transient", model="m1")
    assert retryable_err.retryable is True
    assert retryable_err.model == "m1"
    assert isinstance(retryable_err, WorkersAIError)
    assert isinstance(retryable_err, RuntimeError)

    non_retryable_err = WorkersAINonRetryableError("permanent", model="m2")
    assert non_retryable_err.retryable is False
    assert non_retryable_err.model == "m2"
    assert isinstance(non_retryable_err, WorkersAIError)


def test_workers_ai_provider_default_model():
    """WorkersAIProvider defaults to DEFAULT_TEXT_MODEL."""
    provider = WorkersAIProvider()
    assert provider.text_model == "@cf/google/gemma-4-26b-a4b-it"
    assert provider.embedding_model == "@cf/baai/bge-base-en-v1.5"

    custom = WorkersAIProvider(text_model="custom-model")
    assert custom.text_model == "custom-model"


@pytest.mark.asyncio
async def test_summarize_formats():
    """Verify summarize handles both legacy response and OpenAI choices formats."""
    called_models = []

    # 1. Legacy {'response': '...'}
    def mock_legacy(model, inputs):
        called_models.append(model)
        return {"response": "Legacy summary text."}

    ai = FakeAI()
    FakeAI.script(mock_legacy)
    env = FakeEnv(ai=ai)
    provider = WorkersAIProvider()

    summary = await provider.summarize(env, "Some input text to summarize " * 10)
    assert summary == "Legacy summary text."
    assert called_models[-1] == "@cf/google/gemma-4-26b-a4b-it"

    # 2. OpenAI / Gemma 4 {'choices': [{'message': {'content': '...'}}]}
    def mock_gemma4_choices(model, inputs):
        called_models.append(model)
        return {
            "id": "chat-123",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Gemma 4 choices summary.",
                    },
                }
            ],
        }

    FakeAI.script(mock_gemma4_choices)
    summary2 = await provider.summarize(env, "Some input text to summarize " * 10)
    assert summary2 == "Gemma 4 choices summary."
    assert called_models[-1] == "@cf/google/gemma-4-26b-a4b-it"

    # 3. Choices with 'text'
    def mock_choices_text(model, inputs):
        return {"choices": [{"text": "Choices text field summary."}]}

    FakeAI.script(mock_choices_text)
    summary3 = await provider.summarize(env, "Some input text to summarize " * 10)
    assert summary3 == "Choices text field summary."

    FakeAI.script(None)


@pytest.mark.asyncio
async def test_generate_and_json_parsing_compatibility():
    """Verify generate handles JSON schema responses and cleans markdown fences."""
    enrichment_data = {
        "summary": "Article about Cloudflare Workers AI.",
        "tags": ["cloudflare", "workers-ai", "edge"],
        "key_takeaways": [
            "Serverless inference on edge",
            "Broad model catalog support",
        ],
    }
    raw_json_str = json.dumps(enrichment_data)

    # 1. Plain JSON string in choices format
    class MockGemmaAI:
        async def run(self, model, payload):
            assert model == "@cf/google/gemma-4-26b-a4b-it"
            assert payload["response_format"] == {
                "type": "json_schema",
                "json_schema": {
                    "name": "enrichment",
                    "schema": schema,
                },
            }
            return {
                "choices": [{"message": {"role": "assistant", "content": raw_json_str}}]
            }

    class Env:
        AI = MockGemmaAI()

    provider = WorkersAIProvider()
    schema = {"type": "object", "properties": {"summary": {"type": "string"}}}

    res = await provider.generate(
        Env(),
        system="Extract enrichment metadata",
        user="Input article text",
        json_schema=schema,
    )
    assert res is not None
    parsed = json.loads(res)
    assert parsed == enrichment_data

    # 2. Markdown fenced JSON string (common LLM behavior)
    fenced_json_str = f"```json\n{raw_json_str}\n```"

    class MockFencedAI:
        async def run(self, model, payload):
            return {
                "choices": [
                    {"message": {"role": "assistant", "content": fenced_json_str}}
                ]
            }

    class EnvFenced:
        AI = MockFencedAI()

    res_fenced = await provider.generate(
        EnvFenced(),
        system="Extract enrichment metadata",
        user="Input article text",
        json_schema=schema,
    )
    assert res_fenced is not None
    # Must be valid JSON directly parseable without error
    parsed_fenced = json.loads(res_fenced)
    assert parsed_fenced == enrichment_data


def test_clean_json_text_helper():
    """Test clean_json_text with various LLM formatting quirks."""
    data = {"hello": "world"}
    raw = json.dumps(data)

    # Bare JSON
    assert clean_json_text(raw) == raw
    assert json.loads(clean_json_text(raw)) == data

    # Fenced JSON with json tag
    fenced = f"```json\n{raw}\n```"
    assert json.loads(clean_json_text(fenced)) == data

    # Fenced without language tag
    fenced_no_lang = f"```\n{raw}\n```"
    assert json.loads(clean_json_text(fenced_no_lang)) == data

    # Thinking tags followed by JSON
    thinking = f"<think>\nReasoning about the json schema...\n</think>\n{raw}"
    assert json.loads(clean_json_text(thinking)) == data

    thought = f"<thought>\nStep 1: Parse input...\n</thought>\n```json\n{raw}\n```"
    assert json.loads(clean_json_text(thought)) == data

    # Empty and whitespace
    assert clean_json_text("") == ""
    assert clean_json_text("   ") == ""


@pytest.mark.asyncio
async def test_pyodide_jsproxy_handling():
    """Simulate Pyodide JsProxy response conversion."""

    class MockJsProxy:
        def __init__(self, data):
            self._data = data

        def to_py(self):
            return self._data

    class MockJsProxyAI:
        async def run(self, model, payload):
            return MockJsProxy(
                {
                    "choices": [
                        {"message": {"role": "assistant", "content": "From JsProxy"}}
                    ]
                }
            )

    class EnvJsProxy:
        AI = MockJsProxyAI()

    provider = WorkersAIProvider()
    res = await provider.generate(EnvJsProxy(), "sys", "usr")
    assert res == "From JsProxy"

    sum_res = await provider.summarize(EnvJsProxy(), "test text " * 10)
    assert sum_res == "From JsProxy"


@pytest.mark.asyncio
async def test_error_and_timeout_fallbacks():
    """Verify precondition fallbacks return None while runtime AI failures
    raise retryable errors.
    """

    class FailingAI:
        async def run(self, model, payload):
            raise RuntimeError("Cloudflare AI service unavailable")

    class EnvFail:
        AI = FailingAI()

    provider = WorkersAIProvider()

    # Runtime AI errors raise WorkersAIRetryableError
    with pytest.raises(WorkersAIRetryableError) as exc_sum:
        await provider.summarize(EnvFail(), "test text " * 10)
    assert exc_sum.value.retryable is True
    assert exc_sum.value.model == DEFAULT_TEXT_MODEL
    assert isinstance(exc_sum.value.cause, RuntimeError)

    with pytest.raises(WorkersAIRetryableError) as exc_gen:
        await provider.generate(EnvFail(), "sys", "usr")
    assert exc_gen.value.retryable is True

    class TimeoutAI:
        async def run(self, model, payload):
            await asyncio.sleep(10)
            return {"response": "too late"}

    class EnvTimeout:
        AI = TimeoutAI()

    # Timeouts raise WorkersAIRetryableError
    with pytest.raises(WorkersAIRetryableError) as exc_to:
        await provider.summarize(EnvTimeout(), "test text " * 10)
    assert exc_to.value.retryable is True

    # Precondition fallbacks (None env, missing AI, empty text) return None
    # without calling AI
    assert await provider.summarize(None, "test") is None
    assert await provider.summarize(EnvFail(), "") is None
    assert await provider.generate(None, "sys", "usr") is None
    assert await provider.embed(None, ["test"]) == []


@pytest.mark.asyncio
async def test_workers_ai_request_contract_json_schema_wrapper():
    """Validate Workers AI request contract: response_format must wrap
    schema with name and schema.
    """
    captured_payloads: list[tuple[str, dict]] = []

    class CapturingAI:
        async def run(self, model, payload):
            captured_payloads.append((model, payload))
            return {"response": json.dumps({"summary": "test", "tags": ["t1"]})}

    env = FakeEnv(ai=CapturingAI())
    provider = WorkersAIProvider()

    tags_schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["summary", "tags"],
    }

    # 1. Raw TAGS_SCHEMA gets wrapped with name="enrichment" and schema=TAGS_SCHEMA
    res = await provider.generate(
        env,
        system="System prompt",
        user="Article content",
        json_schema=tags_schema,
    )
    assert res is not None
    assert len(captured_payloads) == 1
    model, payload = captured_payloads[0]
    assert model == DEFAULT_TEXT_MODEL
    assert payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "enrichment",
            "schema": tags_schema,
        },
    }

    # 2. Schema already wrapped with name and schema is not double-wrapped
    already_wrapped = {
        "name": "enrichment",
        "schema": tags_schema,
    }
    await provider.generate(
        env,
        system="System prompt",
        user="Article content",
        json_schema=already_wrapped,
    )
    assert len(captured_payloads) == 2
    assert captured_payloads[1][1]["response_format"] == {
        "type": "json_schema",
        "json_schema": already_wrapped,
    }

    # 3. Schema with explicit custom name
    custom_named_schema = {
        "name": "custom_enrichment",
        "type": "object",
        "properties": {"tag": {"type": "string"}},
    }
    await provider.generate(
        env,
        system="System prompt",
        user="Article content",
        json_schema=custom_named_schema,
    )
    assert len(captured_payloads) == 3
    assert captured_payloads[2][1]["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "custom_enrichment",
            "schema": custom_named_schema,
        },
    }

    # 4. Without json_schema, response_format is not in payload
    await provider.generate(
        env,
        system="System prompt",
        user="Article content",
        json_schema=None,
    )
    assert len(captured_payloads) == 4
    assert "response_format" not in captured_payloads[3][1]


@pytest.mark.asyncio
async def test_workers_ai_failure_propagation():
    """Verify Workers AI errors are distinguishable and retryable."""

    class ServiceUnavailableAI:
        async def run(self, model, payload):
            raise RuntimeError("Cloudflare 503 Service Unavailable")

    env = FakeEnv(ai=ServiceUnavailableAI())
    provider = WorkersAIProvider()

    # generate() raises WorkersAIRetryableError
    with pytest.raises(WorkersAIRetryableError) as exc_gen:
        await provider.generate(env, "sys", "usr")
    assert isinstance(exc_gen.value, WorkersAIError)
    assert exc_gen.value.retryable is True
    assert exc_gen.value.model == DEFAULT_TEXT_MODEL
    assert "RuntimeError" in str(exc_gen.value)

    # summarize() raises WorkersAIRetryableError
    with pytest.raises(WorkersAIRetryableError) as exc_sum:
        await provider.summarize(env, "article text " * 10)
    assert isinstance(exc_sum.value, WorkersAIError)
    assert exc_sum.value.retryable is True
    assert exc_sum.value.model == DEFAULT_TEXT_MODEL

    # embed() raises WorkersAIRetryableError
    with pytest.raises(WorkersAIRetryableError) as exc_emb:
        await provider.embed(env, ["test vector text"])
    assert isinstance(exc_emb.value, WorkersAIError)
    assert exc_emb.value.retryable is True
    assert exc_emb.value.model == DEFAULT_EMBEDDING_MODEL


@pytest.mark.asyncio
async def test_workers_ai_safe_error_logging(caplog):
    """Ensure error logging contains safe metadata without secrets or user content."""

    class FailingAI:
        async def run(self, model, payload):
            raise RuntimeError("Internal Model Server Error 500")

    secret_user_content = "SECRET_USER_DIARY_CONFIDENTIAL_12345"
    system_prompt = "TOP_SECRET_SYSTEM_INSTRUCTIONS_98765"

    env = FakeEnv(ai=FailingAI())
    provider = WorkersAIProvider()

    with caplog.at_level(logging.WARNING, logger="keepfor.defaults"):
        with pytest.raises(WorkersAIRetryableError):
            await provider.generate(
                env,
                system=system_prompt,
                user=secret_user_content,
            )

    # Check warning was logged with metadata
    assert len(caplog.records) >= 1
    log_text = caplog.text
    assert DEFAULT_TEXT_MODEL in log_text
    assert "error_type=RuntimeError" in log_text
    assert "timeout=False" in log_text

    # Verify secret content and system prompt are NOT in the log
    assert secret_user_content not in log_text
    assert system_prompt not in log_text


@pytest.mark.asyncio
async def test_generate_triage_summary_fails_open_when_ai_raises():
    """Verify generate_triage_summary returns None when ai.summarize raises."""
    from keepfor.runtime import set_providers
    from keepfor.spi import Providers

    class FailingAI:
        async def embed(self, env, texts):
            return []

        async def summarize(self, env, text, *, max_words=40):
            raise WorkersAIRetryableError("AI service down", model="test-model")

        async def generate(self, env, system, user, **kwargs):
            return None

    providers = Providers(ai=FailingAI()).resolved()
    set_providers(providers)

    class DummyEnv:
        AI = object()

    result = await generate_triage_summary(DummyEnv(), "Some article content")
    assert result is None

    # Reset providers
    set_providers(Providers().resolved())


@pytest.mark.asyncio
async def test_processor_generate_triage_summary():
    """Verify processor.generate_triage_summary runs through get_providers().ai."""
    from keepfor.runtime import set_providers
    from keepfor.spi import Providers

    class MockAI:
        async def embed(self, env, texts):
            return []

        async def summarize(self, env, text, *, max_words=40):
            return f"Triage: {text[:20]}"

        async def generate(self, env, system, user, **kwargs):
            return None

    providers = Providers(ai=MockAI()).resolved()
    set_providers(providers)

    class DummyEnv:
        AI = object()

    result = await generate_triage_summary(DummyEnv(), "Hello world triage summary")
    assert result == "Triage: Hello world triage s"

    # Reset providers
    set_providers(Providers().resolved())


def test_extract_text_response_formats():
    """Verify _extract_text_response handles dicts, objects, strings,
    choices, and edge cases.
    """
    # Direct strings
    assert _extract_text_response("hello") == "hello"
    assert _extract_text_response("  padded text  ") == "padded text"
    assert _extract_text_response("") is None
    assert _extract_text_response("   ") is None

    # None
    assert _extract_text_response(None) is None

    # Dict legacy response
    assert _extract_text_response({"response": "legacy"}) == "legacy"
    assert _extract_text_response({"response": "  trimmed  "}) == "trimmed"
    assert _extract_text_response({"response": ""}) is None
    assert _extract_text_response({"response": None}) is None

    # Dict choices with message dict
    assert (
        _extract_text_response({"choices": [{"message": {"content": "gemma output"}}]})
        == "gemma output"
    )
    assert (
        _extract_text_response({"choices": [{"message": {"content": "   "}}]}) is None
    )
    assert _extract_text_response({"choices": [{"message": {"content": None}}]}) is None

    # Dict choices with text field
    assert (
        _extract_text_response({"choices": [{"text": "completion text"}]})
        == "completion text"
    )

    # Dict choices with raw string
    assert (
        _extract_text_response({"choices": ["raw choice string"]})
        == "raw choice string"
    )

    # Dict empty choices
    assert _extract_text_response({"choices": []}) is None

    # Object with response attribute
    class ObjResponse:
        response = "from obj response"

    assert _extract_text_response(ObjResponse()) == "from obj response"

    # Object with choices attribute containing message object
    class MsgObj:
        content = "from message obj"

    class ChoiceObj:
        message = MsgObj()

    class ResObj:
        choices = [ChoiceObj()]

    assert _extract_text_response(ResObj()) == "from message obj"

    # Object with choices attribute containing text attribute
    class ChoiceTextObj:
        text = "from choice text obj"

    class ResChoiceTextObj:
        choices = [ChoiceTextObj()]

    assert _extract_text_response(ResChoiceTextObj()) == "from choice text obj"

    # Object with choices attribute containing string
    class ResStringChoices:
        choices = ["choice string in obj"]

    assert _extract_text_response(ResStringChoices()) == "choice string in obj"

    # Arbitrary object with no matching attributes
    class EmptyObj:
        pass

    assert _extract_text_response(EmptyObj()) is None
