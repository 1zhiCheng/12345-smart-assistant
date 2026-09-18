"""OpenAI 兼容大模型客户端请求参数测试。"""
import httpx
import pytest

from app.config import Settings
from app.llm.client import ChatMessage, LLMClient
from app.llm.embeddings import EmbeddingClient


class _TestClient(LLMClient):
    def name(self) -> str:
        return "test"


@pytest.mark.asyncio
async def test_json_completion_disables_default_thinking(monkeypatch):
    captured = {}

    async def fake_post(self, url, **kwargs):
        captured.update(kwargs["json"])
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": '{"status":"ok"}'}}]},
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    client = _TestClient("https://llm.example/v1", "test-key", "test-model")
    result = await client.complete_json([ChatMessage.user("返回 JSON")])

    assert result == {"status": "ok"}
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_embedding_relay_missing_key_fails_closed():
    settings = Settings(
        embedding_provider="relay",
        relay_api_key="",
        embedding_allow_hash_fallback=False,
    )
    client = EmbeddingClient(settings, relay=None)
    with pytest.raises(RuntimeError, match="禁止静默降级"):
        await client.embed(["政务服务"])


@pytest.mark.asyncio
async def test_embedding_hash_fallback_requires_explicit_opt_in():
    settings = Settings(
        embedding_provider="relay",
        relay_api_key="",
        embedding_allow_hash_fallback=True,
        embedding_dim=128,
    )
    client = EmbeddingClient(settings, relay=None)
    vectors = await client.embed(["政务服务"])
    assert len(vectors[0]) == 128
    assert client.last_effective_provider == "hash-fallback"
