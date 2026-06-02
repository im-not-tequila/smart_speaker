#!/usr/bin/env python3
"""
Точка входа — голосовой музыкальный ассистент.
Скажите «Алёша включи песню <название>» — ассистент найдёт трек на SoundCloud и включит.
"""

from services.voice.assistant import VoiceAssistant


def main():
    assistant = VoiceAssistant()
    assistant.run()


if __name__ == "__main__":
    main()
