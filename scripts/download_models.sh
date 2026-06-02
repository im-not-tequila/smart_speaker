#!/usr/bin/env bash
# Скачивает STT-модель T-one для sherpa-onnx (~138 MB).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_DIR="$ROOT/models/sherpa-onnx-t-one-ru"
ARCHIVE_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-t-one-russian-2025-09-08.tar.bz2"
ARCHIVE_NAME="sherpa-onnx-streaming-t-one-russian-2025-09-08.tar.bz2"
EXTRACTED_DIR="sherpa-onnx-streaming-t-one-russian-2025-09-08"

if [[ -f "$MODEL_DIR/model.onnx" && -f "$MODEL_DIR/tokens.txt" ]]; then
    echo "Модель уже установлена: $MODEL_DIR"
    ls -lh "$MODEL_DIR/model.onnx" "$MODEL_DIR/tokens.txt"
    exit 0
fi

if ! command -v wget >/dev/null 2>&1; then
    echo "Ошибка: нужен wget (sudo apt install wget)" >&2
    exit 1
fi

mkdir -p "$MODEL_DIR"
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

echo "Скачиваю T-one Russian (~138 MB)..."
wget -q --show-progress "$ARCHIVE_URL" -O "$tmpdir/$ARCHIVE_NAME"

echo "Распаковка..."
tar -xf "$tmpdir/$ARCHIVE_NAME" -C "$tmpdir"

if [[ ! -d "$tmpdir/$EXTRACTED_DIR" ]]; then
    echo "Ошибка: в архиве нет каталога $EXTRACTED_DIR" >&2
    exit 1
fi

cp -a "$tmpdir/$EXTRACTED_DIR"/. "$MODEL_DIR"/

echo "Готово:"
ls -lh "$MODEL_DIR/model.onnx" "$MODEL_DIR/tokens.txt"
