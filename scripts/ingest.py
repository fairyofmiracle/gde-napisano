"""Проиндексировать папку документов (только изменения) без запуска сервера.

    python scripts/ingest.py            # папка из DOCS_DIR
    python scripts/ingest.py --copy-from путь\\к\\регламентам   # сначала скопировать файлы в DOCS_DIR
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.services import build_services  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--copy-from", help="папка с документами; подпапки = ящички (hr, it, finance…)")
    args = ap.parse_args()
    settings = get_settings()
    if args.copy_from:
        shutil.copytree(args.copy_from, settings.docs_dir, dirs_exist_ok=True)
    svc = build_services(settings)
    try:
        print(json.dumps(svc.indexer.sync().as_dict(), ensure_ascii=False, indent=2))
    finally:
        svc.close()


if __name__ == "__main__":
    main()
