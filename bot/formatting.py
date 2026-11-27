"""Ответ docagent в виде обычного текста для Telegram: ответ, источники, ответственный. Без разметки и эмодзи."""
from __future__ import annotations

TG_LIMIT = 4096
_QUOTE_MAX = 400
_VARIANT_MAX = 500


def _cut(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _owner_line(owner: dict | None) -> str:
    if not owner:
        return ""
    who = ", ".join(p for p in (owner.get("role"), owner.get("full_name"), owner.get("contact")) if p)
    return f"Ответственный: {who}" if who else ""


def _link(c: dict, base: str) -> str:
    link = c.get("link") or ""
    return f"{base.rstrip('/')}{link}" if link and base else ""


def _citations(cites: list[dict], base: str) -> str:
    if not cites:
        return ""
    lines = ["Источники:"]
    for c in cites:
        line = f"[{c.get('n', '')}] {c.get('ref', '')}\n«{_cut(c.get('quote', ''), _QUOTE_MAX)}»"
        url = _link(c, base)
        if url:
            line += f"\n{url}"
        lines.append(line)
    return "\n".join(lines)


def _escalation(esc: dict | None) -> str:
    if not esc:
        return ""
    how = "письмо отправлено" if esc.get("delivery") == "smtp" else "письмо сохранено в исходящих"
    return f"Запрос ГН-{esc.get('id')}: {esc.get('to')}, копия {esc.get('cc')} ({how})."


def _glossary(items: list[dict] | None) -> str:
    if not items:
        return ""
    return "Сокращения: " + "; ".join(f"{g['abbr']} — {g['full']}" for g in items)


def format_answer(data: dict, base_url: str = "") -> str:
    mode = data.get("mode")
    parts: list[str] = []
    if data.get("notice"):
        parts.append(str(data["notice"]).strip())
    parts.append(str(data.get("text") or "").strip())
    if mode == "confident":
        if data.get("advice"):
            parts.append(f"Что делать: {data['advice']}")
        parts.append(_citations(data.get("citations") or [], base_url))
        parts += list(data.get("warnings") or [])
        parts.append(_glossary(data.get("glossary")))
        parts.append(_escalation(data.get("escalation")))
    elif mode == "uncertain":
        variants = data.get("variants") or []
        if variants:
            lines = ["Возможные пункты:"]
            for i, v in enumerate(variants, 1):
                lines.append(f"{i}. {v.get('ref', '')}\n{_cut(v.get('text', ''), _VARIANT_MAX)}")
            parts.append("\n".join(lines))
        parts.append(_owner_line(data.get("owner")))
        parts.append(_escalation(data.get("escalation")))
    elif mode == "by_division":
        for sec in data.get("sections") or []:
            block = [sec.get("division_name", "")]
            if sec.get("mode") == "confident":
                block.append(str(sec.get("text") or "").strip())
                block.append(_citations(sec.get("citations") or [], base_url))
            elif sec.get("mode") == "uncertain" and sec.get("variants"):
                block.append("Однозначного пункта нет, ближайший: " + sec["variants"][0].get("ref", ""))
            else:
                block.append("В регламенте этого подразделения ответа нет.")
            parts.append("\n".join(b for b in block if b))
        parts.append(_glossary(data.get("glossary")))
        parts.append(_escalation(data.get("escalation")))
    elif mode == "faq":
        f = data.get("faq") or {}
        parts.append(f"{f.get('label', 'Разъяснение службы качества')} от {f.get('created_at', '')}. "
                     "В самих регламентах этого ответа нет.")
        parts += list(data.get("warnings") or [])
    elif mode in ("refused", "offtopic", "clarify"):
        # Только текст: без источников, вариантов и ответственного.
        return parts[-1]
    else:
        parts += list(data.get("warnings") or [])
        parts.append(_escalation(data.get("escalation")))
        if not data.get("escalation"):
            parts.append(_owner_line(data.get("owner")))
    return "\n\n".join(p for p in parts if p)


def split_message(text: str, limit: int = TG_LIMIT - 96) -> list[str]:
    """Режет длинный текст по абзацам и строкам под лимит Telegram."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    out: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = max(rest.rfind("\n\n", 0, limit), rest.rfind("\n", 0, limit), rest.rfind(" ", 0, limit))
        if cut < limit // 2:
            cut = limit
        out.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        out.append(rest)
    return out
