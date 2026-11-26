"""Фильтр нецензурной лексики для вопросов пользователей (русский и английский).

Принцип: текст приводится к нормальному виду (регистр, ё/е, невидимые символы, латиница, похожая на
кириллицу, разделители внутри слова, растянутые буквы, маскировка звёздочками), после чего каждое слово
проверяется регулярными выражениями. Корни, которые не встречаются в обычных словах, ищутся в любом
месте слова, а остальные («хуй», «говн», «еб», «сук», «жоп», «хер»), только в начале слова после
допустимой приставки и, где нужно, с ограниченным набором окончаний. Так «хлеб», «страхование», «оскорбление»,
«истреблять», «рубль», «суббота», «бляха» и «Херсон» остаются без блокировки.

Известные ограничения: слово, разбитое пробелами на части по две буквы («ху й»), не находится;
сочетания, написанные в чужой раскладке, не распознаются; редкие законные слова с совпавшим корнем
(например «сучка» как мн. число от «сучок») блокируются. Для таких слов есть список исключений
PROFANITY_ALLOWLIST.

Текст вопроса сюда только передаётся на проверку: он нигде не сохраняется и не пишется в лог.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

REFUSAL_TEXT = (
    "Не могу отвечать на запросы с нецензурной лексикой. "
    "Переформулируйте вопрос без грубых выражений, и я поищу ответ в документах."
)

_MASK = "*#@$%"
_NOISE = re.compile(r"https?://\S+|www\.\S+|\S+@\S+\.\S+")
_TOKEN = re.compile(r"(?:[^\W_]|[*#@$%])+")
_JOIN = re.compile(r"(?<=[^\W\d_])[.\-_·•~|/\\'’]+(?=[^\W\d_])")
_REPEAT = re.compile(r"([^\W\d_*#@$%])\1+")

_UA = str.maketrans({"і": "и", "ї": "и", "є": "е", "ґ": "г", "ъ": None, "ь": None})
# Латиница, похожая на кириллицу (b разбирается отдельно: б или в)
_LAT2CYR = str.maketrans({
    "a": "а", "c": "с", "e": "е", "h": "н", "k": "к", "m": "м", "o": "о", "p": "р", "t": "т",
    "x": "х", "y": "у", "u": "и", "i": "и", "n": "п", "r": "г", "0": "о", "3": "з", "6": "б", "1": "и",
})
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s"})

# --- русские корни ---------------------------------------------------------------------------------
# Всё записано без «ь» и «ъ», с «е» вместо «ё» (слова приводятся к такому же виду).
_PREF_EB = r"(?:за|по|на|при|до|от|об|под|пере|вы|у|раз|рас|ис|из|вз|над|недо|про)?"
_PREF_ZH = r"(?:за|по|на|в|у|с|вы|от|под|пере|раз)?"
_PREF_SR = r"(?:за|по|на|вы|об|от|рас|вс|с|ис|пере|при|до)?"

_PREF_ANY = r"(?:(?:за|по|на|при|до|от|об|обо|под|пере|вы|раз|рас|ис|из|вз|над|недо|про|ни)){0,2}"
_PREF_HU = r"(?:(?:за|по|на|при|до|от|об|под|пере|вы|у|раз|рас|ис|из|вз|над|недо|про|ни|о|а|ох|ах)){0,2}"

_RU = [re.compile(p) for p in (
    # в любом месте слова: таких сочетаний нет в обычных словах
    r"пизд|пизж|пезд|залуп|бляд|долб[ао]еб|шлюх|муд(?:ак|озвон)",
    # с начала слова после приставок («страхует», «Олеговна», «Лондон» сюда не попадают)
    rf"^{_PREF_HU}ху(?:[йяе]|и(?:т|н|$))",
    rf"^{_PREF_ANY}(?:г[ао]вн|г[ао]ндон|дроч|муд(?:ач|ил))",
    # только с начала слова
    r"^ублюд",
    r"^(?:на|по|за|вы)?пид(?:ор|ар|рил)",
    r"^блят",
    r"^(?:бля|блеа|блеат)$",
    r"^п[зж]дц?$",
    rf"^{_PREF_EB}еб(?:а[тлнш]|ак|ац|л[оаяеиыу]|у[чнт]|ну|е[тшм]|ыр|тв)",
    rf"^{_PREF_EB}(?:еби(?:с|те|т)?|ебу)$",
    r"^себ(?:ат|ал|ис)",  # съебать, сьебись
    r"^(?:сука|суки|суке|суку|сукой|сукою|сукам|суками|сукин\w*)$",
    r"^сучк[аиуе]$|^сучар",
    rf"^{_PREF_ZH}жоп(?:а|у|ы|е|ой|ою|ами|ах|ам)$|^{_PREF_ZH}жоп(?:о|а)(?:голов|лиз|шник)",
    rf"^{_PREF_SR}(?:срат(?:ся)?|срал[аио]?|срет|срут|сре[шм]|сран(?:ы|ая|ое|ые|ь|ец))$",
    r"^(?:на|по)?хер(?:а|у|ом|е|ня|ню|ни|ней|ов\w*)?$",
    r"^нах$",
    r"^(?:манда|манду|манды|мандой|манде|мандавош\w*|мандюк\w*)$",
)]

# --- английские слова и русская транслитерация -------------------------------------------------------
_EN = [re.compile(p) for p in (
    r"^(?:mother)?f(?:u+c+k|u+k|c+k|c+u+k)",
    r"^(?:bull|horse|dip|dumb|chicken|bat)?sh+i+t(?:$|s$|ty$|head|face|hole|show|ting$|ted$|ter$|load|storm|bag)",
    r"^bitch(?:es|y|ing|ed)?$",
    r"^cunts?$",
    r"^as+holes?$",
    r"^dickheads?$",
    r"^bastards?$",
    r"^whores?$",
    r"^slut(?:s|ty)?$",
    r"^(?:wtf|stfu)$",
    r"^nigg(?:er|a)s?$",
    r"^faggots?$",
    # русский мат латиницей
    r"pizd|pezd",
    r"^bl[yji]+a[td]\w*",
    r"^bl[yj]a$",
    r"^(?:suka|cyka|syka)$",
    r"^(?:xu[yj]|xyu|xui)(?:nya|nyu|ni|ne|ev\w*|ov\w*|lo|la)?$",
    r"^(?:na|po|za|ni)(?:hu[yji]|xu[yji])\w*",
    r"^(?:o|a)hue(?:t|l|nn)",
    r"^hu[yj](?:nya|nyu|ni|ne|ev\w*|lo)$",
    r"^(?:za|na|po|do|vy|u|raz|pro|ot|pri|ob)?y?eb(?:at|al|an(?:at|ut|yj|iy|yy)|uch|lan|lo)",
    r"^mud(?:ak|ozvon)|^mudil(?:a|o|ov)?$",
    r"^(?:na|po|za)?pid[oa]r(?:as|y|ov|u|a)?",
    r"^g[ao]ndon|^zalup|^g[ao]vno|^shlyu[hx]|^dolb[oa]y?eb|^ublyud",
    r"^zhop(?:a|u|y|oy|e)$",
)]

# Основы для проверки слов с маскировкой («п*зда», «бл@дь», «f*ck»)
_RU_STEMS = ("пизд", "пезд", "бляд", "блят", "ебат", "ебан", "ебал", "ебуч", "ебну", "хуй", "хуе", "хуя",
             "сука", "жопа", "говн", "мудак", "мудил", "залуп", "пидор", "пидар", "дроч", "шлюх", "гандон")
_EN_STEMS = ("fuck", "shit", "bitch", "cunt", "whore", "slut", "asshole", "bastard", "pizd", "blyat", "blyad")


_PHRASES = re.compile(r"(?<![^\W\d_])еб\s+(?:твою|вашу)(?![^\W\d_])")


def _prepare(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    t = "".join(c for c in t if unicodedata.category(c) not in ("Mn", "Cf"))
    t = t.lower().replace("ё", "е")
    return _NOISE.sub(" ", t)


def _tokens(text: str) -> list[str]:
    out = []
    for raw in _TOKEN.findall(text):
        tok = raw.strip(_MASK)
        if tok and any(ch.isalpha() for ch in tok):
            out.append(tok)
    return out


def _candidates(text: str) -> list[str]:
    """Слова текста плюс склейки: «х.у.й», «х-у-й», «х у й», «пи_зда»."""
    words = _tokens(text)
    found = list(words)
    found += _tokens(_JOIN.sub("", text))
    run: list[str] = []
    for w in words + [""]:
        if len(w) == 1 and w.isalpha():
            run.append(w)
            continue
        if len(run) >= 3:
            found.append("".join(run))
        run = []
    return found


def _is_cyr(ch: str) -> bool:
    return "а" <= ch <= "я"


def _wild(v: str, stems: tuple[str, ...]) -> bool:
    for s in stems:
        n = len(s)
        head = v[:n]
        if len(head) < n:
            continue
        wild = sum(c in _MASK for c in head)
        if not wild or (n - wild) * 2 < n:
            continue
        if all(c == sc or c in _MASK for c, sc in zip(head, s)) and (n > 3 or len(v) - n <= 4):
            return True
    return False


def _variants(tok: str) -> list[tuple[str, bool]]:
    """Возможные прочтения слова: (текст, это кириллица)."""
    tok = tok.translate(_UA)
    if any(_is_cyr(c) for c in tok):
        if "b" in tok:
            vs = [tok.replace("b", "б").translate(_LAT2CYR), tok.replace("b", "в").translate(_LAT2CYR)]
        else:
            vs = [tok.translate(_LAT2CYR)]
        return [(v, True) for v in dict.fromkeys(vs)]
    return [(tok.translate(_LEET), False)]


def _word_is_obscene(tok: str) -> bool:
    for v, cyr in _variants(tok):
        for form in dict.fromkeys((v, _REPEAT.sub(r"\1", v))):
            if cyr:
                if any(p.search(form) for p in _RU) or _wild(form, _RU_STEMS):
                    return True
            elif any(p.search(form) for p in _EN) or _wild(form, _EN_STEMS):
                return True
    return False


def contains_profanity(text: str, allow: frozenset[str] | set[str] = frozenset()) -> bool:
    """True, если в тексте есть русский или английский мат. `allow` — слова-исключения (в нижнем регистре)."""
    prepared = _prepare(text)
    if _PHRASES.search(prepared):
        return True
    for tok in _candidates(prepared):
        if tok in allow:
            continue
        if _word_is_obscene(tok):
            return True
    return False


@dataclass
class ProfanityFilter:
    enabled: bool = True
    allow: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_settings(cls, settings) -> "ProfanityFilter":
        words = {w.strip().lower().replace("ё", "е") for w in settings.profanity_allowlist.split(",") if w.strip()}
        return cls(bool(settings.profanity_filter), frozenset(words))

    def check(self, text: str) -> bool:
        """True — текст нужно отклонить."""
        return self.enabled and contains_profanity(text, self.allow)
