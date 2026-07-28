"""Tier-0 гейт: детерминированный, без LLM. Идеи перенесены из легаси ReflexGate."""
import re

_ACK = re.compile(r"^(ок(ей)?|ok|угу|ага|да|нет|спс|спасибо|пон(ятно)?|плюс|\+|лол|ха+|👍|❤️|🔥|\W)+$", re.I)


def _ack_max_len() -> int:
    """PASS 21: порог «шума» по длине — её живой рычаг (0 = по длине не резать)."""
    try:
        import perception
        return int(perception.value("group_ack_max_len"))
    except Exception:
        return 0


def triage(text: str, *, is_private: bool, mentioned: bool = False, replied_to_self: bool = False,
           named: bool = False, media: str = "") -> str:
    """-> 'reply' (точно отвечаем), 'maybe' (решит дешёвый первый проход), 'ignore' (0 токенов).

    named — её имя в тексте (прямое обращение без @, «Пракс?» — не шум, даже короткое).
    media — структурный тег медиа-конверта ('[Изображение]' и т.п.); стикер без текста —
    реакция-шум (остаётся контекстом в буфере), остальное медиа будит наравне с текстом.
    Решение «будить ли группу» принимает раннер по room policy: адрес всегда будит,
    reflective-комната может собрать и отдать Praxis обычный ambient batch."""
    t = (text or "").strip()
    if not t and not media:
        return "ignore"
    if mentioned or replied_to_self or named:
        return "reply"
    if not t:
        if media == "[Стикер]":
            return "ignore"
        return "reply" if is_private else "maybe"
    if is_private:
        return "reply"
    ml = _ack_max_len()
    # Zero disables the whole content classifier. A fixed ACK regex used to keep
    # suppressing speech even after Praxis set the visible threshold to zero.
    if ml > 0 and (_ACK.match(t) or len(t) < ml):
        return "ignore"
    return "maybe"
