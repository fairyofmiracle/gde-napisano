"""Выгрузка хранилища для Confluence: регламенты по пунктам с якорями и разъяснения службы качества.

Архив (zip):
- storage/*.xml — страницы в формате хранения Confluence (storage format), ссылки между страницами
  через ac:link и якоря через макрос anchor;
- html/*.html — те же страницы обычным HTML, чтобы посмотреть или перенести вручную;
- manifest.json — порядок и иерархия страниц (корень → регламенты, разъяснения);
- README.txt — коротко, как импортировать (подробно в docs/confluence.md).
Загрузка в пространство: scripts/confluence_upload.py по manifest.json через REST API.
"""
from __future__ import annotations

import html
import io
import json
import re
import time
import zipfile

ROOT_TITLE = "Где написано? Регламенты"
FAQ_TITLE = "Разъяснения службы качества"


def _esc(text: str) -> str:
    return html.escape(text or "", quote=True)


def _cdata(text: str) -> str:
    return "<![CDATA[" + (text or "").replace("]]>", "]]]]><![CDATA[>") + "]]>"


def anchor_name(clause_no: str, ord_: int) -> str:
    base = re.sub(r"[^0-9A-Za-zА-Яа-яЁё.\-]+", "-", (clause_no or "").strip()).strip("-.")
    return f"p-{base}" if base else f"o-{ord_}"


def _slug(i: int, title: str) -> str:
    s = re.sub(r"[^0-9A-Za-zА-Яа-яЁё]+", "-", title).strip("-").lower()[:60]
    return f"{i:02d}-{s or 'doc'}"


