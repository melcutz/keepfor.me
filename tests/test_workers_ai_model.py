# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""Tests for Workers AI text model update and enrichment JSON parsing compatibility."""

import asyncio
import json

import pytest

from keepfor.consumer.processor import SUMMARY_MODEL, generate_triage_summary
from keepfor.defaults import (
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_TEXT_MODEL,
    WorkersAIProvider,
    clean_json_text,
)
from tests.fakes import FakeAI, FakeEnv


def test_text_model_constants():
    """Verify that text model constants point to supported Gemma 4 model."""
    assert DEFAULT_TEXT_MODEL == "@cf/google/gemma-4-26b-a4b-it"
    assert SUMMARY_MODEL == DEFAULT_TEXT_MODEL
    # Embedding model must remain untouched
    assert DEFAULT_EMBEDDING_MODEL == "@cf/baai/bge-base-en-v1.5"


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
            assert "response_format" in payload
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
    """WorkersAIProvider fails open (returns None) on errors or timeouts."""

    class FailingAI:
        async def run(self, model, payload):
            raise RuntimeError("Cloudflare AI service unavailable")

    class EnvFail:
        AI = FailingAI()

    provider = WorkersAIProvider()
    assert await provider.summarize(EnvFail(), "test text " * 10) is None
    assert await provider.generate(EnvFail(), "sys", "usr") is None

    class TimeoutAI:
        async def run(self, model, payload):
            await asyncio.sleep(10)
            return {"response": "too late"}

    class EnvTimeout:
        AI = TimeoutAI()

    assert await provider.summarize(EnvTimeout(), "test text " * 10) is None

    # None and empty inputs
    assert await provider.summarize(None, "test") is None
    assert await provider.summarize(EnvFail(), "") is None
    assert await provider.generate(None, "sys", "usr") is None


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
