"""Voice-to-note.

The invariant under test: audio-derived text never reaches a cloud model. Voice
memos are the most personal thing Loop handles, so they are ``local_only``
unconditionally rather than by configuration.
"""

from __future__ import annotations

import sys

import pytest

from integrations.transcribe import INSTALL_HINT, FasterWhisperTranscriber, Transcript


class FakeTranscriber:
    """Stand-in for a working local Whisper."""

    def __init__(self, text: str = "remember to call the plumber") -> None:
        self.text = text
        self.calls: list = []

    def available(self) -> bool:
        return True

    def transcribe(self, audio_path):
        self.calls.append(audio_path)
        return Transcript(text=self.text, language="en", duration_seconds=3.2)


class RecordingRouter:
    """Records the privacy metadata of every routed call."""

    def __init__(self) -> None:
        self.metadata: list[dict] = []

    def route(self, prompt, context_metadata=None, **kwargs):
        self.metadata.append(context_metadata or {})
        return '{"title": "Call the plumber", "tags": ["home"], "links": [], ' \
               '"body": "Remember to call the plumber."}'


@pytest.fixture
def audio_file(tmp_path):
    path = tmp_path / "memo.ogg"
    path.write_bytes(b"not really audio")
    return path


# --------------------------------------------------------------------------- #
# Optional dependency handling
# --------------------------------------------------------------------------- #
def test_reports_unavailable_when_the_package_is_missing(settings, monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    assert FasterWhisperTranscriber(settings).available() is False


def test_transcribe_without_the_package_raises_a_clear_error(settings, monkeypatch,
                                                             audio_file):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)

    with pytest.raises(RuntimeError) as excinfo:
        FasterWhisperTranscriber(settings).transcribe(audio_file)

    assert "faster-whisper" in str(excinfo.value)
    assert "voice" in str(excinfo.value)


def test_install_hint_names_the_extra():
    assert '.[voice]' in INSTALL_HINT


def test_missing_audio_file_raises_file_not_found(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(FasterWhisperTranscriber, "available", lambda self: True)

    with pytest.raises(FileNotFoundError):
        FasterWhisperTranscriber(settings).transcribe(tmp_path / "nope.ogg")


def test_transcript_is_falsy_when_empty():
    assert not Transcript(text="   ")
    assert Transcript(text="something")


# --------------------------------------------------------------------------- #
# The privacy invariant
# --------------------------------------------------------------------------- #
def test_note_from_audio_routes_local_only(settings, audio_file):
    """Every LLM call made while processing audio must be local-only."""
    from specialists.knowledge import KnowledgeSpecialist

    router = RecordingRouter()
    spec = KnowledgeSpecialist(settings, router=router, vector_store=object(),
                               transcriber=FakeTranscriber())

    spec.note_from_audio(audio_file, write=False)

    assert router.metadata, "expected the note to be formatted via the router"
    assert all(m.get("local_only") is True for m in router.metadata)
    assert all(m.get("source") == "obsidian_private" for m in router.metadata)


def test_note_from_audio_returns_the_transcribed_text(settings, audio_file):
    from specialists.knowledge import KnowledgeSpecialist

    spec = KnowledgeSpecialist(settings, router=RecordingRouter(),
                               vector_store=object(),
                               transcriber=FakeTranscriber("call the plumber"))

    draft = spec.note_from_audio(audio_file, write=False)

    assert draft.title == "Call the plumber"


def test_empty_transcript_raises_rather_than_writing_an_empty_note(settings, audio_file):
    from specialists.knowledge import KnowledgeSpecialist

    spec = KnowledgeSpecialist(settings, router=RecordingRouter(),
                               vector_store=object(),
                               transcriber=FakeTranscriber(text="   "))

    with pytest.raises(ValueError):
        spec.note_from_audio(audio_file, write=False)


def test_unavailable_transcriber_raises_the_install_hint(settings, audio_file):
    from specialists.knowledge import KnowledgeSpecialist

    class Unavailable:
        def available(self) -> bool:
            return False

        def transcribe(self, path):  # pragma: no cover - never reached
            raise AssertionError("should not be called")

    spec = KnowledgeSpecialist(settings, router=RecordingRouter(),
                               vector_store=object(), transcriber=Unavailable())

    with pytest.raises(RuntimeError, match="faster-whisper"):
        spec.note_from_audio(audio_file, write=False)


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #
def test_note_is_written_to_the_private_vault_when_configured(tmp_path, audio_file):
    from config.settings import Settings
    from specialists.knowledge import KnowledgeSpecialist

    private = tmp_path / "private-vault"
    private.mkdir()
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'loop.db'}",
        obsidian_vault_path=str(tmp_path / "main-vault"),
        obsidian_private_vault_path=str(private),
    )
    spec = KnowledgeSpecialist(settings, router=RecordingRouter(),
                               vector_store=object(), transcriber=FakeTranscriber())

    draft = spec.note_from_audio(audio_file)

    written = list(private.rglob("*.md"))
    assert written, "expected the note under the private vault"
    assert draft.title in written[0].read_text()


