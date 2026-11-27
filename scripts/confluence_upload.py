"""Загрузка выгрузки «Где написано?» в пространство Confluence через REST API.

    CONFLUENCE_URL=https://example.atlassian.net/wiki CONFLUENCE_SPACE=GN \\
    CONFLUENCE_USER=me@example.com CONFLUENCE_TOKEN=... \\
    python scripts/confluence_upload.py gde-napisano-confluence.zip [--dry-run]

Страницы создаются в порядке manifest.json: корень, под ним регламенты и разъяснения. Если страница с таким
названием в пространстве уже есть, она обновляется (новая версия). Токен не печатается.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import zipfile

import httpx


def load(path: str) -> tuple[list[dict], dict]:
    with zipfile.ZipFile(path) as z:
        manifest = json.loads(z.read("manifest.json"))
        bodies = {p["storage"]: z.read(p["storage"]).decode("utf-8") for p in manifest["pages"]}
    return manifest["pages"], bodies


def upload(pages: list[dict], bodies: dict, client: httpx.Client, space: str) -> dict[str, str]:
    ids: dict[str, str] = {}
    for p in pages:
        found = client.get("/rest/api/content", params={"spaceKey": space, "title": p["title"],
                                                         "expand": "version"}).raise_for_status().json()
        body = {"type": "page", "title": p["title"], "space": {"key": space},
                "body": {"storage": {"value": bodies[p["storage"]], "representation": "storage"}}}
        if p.get("parent") and p["parent"] in ids:
            body["ancestors"] = [{"id": ids[p["parent"]]}]
        if found.get("results"):
            cur = found["results"][0]
            body["version"] = {"number": int(cur["version"]["number"]) + 1}
            r = client.put(f"/rest/api/content/{cur['id']}", json=body).raise_for_status().json()
        else:
            r = client.post("/rest/api/content", json=body).raise_for_status().json()
        ids[p["title"]] = str(r["id"])
        print(f"{p['title']}: {r['id']}")
    return ids


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("zip")
    ap.add_argument("--dry-run", action="store_true", help="только показать, какие страницы будут созданы")
    a = ap.parse_args()
    pages, bodies = load(a.zip)
    if a.dry_run:
        for p in pages:
            print(f"{p['title']}  (родитель: {p.get('parent') or '—'}, {len(bodies[p['storage']])} симв.)")
        return 0
    env = {k: os.environ.get(k, "") for k in ("CONFLUENCE_URL", "CONFLUENCE_SPACE", "CONFLUENCE_USER", "CONFLUENCE_TOKEN")}
    missing = [k for k, v in env.items() if not v]
    if missing:
        print("Не заданы: " + ", ".join(missing))
        return 2
    with httpx.Client(base_url=env["CONFLUENCE_URL"].rstrip("/"), auth=(env["CONFLUENCE_USER"], env["CONFLUENCE_TOKEN"]),
                      timeout=60) as c:
        upload(pages, bodies, c, env["CONFLUENCE_SPACE"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
