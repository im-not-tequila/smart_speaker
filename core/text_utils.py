"""
Утилиты для работы с распознанным текстом.
"""


def text_after_wake_word(text: str, wake_words: list[str]) -> str:
    """Возвращает только текст справа от wake word."""
    text_lower = text.lower()
    best_end = 0
    for ww in wake_words:
        pos = text_lower.find(ww.lower())
        while pos != -1:
            end = pos + len(ww)
            if end > best_end:
                best_end = end
            pos = text_lower.find(ww.lower(), pos + 1)
    return text[best_end:].strip()
