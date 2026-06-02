# Модели

В репозитории хранится только описание. Бинарники (`.onnx` и т.п.) **не коммитятся** — их нужно скачать локально.

## STT (используется приложением)

| Каталог | Модель | Размер |
|---------|--------|--------|
| `sherpa-onnx-t-one-ru/` | [T-one Russian](https://github.com/voicekit-team/T-one) через sherpa-onnx | ~138 MB |

```bash
./scripts/download_models.sh
```

Источник: [sherpa-onnx-streaming-t-one-russian-2025-09-08](https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models)

## Не используются приложением

Каталоги `sherpa-onnx-gigaam-ru/` и `sherpa-onnx-streaming-zipformer-bn-vosk-2026-02-09/` — экспериментальные/запасные модели. Для работы ассистента не нужны; можно удалить локально.