def _paras(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n+", text or "") if p.strip()]


def collect(db, boxes=None) -> dict:
    """Документы с пунктами (части одного пункта склеены) и активные разъяснения."""
    docs = []
    for i, f in enumerate(db.query("SELECT doc_id, title, box, mtime FROM files WHERE status='ok' AND n_clauses > 0 "
                                   "ORDER BY box, title"), start=1):
        rows = db.query("SELECT point_id, ord, clause_no, label, section, page, kind, text FROM clauses WHERE doc_id=? "
                        "ORDER BY ord", (f["doc_id"],))
        clauses, seen = [], {}
        for r in rows:
            last = clauses[-1] if clauses else None
            if last and r["clause_no"] and r["clause_no"] == last["clause_no"] and r["kind"] == last["kind"]:
                last["text"] += "\n" + r["text"]
                last["point_ids"].append(r["point_id"])
                continue
            name = anchor_name(r["clause_no"], r["ord"])
            if name in seen:
                seen[name] += 1
                name = f"{name}-{seen[name]}"
            else:
                seen[name] = 1
            clauses.append({"anchor": name, "clause_no": r["clause_no"], "label": r["label"] or "", "section": r["section"], "page": r["page"],
                            "kind": r["kind"], "text": r["text"], "point_ids": [r["point_id"]]})
        box_name = boxes.name(f["box"]) if boxes is not None and f["box"] else (f["box"] or "")
        docs.append({"doc_id": f["doc_id"], "title": f["title"], "box": f["box"] or "", "box_name": box_name,
                     "slug": _slug(i, f["title"]), "clauses": clauses})
    faq = [dict(r) for r in db.query("SELECT id, created_at, question, answer, doc_id, doc_title, clause_no, "
                                     "escalation_id FROM faq WHERE active=1 ORDER BY id")]
    return {"documents": docs, "faq": faq}


def _clause_anchor(doc: dict, clause_no: str) -> str | None:
    for c in doc["clauses"]:
        if c["clause_no"] == clause_no:
            return c["anchor"]
    return None


def _faq_by_clause(data: dict) -> dict:
    out: dict = {}
    for f in data["faq"]:
        if f.get("doc_id"):
            out.setdefault((f["doc_id"], f.get("clause_no") or ""), []).append(f)
    return out


def _label(c: dict) -> str:
    if c["kind"] == "figure":
        return f"Рисунок {c['clause_no']}".strip()
    no = c.get("label") or c["clause_no"]
    return f"п. {no}" if no else "Без номера"


# --- формат хранения Confluence -----------------------------------------------------------
def _st_link(title: str, anchor: str | None, text: str) -> str:
    a = f' ac:anchor="{_esc(anchor)}"' if anchor else ""
    return (f'<ac:link{a}><ri:page ri:content-title="{_esc(title)}" />'
            f"<ac:plain-text-link-body>{_cdata(text)}</ac:plain-text-link-body></ac:link>")


def _st_anchor(name: str) -> str:
    return (f'<ac:structured-macro ac:name="anchor"><ac:parameter ac:name="">{_esc(name)}</ac:parameter>'
            "</ac:structured-macro>")


def storage_doc(doc: dict, data: dict) -> str:
    fbc = _faq_by_clause(data)
    parts = [f"<p><em>{_esc(doc['box_name'])}</em></p>" if doc["box_name"] else ""]
    section = None
    for c in doc["clauses"]:
        if c["section"] and c["section"] != section:
            section = c["section"]
            parts.append(f"<h2>{_esc(section)}</h2>")
        parts.append(f"<h3>{_st_anchor(c['anchor'])}{_esc(_label(c))}</h3>")
        parts += [f"<p>{_esc(p)}</p>" for p in _paras(c["text"])]
        for f in fbc.get((doc["doc_id"], c["clause_no"]), []):
            parts.append(f"<p><strong>Разъяснение службы качества:</strong> "
                         f"{_st_link(FAQ_TITLE, f'faq-{f["id"]}', f['question'])}</p>")
    return "".join(parts)


def storage_faq(data: dict) -> str:
    docs = {d["doc_id"]: d for d in data["documents"]}
    if not data["faq"]:
        return "<p>Разъяснений пока нет.</p>"
    parts = []
    for f in data["faq"]:
        parts.append(f"<h3>{_st_anchor(f'faq-{f["id"]}')}{_esc(f['question'])}</h3>")
        parts += [f"<p>{_esc(p)}</p>" for p in _paras(f["answer"])]
        meta = [f"Добавлено {_esc(str(f['created_at'])[:10])}"]
        if f.get("escalation_id"):
            meta.append(f"по заявке ГН-{int(f['escalation_id'])}")
        d = docs.get(f.get("doc_id") or "")
        if d:
            anchor = _clause_anchor(d, f.get("clause_no") or "")
            label = d["title"] + (f", п. {f['clause_no']}" if f.get("clause_no") else "")
            meta.append("связано с " + _st_link(d["title"], anchor, label))
        parts.append(f"<p><em>{', '.join(meta)}</em></p>")
    return "".join(parts)


def storage_root(data: dict) -> str:
    items = "".join(f"<li>{_st_link(d['title'], None, d['title'])}"
                    + (f" ({_esc(d['box_name'])})" if d["box_name"] else "") + "</li>" for d in data["documents"])
    return ("<p>Регламенты по пунктам с якорями и разъяснения службы качества. Выгружено из «Где написано?» "
            f"{time.strftime('%d.%m.%Y')}.</p><ul>{items}<li>{_st_link(FAQ_TITLE, None, FAQ_TITLE)}</li></ul>")


# --- обычный HTML ----------------------------------------------------------------------------
_HTML_HEAD = ('<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>{t}</title></head>'
              '<body><p><a href="index.html">{root}</a></p><h1>{t}</h1>')


def html_doc(doc: dict, data: dict) -> str:
    fbc = _faq_by_clause(data)
    out = [_HTML_HEAD.format(t=_esc(doc["title"]), root=_esc(ROOT_TITLE))]
    if doc["box_name"]:
        out.append(f"<p><em>{_esc(doc['box_name'])}</em></p>")
    section = None
    for c in doc["clauses"]:
        if c["section"] and c["section"] != section:
            section = c["section"]
            out.append(f"<h2>{_esc(section)}</h2>")
        out.append(f'<h3 id="{_esc(c["anchor"])}">{_esc(_label(c))}</h3>')
        out += [f"<p>{_esc(p)}</p>" for p in _paras(c["text"])]
        for f in fbc.get((doc["doc_id"], c["clause_no"]), []):
            out.append(f'<p><strong>Разъяснение службы качества:</strong> '
                       f'<a href="faq.html#faq-{f["id"]}">{_esc(f["question"])}</a></p>')
    return "".join(out) + "</body></html>"


def html_faq(data: dict) -> str:
    docs = {d["doc_id"]: d for d in data["documents"]}
    out = [_HTML_HEAD.format(t=_esc(FAQ_TITLE), root=_esc(ROOT_TITLE))]
    if not data["faq"]:
        out.append("<p>Разъяснений пока нет.</p>")
    for f in data["faq"]:
        out.append(f'<h3 id="faq-{f["id"]}">{_esc(f["question"])}</h3>')
        out += [f"<p>{_esc(p)}</p>" for p in _paras(f["answer"])]
        d = docs.get(f.get("doc_id") or "")
        if d:
            anchor = _clause_anchor(d, f.get("clause_no") or "")
            href = f"{d['slug']}.html" + (f"#{anchor}" if anchor else "")
            label = d["title"] + (f", п. {f['clause_no']}" if f.get("clause_no") else "")
            out.append(f'<p><em>Связано с <a href="{_esc(href)}">{_esc(label)}</a></em></p>')
    return "".join(out) + "</body></html>"


def html_root(data: dict) -> str:
    out = [_HTML_HEAD.format(t=_esc(ROOT_TITLE), root=_esc(ROOT_TITLE)), "<ul>"]
    for d in data["documents"]:
        out.append(f'<li><a href="{d["slug"]}.html">{_esc(d["title"])}</a></li>')
    out.append(f'<li><a href="faq.html">{_esc(FAQ_TITLE)}</a></li></ul></body></html>')
    return "".join(out)


README = """Выгрузка «Где написано?» для Confluence.

storage/      страницы в формате хранения Confluence (storage format)
html/         те же страницы обычным HTML (index.html — начало)
manifest.json порядок и иерархия страниц

Загрузка в пространство через REST API:
  CONFLUENCE_URL=https://<сайт>.atlassian.net/wiki CONFLUENCE_SPACE=<ключ> \\
  CONFLUENCE_USER=<почта> CONFLUENCE_TOKEN=<токен> python scripts/confluence_upload.py gde-napisano-confluence.zip
Вручную: создать страницу, в редакторе вставить содержимое html/<страница>.html.
Подробно: docs/confluence.md
"""


def build_zip(db, boxes=None) -> bytes:
    data = collect(db, boxes)
    pages = [{"title": ROOT_TITLE, "parent": None, "storage": "storage/00-index.xml", "html": "html/index.html"}]
    files = {"storage/00-index.xml": storage_root(data), "html/index.html": html_root(data)}
    for d in data["documents"]:
        pages.append({"title": d["title"], "parent": ROOT_TITLE, "storage": f"storage/{d['slug']}.xml",
                      "html": f"html/{d['slug']}.html", "doc_id": d["doc_id"]})
        files[f"storage/{d['slug']}.xml"] = storage_doc(d, data)
        files[f"html/{d['slug']}.html"] = html_doc(d, data)
    pages.append({"title": FAQ_TITLE, "parent": ROOT_TITLE, "storage": "storage/99-faq.xml", "html": "html/faq.html"})
    files["storage/99-faq.xml"] = storage_faq(data)
    files["html/faq.html"] = html_faq(data)
    manifest = {"format": "confluence-storage", "version": 1, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "pages": pages}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
        z.writestr("README.txt", README)
        for name, body in files.items():
            z.writestr(name, body)
    return buf.getvalue()
