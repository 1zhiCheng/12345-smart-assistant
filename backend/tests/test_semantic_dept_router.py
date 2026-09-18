import json

import pytest

from app.config import Settings
from app.harness.agents.semantic_dept_router import SemanticDepartmentRouter


class FakeEmbeddings:
    def __init__(self, artifact_path):
        self.model = "test-model"
        self.settings = Settings(
            embedding_model=self.model,
            embedding_dim=2,
            routing_prototype_path=str(artifact_path),
        )

    async def embed_query(self, _query):
        return [0.9, 0.1]


@pytest.mark.asyncio
async def test_semantic_department_router_ranks_valid_prototypes(tmp_path):
    artifact = tmp_path / "prototypes.json"
    artifact.write_text(json.dumps({
        "metadata": {"model": "test-model", "dimension": 2},
        "prototypes": {
            "dept_a": {"documents": [[1.0, 0.0]], "descriptor": [1.0, 0.0]},
            "dept_b": {"documents": [[0.0, 1.0]], "descriptor": [0.0, 1.0]},
            "dept_hidden": {"documents": [[1.0, 0.0]], "descriptor": [1.0, 0.0]},
        },
    }), encoding="utf-8")
    router = SemanticDepartmentRouter(FakeEmbeddings(artifact), str(artifact))

    ranked = await router.rank("测试诉求", {"dept_a", "dept_b"})

    assert [dept for dept, _ in ranked] == ["dept_a", "dept_b"]
    assert ranked[0][1] > ranked[1][1]
