"""Ящички (направления документов), ответственные и маршрутизатор вопросов."""
from __future__ import annotations

import csv
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from app.text import tokens

log = logging.getLogger(__name__)

GENERAL_BOX = "general"


@dataclass
class Owner:
    role: str
    full_name: str
    contact: str
    topics: str
    notes: str = ""
    is_example: bool = False
    topic_stems: set[str] = field(default_factory=set)

    def as_dict(self) -> dict:
        return {"role": self.role, "full_name": self.full_name, "contact": self.contact,
                "notes": self.notes, "is_example": self.is_example}


@dataclass
class Box:
    box_id: str
    name: str
    owners: list[Owner] = field(default_factory=list)
    description: str = ""
    stems: set[str] = field(default_factory=set)
    # Регулярные выражения: по имени файла документа (doc_patterns) и по упоминанию в вопросе (aliases)
    doc_patterns: list[re.Pattern] = field(default_factory=list)
    aliases: list[re.Pattern] = field(default_factory=list)


@dataclass
class Route:
    boxes: list[str]
    scores: dict[str, float]
    method: str
    need_clarification: bool = False
    clarifying_question: str | None = None


class BoxDirectory:
    def __init__(self, boxes: dict[str, Box]) -> None:
        self.boxes = boxes
        self._box_vecs: np.ndarray | None = None
        self._box_ids: list[str] = list(boxes)
        # слова, которые встречаются в темах трёх и более ящичков, ничего не говорят о направлении
        df = Counter(s for b in boxes.values() for s in b.stems)
        self.common = {s for s, n in df.items() if n >= 3}

    @classmethod
    def load(cls, path: Path) -> BoxDirectory:
        boxes: dict[str, Box] = {}
        if not path.exists():
            log.warning("Справочник ящичков %s не найден — поиск идёт по всем документам", path)
            return cls(boxes)
        with path.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                bid = (row.get("box_id") or "").strip()
                if not bid:
                    continue
                box = boxes.setdefault(bid, Box(bid, (row.get("box_name") or bid).strip()))
                topics = row.get("topics") or ""
                owner = Owner(
                    role=(row.get("role") or "").strip(),
                    full_name=(row.get("full_name") or "").strip(),
                    contact=(row.get("contact") or "").strip(),
                    topics=topics,
                    notes=(row.get("notes") or "").strip(),
                    is_example=(row.get("is_fictional_example") or "").strip().lower().startswith("да"),
                    topic_stems=set(tokens(topics)),
                )
                box.owners.append(owner)
                for col, target in (("doc_patterns", box.doc_patterns), ("aliases", box.aliases)):
                    for pat in (row.get(col) or "").split("|"):
                        pat = pat.strip()
                        if pat and pat not in [x.pattern for x in target]:
                            try:
                                target.append(re.compile(pat, re.I))
                            except re.error:
                                log.warning("Ящичек %s: неверное выражение в %s: %s", bid, col, pat)
                box.stems |= owner.topic_stems | set(tokens(box.name)) | set(tokens(row.get("typical_docs") or ""))
                box.description += f" {topics}; {row.get('typical_docs') or ''}"
        for b in boxes.values():
            b.description = f"{b.name}: {b.description.strip()}"
        return cls(boxes)

    def ids(self) -> list[str]:
        return list(self.boxes)

    def box_for_doc(self, rel_path: str, title: str = "") -> str | None:
        """Ящичек по имени файла или заголовку документа (столбец doc_patterns)."""
        name = f"{rel_path} {title}".replace("_", " ")
        for bid, box in self.boxes.items():
            if any(p.search(name) for p in box.doc_patterns):
                return bid
        return None

    def mentioned(self, text: str) -> list[str]:
        """Ящички, явно названные в тексте вопроса (столбец aliases), в порядке упоминания."""
        hits = []
        for bid, box in self.boxes.items():
            pos = [m.start() for p in box.aliases for m in [p.search(text or "")] if m]
            if pos:
                hits.append((min(pos), bid))
        return [b for _, b in sorted(hits)]

    def name(self, box_id: str) -> str:
        if box_id == GENERAL_BOX:
            return "Общие документы"
        b = self.boxes.get(box_id)
        return b.name if b else box_id

    def owner_for(self, box_id: str, question: str = "") -> Owner | None:
        box = self.boxes.get(box_id)
        if not box or not box.owners:
            return None
        q = set(tokens(question)) - self.common
        best = max(box.owners, key=lambda o: len(q & o.topic_stems))
        return best if q & best.topic_stems else box.owners[0]

    # --- маршрутизация -------------------------------------------------------------------
    def rule_scores(self, text: str) -> dict[str, float]:
        q = set(tokens(text)) - self.common
        out = {}
        for bid, box in self.boxes.items():
            hits = q & box.stems
            if hits:
                out[bid] = float(len(hits))
        return out

    def guess_box(self, text: str) -> str:
        scores = self.rule_scores(text)
        if not scores:
            return GENERAL_BOX
        best = max(scores, key=scores.get)
        return best if scores[best] >= 2 else GENERAL_BOX

    def _ensure_vecs(self, embedder) -> None:
        if self._box_vecs is None and self.boxes:
            self._box_vecs = embedder.embed_passages([self.boxes[b].description for b in self._box_ids])

    def route(self, question: str, q_vec: np.ndarray | None, embedder, settings, llm=None) -> Route:
        if not self.boxes:
            return Route([], {}, "none")
        if settings.router_use_llm and llm is not None:
            r = self._route_llm(question, llm)
            if r is not None:
                return r
        rules = self.rule_scores(question)
        if rules:
            top = max(rules.values())
            chosen = sorted((b for b, s in rules.items() if s >= max(1.0, top * 0.5)), key=lambda b: -rules[b])[:3]
            return Route(chosen, rules, "rules")
        if q_vec is not None and embedder is not None:
            self._ensure_vecs(embedder)
            sims = self._box_vecs @ q_vec
            order = np.argsort(-sims)
            scores = {self._box_ids[i]: float(sims[i]) for i in order[:3]}
            best = float(sims[order[0]])
            if best >= settings.router_min_similarity:
                chosen = [self._box_ids[i] for i in order[:2] if sims[i] >= best - 0.01]
                return Route(chosen, scores, "embeddings")
            return Route([], scores, "embeddings-low")
        return Route([], {}, "none")

    def _route_llm(self, question: str, llm) -> Route | None:
        from app.llm.prompts import ROUTER_SYSTEM, router_user

        boxes_json = json.dumps(
            [{"box_id": b.box_id, "name": b.name, "topics": b.description[:300]} for b in self.boxes.values()],
            ensure_ascii=False,
        )
        try:
            res = llm.chat(ROUTER_SYSTEM, router_user(boxes_json, question), json_mode=True)
            data = json.loads(re.search(r"\{.*\}", res.text, re.S).group(0))
        except Exception as e:  # маршрутизатор необязателен — откатываемся на правила
            log.warning("LLM-маршрутизатор не сработал: %s", e)
            return None
        ids = [b for b in data.get("box_ids") or [] if b in self.boxes]
        return Route(ids, {b: 1.0 for b in ids}, "llm", bool(data.get("need_clarification")),
                     data.get("clarifying_question"))
