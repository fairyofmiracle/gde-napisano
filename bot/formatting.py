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


def format_answer(data: dict) -> str:
    mode = data.get("mode")
    parts: list[str] = [str(data.get("text") or "").strip()]
    if mode == "confident":
        if data.get("advice"):
            parts.append(f"Что делать: {data['advice']}")
        cites = data.get("citations") or []
        if cites:
            lines = ["Источники:"]
            for c in cites:
                lines.append(f"[{c.get('n', '')}] {c.get('ref', '')}\n«{_cut(c.get('quote', ''), _QUOTE_MAX)}»")
            parts.append("\n".join(lines))
    elif mode == "uncertain":
        variants = data.get("variants") or []
        if variants:
            lines = ["Возможные пункты:"]
            for i, v in enumerate(variants, 1):
                lines.append(f"{i}. {v.get('ref', '')}\n{_cut(v.get('text', ''), _VARIANT_MAX)}")
            parts.append("\n".join(lines))
        parts.append(_owner_line(data.get("owner")))
    elif mode == "refused":
        # Только вежливый отказ: без источников, вариантов и ответственного.
        return parts[0]
    else:
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
