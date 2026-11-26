"""Скачивает модели поиска (эмбеддинги и реранкер) в кэш HF_HOME заранее.

Нужны только веса safetensors, токенизатор и конфиги: варианты ONNX/OpenVINO и дубли весов .bin
не скачиваются (вместо ~7 ГБ получается ~3,4 ГБ).

    python scripts/prefetch_models.py                      # модели из .env / переменных окружения
    docker compose run --rm app python scripts/prefetch_models.py

После этого можно включить HF_HUB_OFFLINE=1: стенд поднимется без доступа к huggingface.co.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ALLOW = ["*.json", "*.safetensors", "*.model", "*.txt", "1_Pooling/*"]
IGNORE = ["onnx/*", "openvino/*", "*.onnx", "*.bin", "*.h5", "*.msgpack", "*.ot", ".eval_results/*"]


def main() -> int:
    from huggingface_hub import snapshot_download

    embed = os.environ.get("EMBED_MODEL")
    rerank = os.environ.get("RERANKER_MODEL")
    if not embed or not rerank:
        try:
            from app.config import Settings

            s = Settings()
            embed, rerank = embed or s.embed_model, rerank or s.reranker_model
        except Exception:
            embed = embed or "intfloat/multilingual-e5-base"
            rerank = rerank or "BAAI/bge-reranker-v2-m3"
    for repo in (embed, rerank):
        print(f"Скачиваю {repo} в {os.environ.get('HF_HOME', '~/.cache/huggingface')} …", flush=True)
        path = snapshot_download(repo_id=repo, allow_patterns=ALLOW, ignore_patterns=IGNORE)
        size = sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file()) / 1e9
        print(f"  готово: {size:.1f} ГБ", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
