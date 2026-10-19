"""Tokenization shared by every index.

Memory and chats mix Kazakh, Russian and English, and the same name is written
in Cyrillic one day and in Latin the next ("алма парк" / "alma-park"). Every
token is transliterated into one rough Latin skeleton and cut to a prefix, so
inflected Kazakh forms ("тіркелуді", "тіркелу") meet on the same stem.
"""
import re

_CYR = {
    "а": "a", "ә": "a", "б": "b", "в": "v", "г": "g", "ғ": "g", "д": "d",
    "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k",
    "қ": "k", "л": "l", "м": "m", "н": "n", "ң": "n", "о": "o", "ө": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ұ": "u", "ү": "u",
    "ф": "f", "х": "h", "һ": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sh",
    "ъ": "", "ы": "y", "і": "i", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}
# Latin letters that Kazakh and Russian spell with a different Cyrillic one.
_LAT = {"q": "k", "c": "k", "x": "ks", "j": "zh", "w": "u"}

STEM_LEN = 6
MIN_LEN = 3

_STOP_RAW = """
мен және бұл осы сол не да де ма ме ба бе па пе ғой бар жоқ керек еді екен
қалай қашан қайда неге кім маған саған оған біз сіз олар ол сен мына ана
жаса жасап жасашы істе істеп қыл қылып болса болды бол үшін деп дейін
кейін енді тағы әлі бір екі қазір просто сосын потом
жалпы емес басқа басқалар әртүрлі түрлі пайдалан қолдан етіп ету етеді
жатыр жатырмын жатырмыз үшін ушын ушында сондықтан себебі өйткені
барлық бәрі барлығы кез келген сияқты сықылды тек ғана әрі да бірақ
мысалы яғни солай осылай мынау анау жақсы жаман дұрыс болады болмайды
айт айтшы айтып көр көрші қара қарашы біл білу істеу жасау дегенмен
и в во на не что как это так но да нет или по из за для от до же ли бы
мне меня ты вы мы они он она его ее сделай сделать нужно надо можно
все всё весь вообще тоже ещё еще уже очень только чтобы потому если когда
the a an to of and or is are be in on for it this that with do can you
please make just now then also
"""


def _fold(text: str) -> str:
    out = []
    for ch in text.lower():
        if ch in _CYR:
            out.append(_CYR[ch])
        elif ch in _LAT:
            out.append(_LAT[ch])
        else:
            out.append(ch)
    return "".join(out)


def _stem(tok: str) -> str:
    return tok[:STEM_LEN]


STOP = {_stem(_fold(w)) for w in _STOP_RAW.split()}


def tokens(text: str) -> list[str]:
    out = []
    for raw in re.split(r"[^a-z0-9]+", _fold(text)):
        if len(raw) < MIN_LEN and not raw.isdigit():
            continue
        tok = _stem(raw)
        if tok in STOP:
            continue
        out.append(tok)
    return out
