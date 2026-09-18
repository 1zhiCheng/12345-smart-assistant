import asyncio

from app.retrieval.desktop_vector_cache import load_or_build_desktop_vectors


class DummyEmbeddings:
    model = "test-bge"

    def __init__(self):
        self.calls = 0

    async def embed(self, texts):
        self.calls += 1
        return [[float(index + 1), 0.5] for index, _ in enumerate(texts)]


def test_desktop_vector_cache_reuses_same_official_chunks(tmp_path):
    chunks = [{"_id": "a", "content": "城市管理官方文档"}, {"_id": "b", "content": "公共服务官方文档"}]
    embeddings = DummyEmbeddings()
    path = tmp_path / "desktop_vectors.npz"

    first, first_status = asyncio.run(load_or_build_desktop_vectors(embeddings, chunks, path))
    second, second_status = asyncio.run(load_or_build_desktop_vectors(embeddings, chunks, path))

    assert first_status == "rebuilt"
    assert second_status == "hit"
    assert embeddings.calls == 1
    assert first == second


def test_desktop_vector_cache_rebuilds_when_official_chunk_changes(tmp_path):
    embeddings = DummyEmbeddings()
    path = tmp_path / "desktop_vectors.npz"
    asyncio.run(load_or_build_desktop_vectors(embeddings, [{"_id": "a", "content": "旧内容"}], path))
    _, status = asyncio.run(load_or_build_desktop_vectors(embeddings, [{"_id": "a", "content": "新内容"}], path))

    assert status == "rebuilt"
    assert embeddings.calls == 2
