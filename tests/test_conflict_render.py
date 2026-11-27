"""Сборка текста при противоречии: номер заявки, формулировки, сокращения, без vs."""
from bot.formatting import format_answer


def test_format_conflict_keeps_ticket_on_own_line_and_glossary():
    text = format_answer({
        "mode": "confident",
        "notice": ("Пункты регламента расходятся, поэтому однозначно ответить нельзя. "
                   "Вопрос передан в службу качества.\nНомер заявки: ГН-10."),
        "text": ("П. 5.2.2 говорит: «Приоритет не меняется» [1]. "
                 "П. 5.2.3 говорит: «ПА может понизить приоритет» [2]. "
                 "Суть расхождения: приоритет не меняется или может быть понижен."),
        "advice": "Какое правило действует, решает служба качества.",
        "citations": [
            {"n": 1, "ref": "Пресейловая деятельность Дирекции ИБ, п. 5.2.2",
             "quote": "Приоритет не меняется", "link": "/doc/x?p=a"},
            {"n": 2, "ref": "Пресейловая деятельность Дирекции ИБ, п. 5.2.3",
             "quote": "ПА может понизить приоритет", "link": "/doc/x?p=b"},
        ],
        "glossary": [
            {"abbr": "SLA", "full": "Service Level Agreement"},
            {"abbr": "ПА", "full": "Пресейл-архитектор"},
            {"abbr": "SS", "full": "Solution Sales"},
        ],
        "escalation": {"id": 10, "to": "quality-test@example.local",
                       "cc": "employee-test@example.local", "delivery": "outbox"},
    }, base_url="http://example.local")
    assert "Что написано в документах" not in text
    assert "передал" not in text
    assert "vs" not in text.lower()
    assert "[object" not in text
    assert "\nНомер заявки: ГН-10." in text
    assert "SLA — Service Level Agreement" in text
    assert "ПА — Пресейл-архитектор" in text
    assert "SS — Solution Sales" in text
    assert "Запрос ГН-10" in text
    assert "Вопрос передан в службу качества" in text
    assert text.count("передан") == 1
