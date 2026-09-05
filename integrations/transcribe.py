"""Voice transcription — local only, by construction.

Voice memos are the most personal content Loop touches, and the user cannot
realistically review each one before it is processed. So audio is treated as
``local_only`` unconditionally: there is no cloud transcriber behind this
interface, and adding one would breach the privacy gate's central promise.

:class:`FasterWhisperTranscriber` runs `faster-whisper
<https://github.com/SYSTRAN/faster-whisper>`_ on the CPU by default. The package
is an **optional extra** (``pip install -e ".[voice]"``) so the base install and
the Docker image stay light; when it is missing, :meth:`Transcriber.available`
returns ``False`` and callers report the feature as unavailable rather than
raising a traceback at the user.
"""

from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

INSTALL_HINT = 'faster-whisper is not installed. Run: pip install -e ".[voice]"'


@dataclass
class Transcript:
    """The result of transcribing one audio file."""

    text: str
    language: str = ""
    duration_seconds: float = 0.0

    def __bool__(self) -> bool:
        return bool(self.text.strip())


@runtime_checkable
class Transcriber(Protocol):
    """Anything that can turn an audio file into text, locally."""

    def available(self) -> bool:
        """True when this transcriber can actually run."""
        ...

    def transcribe(self, audio_path: Path) -> Transcript:
        """Transcribe ``audio_path``. Raises RuntimeError when unavailable."""
        ...


class FasterWhisperTranscriber:
    """Local Whisper transcription via CTranslate2. Audio never leaves the machine."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._model: Any | None = None

    def available(self) -> bool:
        """True when the optional ``faster-whisper`` package is importable."""
        try:
            return importlib.util.find_spec("faster_whisper") is not None
        except (ImportError, ValueError):
            # ValueError: the module is present in sys.modules but set to None,
            # which is how tests simulate an uninstalled package.
            return False

    def _get_model(self) -> Any:
        """Lazily load the Whisper model (a slow, memory-hungry operation)."""
        if self._model is None:
            if not self.available():
                raise RuntimeError(INSTALL_HINT)
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                self.settings.whisper_model_size,
                device=self.settings.whisper_device,
                compute_type=self.settings.whisper_compute_type,
            )
            logger.info("Loaded local Whisper model %r on %s",
                        self.settings.whisper_model_size, self.settings.whisper_device)
        return self._model

    def transcribe(self, audio_path: Path) -> Transcript:
        """Transcribe an audio file locally."""
        path = Path(audio_path)
        if not self.available():
            raise RuntimeError(INSTALL_HINT)
        if not path.exists():
            raise FileNotFoundError(f"No such audio file: {path}")

        segments, info = self._get_model().transcribe(str(path))
        text = " ".join(segment.text.strip() for segment in segments).strip()

        return Transcript(
            text=text,
            language=getattr(info, "language", "") or "",
            duration_seconds=float(getattr(info, "duration", 0.0) or 0.0),
        )
