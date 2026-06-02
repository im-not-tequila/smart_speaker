"""
Распознавание речи (STT) через sherpa-onnx.
"""

import sherpa_onnx

from settings import AUDIO_SAMPLE_RATE, STT_MODEL_PATH, STT_NUM_THREADS, STT_PROVIDER


def create_recognizer():
    """Создаёт online-распознаватель для стримингового распознавания."""
    return sherpa_onnx.OnlineRecognizer.from_t_one_ctc(
        tokens=str(STT_MODEL_PATH / "tokens.txt"),
        model=str(STT_MODEL_PATH / "model.onnx"),
        num_threads=STT_NUM_THREADS,
        sample_rate=AUDIO_SAMPLE_RATE,
        provider=STT_PROVIDER,
    )
