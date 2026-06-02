"""
Нормализация поискового запроса через Gemini.

Транслитерированные названия (кириллица) → оригинальное написание:
  «линкин парк ин зе енд» → «Linkin Park In The End»
  «атл я забил»           → «АТЛ Я Забил»

При ошибке API или отсутствии ключа — возвращает исходный запрос.
"""

import logging

import google.generativeai as genai

import settings

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "Ты помощник, который преобразует транслитерированные названия песен "
    "и исполнителей из кириллицы в оригинальное написание.\n"
    "Правила:\n"
    "— Если название изначально на русском (русский исполнитель / русская песня), "
    "оставь на русском, но исправь регистр.\n"
    "— Если название транслитерировано с английского или другого языка, "
    "верни его в оригинальном написании на латинице.\n"
    "— Верни ТОЛЬКО исправленное название, без пояснений и кавычек."
)

_model: genai.GenerativeModel | None = None


def _get_model() -> genai.GenerativeModel | None:
    global _model
    if _model is not None:
        return _model

    api_key = settings.GEMINI_API_KEY
    if not api_key:
        logger.warning("GEMINI_API_KEY не задан — нормализация запросов отключена")
        return None

    genai.configure(api_key=api_key)
    _model = genai.GenerativeModel(
        model_name=settings.GEMINI_MODEL_NAME,
        generation_config={
            "temperature": 0.1,
            "top_p": 0.95,
            "top_k": 0,
            "max_output_tokens": 1024,
        },
        system_instruction=_SYSTEM_PROMPT,
    )
    return _model


def normalize_query(query: str) -> str:
    """
    Пропускает запрос через Gemini для нормализации.
    При любой ошибке возвращает оригинальный запрос.
    """
    model = _get_model()
    if model is None:
        return query

    try:
        response = model.generate_content(query)
        normalized = response.text.strip()
        if normalized:
            logger.info("query normalized: %r → %r", query, normalized)
            return normalized
    except Exception as e:
        logger.warning("Gemini normalize failed: %s", e)

    return query