def test_falls_back_to_the_main_vault_when_no_private_vault(tmp_path, audio_file):
    from config.settings import Settings
    from specialists.knowledge import KnowledgeSpecialist

    main = tmp_path / "main-vault"
    main.mkdir()
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'loop.db'}",
        obsidian_vault_path=str(main),
        obsidian_private_vault_path="",
    )
    spec = KnowledgeSpecialist(settings, router=RecordingRouter(),
                               vector_store=object(), transcriber=FakeTranscriber())

    spec.note_from_audio(audio_file)

    assert list(main.rglob("*.md"))


# --------------------------------------------------------------------------- #
# Telegram voice handler
# --------------------------------------------------------------------------- #
class FakeTelegramFile:
    def __init__(self, payload: bytes = b"audio") -> None:
        self.payload = payload
        self.downloaded_to: str | None = None

    async def download_to_drive(self, path: str) -> None:
        self.downloaded_to = path
        from pathlib import Path as _Path

        _Path(path).write_bytes(self.payload)


class FakeBot:
    def __init__(self) -> None:
        self.file = FakeTelegramFile()
        self.requested: list[str] = []

    async def get_file(self, file_id: str) -> FakeTelegramFile:
        self.requested.append(file_id)
        return self.file


async def test_voice_handler_files_a_note(settings):
    from integrations.telegram_bot import handle_voice_message
    from specialists.knowledge import KnowledgeSpecialist

    bot = FakeBot()
    spec = KnowledgeSpecialist(settings, router=RecordingRouter(),
                               vector_store=object(), transcriber=FakeTranscriber())

    draft = await handle_voice_message(bot, "FILE123", spec)

    assert bot.requested == ["FILE123"]
    assert draft.title == "Call the plumber"


async def test_voice_handler_deletes_the_recording(settings):
    """Loop keeps the note, not the audio."""
    from pathlib import Path

    from integrations.telegram_bot import handle_voice_message
    from specialists.knowledge import KnowledgeSpecialist

    bot = FakeBot()
    spec = KnowledgeSpecialist(settings, router=RecordingRouter(),
                               vector_store=object(), transcriber=FakeTranscriber())

    await handle_voice_message(bot, "FILE123", spec)

    assert not Path(bot.file.downloaded_to).exists()


async def test_voice_handler_cleans_up_even_on_failure(settings):
    from pathlib import Path

    from integrations.telegram_bot import handle_voice_message
    from specialists.knowledge import KnowledgeSpecialist

    bot = FakeBot()
    spec = KnowledgeSpecialist(settings, router=RecordingRouter(),
                               vector_store=object(),
                               transcriber=FakeTranscriber(text="  "))

    with pytest.raises(ValueError):
        await handle_voice_message(bot, "FILE123", spec)

    assert not Path(bot.file.downloaded_to).exists()
