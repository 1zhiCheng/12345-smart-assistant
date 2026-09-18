"""用冻结留出集之外的官方文档生成部门多原型 BGE 向量。"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from app.domain.wuhu import CATEGORY_TO_DEPT, DEPARTMENTS, DEPARTMENT_KEYWORDS
from scripts.build_routing_holdout import clean_markdown


def _normalized_path(value: str) -> str:
    return value.replace("\\", "/")


def _unit(vector: np.ndarray) -> np.ndarray:
    return vector / max(float(np.linalg.norm(vector)), 1e-9)


def _prototypes(vectors: np.ndarray) -> list[list[float]]:
    # 12 个部门内部事项跨度很大，聚类中心会抹平低频业务。240 份文档原型
    # 只有约 12 万个 float，运行时逐一余弦相似度仍是毫秒级。
    return [_unit(vector).round(7).tolist() for vector in vectors]


def _descriptor(dept: dict) -> str:
    keywords = "、".join(DEPARTMENT_KEYWORDS[dept["_id"]])
    return f"{dept['name']}。12345事项类别：{dept['category']}。典型职责和诉求：{keywords}。"


def _semantic_score(
    query: np.ndarray, documents: np.ndarray, descriptor: np.ndarray, top_n: int, descriptor_weight: float
) -> float:
    similarities = np.sort(documents @ query)[::-1]
    document_score = float(np.mean(similarities[: min(top_n, len(similarities))]))
    return (1.0 - descriptor_weight) * document_score + descriptor_weight * float(descriptor @ query)


def build(args: argparse.Namespace) -> dict:
    if args.output.exists() and not args.force:
        raise FileExistsError(f"原型已存在，拒绝覆盖：{args.output}")
    holdout = json.loads(args.holdout.read_text(encoding="utf-8"))
    excluded = {_normalized_path(case["source_file"]) for case in holdout["cases"]}
    records = [json.loads(line) for line in args.manifest.read_text(encoding="utf-8").splitlines() if line.strip()]

    rows = []
    for record in records:
        relative = _normalized_path(record["relative_path"])
        if relative in excluded:
            continue
        path = args.corpus / Path(relative)
        if not path.exists() or record["category"] not in CATEGORY_TO_DEPT:
            continue
        title, body = clean_markdown(path)
        if len(body) < 120:
            continue
        rows.append({"category": record["category"], "relative": relative, "text": f"{title}\n{body[:1800]}"})
    if {row["category"] for row in rows} != set(CATEGORY_TO_DEPT):
        raise ValueError("非留出官方文档未覆盖全部 12 类")

    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(args.model, local_files_only=True)
    texts = [row["text"] for row in rows]
    vectors = np.asarray(model.encode(texts, batch_size=args.batch_size, normalize_embeddings=True, show_progress_bar=True))

    by_category: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        by_category[row["category"]].append(index)

    descriptors = [_descriptor(dept) for dept in DEPARTMENTS]
    descriptor_vectors = np.asarray(model.encode(descriptors, normalize_embeddings=True))
    descriptor_by_category = {
        dept["category"]: vector for dept, vector in zip(DEPARTMENTS, descriptor_vectors, strict=True)
    }

    # 只用训练片构建临时原型，在未参与原型构建的开发片上选择固定聚合策略。
    train: dict[str, list[int]] = {}
    dev: dict[str, list[int]] = {}
    for category, indexes in by_category.items():
        ordered = sorted(indexes, key=lambda idx: hashlib.sha256(f"routing-dev-v1:{rows[idx]['relative']}".encode()).hexdigest())
        dev_count = max(2, round(len(ordered) * 0.2))
        dev[category], train[category] = ordered[:dev_count], ordered[dev_count:]
    train_vectors = {category: vectors[indexes] for category, indexes in train.items()}
    candidates = []
    for top_n in (1, 2, 3, 5, 8):
        for descriptor_weight in (0.0, 0.1, 0.2, 0.3):
            for keyword_bonus in (0.0, 0.04, 0.08, 0.12):
                dev_total = dev_top1 = dev_topk = 0
                for expected, indexes in dev.items():
                    for index in indexes:
                        text = rows[index]["text"]
                        scores = []
                        for category, documents in train_vectors.items():
                            dept_id = CATEGORY_TO_DEPT[category]
                            score = _semantic_score(
                                vectors[index], documents, descriptor_by_category[category], top_n, descriptor_weight
                            )
                            if any(keyword in text for keyword in DEPARTMENT_KEYWORDS[dept_id]):
                                score += keyword_bonus
                            scores.append((category, score))
                        scores.sort(key=lambda item: (-item[1], item[0]))
                        dev_total += 1
                        dev_top1 += int(scores[0][0] == expected)
                        dev_topk += int(expected in {category for category, _ in scores[:3]})
                candidates.append({
                    "top_n": top_n,
                    "descriptor_weight": descriptor_weight,
                    "keyword_bonus": keyword_bonus,
                    "cases": dev_total,
                    "top1_accuracy": round(dev_top1 / dev_total, 4),
                    "top3_accuracy": round(dev_topk / dev_total, 4),
                })
    selected = max(candidates, key=lambda item: (item["top1_accuracy"], item["top3_accuracy"], -item["top_n"]))

    prototypes = {}
    for dept, descriptor_vector in zip(DEPARTMENTS, descriptor_vectors, strict=True):
        category = dept["category"]
        prototypes[dept["_id"]] = {
            "documents": _prototypes(vectors[by_category[category]]),
            "descriptor": _unit(descriptor_vector).round(7).tolist(),
        }

    payload = {
        "metadata": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "model": args.model,
            "dimension": int(vectors.shape[1]),
            "method": "nearest_official_document_prototypes_plus_department_descriptor",
            "aggregation": {
                "top_n": selected["top_n"],
                "descriptor_weight": selected["descriptor_weight"],
                "recommended_keyword_bonus": selected["keyword_bonus"],
            },
            "source": "official_documents_excluding_frozen_holdout",
            "source_documents": len(rows),
            "excluded_holdout_documents": len(excluded),
            "development_evaluation": {
                "cases": selected["cases"],
                "top1_accuracy": selected["top1_accuracy"],
                "top3_accuracy": selected["top3_accuracy"],
                "split": "sha256(routing-dev-v1:relative_path), 20% per category",
                "selection_grid": "top_n x descriptor_weight x keyword_bonus; selected on development only",
            },
            "privacy": "no_source_text_or_paths_stored",
        },
        "prototypes": prototypes,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return payload["metadata"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=Path("wuhu_knowledge_base"))
    parser.add_argument("--manifest", type=Path, default=Path("wuhu_knowledge_base/manifest.jsonl"))
    parser.add_argument("--holdout", type=Path, default=Path("backend/evaluation/routing_holdout.frozen.json"))
    parser.add_argument("--output", type=Path, default=Path("backend/resources/routing_prototypes.bge-small-zh-v1.5.json"))
    parser.add_argument("--model", default="BAAI/bge-small-zh-v1.5")
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    print(json.dumps(build(args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
