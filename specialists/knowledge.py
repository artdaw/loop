"""Knowledge Specialist — Obsidian capture with PARA + Zettelkasten.

Turns raw text (a chat message, a captured thought, a note edit) into a
well-formed Obsidian note: it suggests the correct PARA folder, formats the
content as a Zettelkasten slip (title, tags, links, body), and writes it to the
vault. It also indexes notes into ChromaDB for semantic search.

Privacy: notes from the *private* vault (``local_only=True``) are only ever
processed by the local Ollama model — the LLM router raises ``PrivacyError``
rather than fall back to the cloud.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from config.settings import Settings, get_settings
from core.llm_router import LLMRouter
from core.vector_store import VectorStore
from integrations.obsidian import ObsidianVault, VaultEvent

PARA_FOLDERS = ("Projects", "Areas", "Resources", "Archive")


@dataclass
class NoteDraft:
    """A formatted note ready to be written to the vault (after approval)."""

    title: str
    body: str
    para_folder: str  # Projects | Areas | Resources | Archive
    tags: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        """Render the draft as an Obsidian markdown note with frontmatter."""
        created = datetime.now().strftime("%Y-%m-%d %H:%M")
        tag_line = " ".join(f"#{t.lstrip('#')}" for t in self.tags) if self.tags else ""
        link_line = " ".join(
            f"[[{link.strip('[]')}]]" for link in self.links
        ) if self.links else ""
        frontmatter = (
            "---\n"
            f"title: {self.title}\n"
            f"para: {self.para_folder}\n"
            f"tags: [{', '.join(t.lstrip('#') for t in self.tags)}]\n"
            f"created: {created}\n"
            "---\n\n"
        )
        parts = [frontmatter, f"# {self.title}\n\n", f"{self.body.strip()}\n"]
        if tag_line:
            parts.append(f"\n{tag_line}\n")
        if link_line:
            parts.append(f"\n## Links\n{link_line}\n")
        return "".join(parts)


class KnowledgeSpecialist:
    """Focused sub-agent for the Obsidian knowledge base."""

    def __init__(self, settings: Settings | None = None,
                 router: LLMRouter | None = None,
                 vault: ObsidianVault | None = None,
                 vector_store: VectorStore | None = None,
                 transcriber: Any | None = None,
                 gate: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self.router = router or LLMRouter(self.settings)
        self.vault = vault or ObsidianVault(self.settings)
        self.vectors = vector_store or VectorStore(self.settings)
        # Optional local transcriber for voice-to-note (Phase 4).
        self._transcriber = transcriber
        # Optional AutonomyGate; vault writes are gated by NOTE_WRITE.
        self._gate = gate

    def _get_transcriber(self) -> Any:
        if self._transcriber is None:
            from integrations.transcribe import FasterWhisperTranscriber

            self._transcriber = FasterWhisperTranscriber(self.settings)
        return self._transcriber

    # ------------------------------------------------------------------ #
    # Voice-to-note (Phase 4)
    # ------------------------------------------------------------------ #
    def note_from_audio(self, audio_path: str | Path, *,
                        source: str = "telegram_voice",
                        write: bool = True) -> NoteDraft:
        """Transcribe an audio file and turn it into a vault note.

        **Audio is unconditionally local-only.** Voice memos are the most
        personal content Loop handles and the user cannot review each one before
        it is processed, so both the transcription (local by construction) and
        the note-formatting LLM call are pinned to the local model. If Ollama is
        down, the privacy gate fails closed and this raises rather than falling
        back to the cloud.

        The note lands in the private vault when one is configured, otherwise
        the main vault.
        """
        from integrations.transcribe import INSTALL_HINT

        path = Path(audio_path)
        transcriber = self._get_transcriber()
        if not transcriber.available():
            raise RuntimeError(INSTALL_HINT)

        transcript = transcriber.transcribe(path)
        if not transcript:
            raise ValueError(f"Transcription of {path.name} produced no text.")

        # local_only=True is not a default a caller may override: see the
        # docstring. Everything below stays on the local model.
        slip = self.format_zettelkasten(transcript.text, local_only=True)
        draft = NoteDraft(
            title=slip["title"],
            body=slip["body"],
            para_folder="Resources",
            tags=sorted({*slip["tags"], "voice-note"}),
            links=slip["links"],
        )

        if write:
            self._write_private_note(draft, source=source)
        return draft

    def _write_private_note(self, draft: NoteDraft, *, source: str) -> Path:
        """Write a voice note to the private vault (falling back to the main one)."""
        private = (self.settings.obsidian_private_vault_path or "").strip()
        root = Path(private).expanduser() if private else self.vault.vault_path

        if self._gate is not None:
            from core.autonomy import ActionType

            self._gate.guard(ActionType.NOTE_WRITE, approved=True,
                             detail=f"{source}: {draft.title}")

        filename = self._slugify(draft.title) + ".md"
        return self.write_to_vault(
            draft.to_markdown(), filename, root,
            para_folder=draft.para_folder, overwrite=True,
        )

    # ------------------------------------------------------------------ #
    # PARA classification
    # ------------------------------------------------------------------ #
    def format_para(self, note_text: str, filename: str, *,
                    local_only: bool = False) -> str:
        """Suggest a PARA folder for a note using the local LLM.

        Returns one of Projects | Areas | Resources | Archive. Falls back to
        ``Resources`` if the model output is unrecognised.
        """
        prompt = (
            "You are organising notes with the PARA method.\n"
            "- Projects: things with a deadline and a specific outcome.\n"
            "- Areas: ongoing responsibilities to maintain over time.\n"
            "- Resources: reference material and topics of interest.\n"
            "- Archive: inactive items from the other three.\n\n"
            f"Filename: {filename}\n"
            f"Note content:\n{note_text[:2000]}\n\n"
            "Reply with EXACTLY one word: Projects, Areas, Resources, or Archive."
        )
        raw = self.router.route(
            prompt,
            {"local_only": local_only, "source": "obsidian_private" if local_only else "work"},
        )
        for folder in PARA_FOLDERS:
            if folder.lower() in raw.lower():
                return folder
        return "Resources"

    # ------------------------------------------------------------------ #
    # Zettelkasten formatting
    # ------------------------------------------------------------------ #
    def format_zettelkasten(self, note_text: str, *,
                            local_only: bool = False) -> dict:
        """Format raw text as a Zettelkasten slip.

        Returns a dict: ``{title, tags, links, body}``. Uses the local LLM to
        propose a concise title, atomic body, tags and any [[wiki-links]].
        """
        prompt = (
            "Reformat the note below into an atomic Zettelkasten slip. "
            "Return STRICT JSON with keys: title (short, descriptive), "
            "tags (array of 2-5 lowercase keywords, no '#'), "
            "links (array of related note titles to wiki-link, may be empty), "
            "body (a single self-contained idea, rewritten clearly in markdown).\n\n"
            f"Note:\n{note_text[:3000]}\n\n"
            "JSON:"
        )
        raw = self.router.route(
            prompt,
            {"local_only": local_only, "source": "obsidian_private" if local_only else "work"},
        )
        parsed = self._extract_json(raw)
        if parsed is None:
            # Deterministic fallback so capture never fails outright.
            return {
                "title": self._fallback_title(note_text),
                "tags": [],
                "links": [],
                "body": note_text.strip(),
            }
        return {
            "title": str(parsed.get("title") or self._fallback_title(note_text)).strip(),
            "tags": [str(t).lstrip("#") for t in parsed.get("tags", []) if str(t).strip()],
            "links": [str(link) for link in parsed.get("links", []) if str(link).strip()],
            "body": str(parsed.get("body") or note_text).strip(),
        }

    # ------------------------------------------------------------------ #
    # Capture from chat
    # ------------------------------------------------------------------ #
    def capture_from_chat(self, message_text: str, *,
                          local_only: bool = False) -> str:
        """Process a raw chat message into a ready-to-write markdown note."""
        slip = self.format_zettelkasten(message_text, local_only=local_only)
        filename = self._slugify(slip["title"]) + ".md"
        para = self.format_para(slip["body"], filename, local_only=local_only)
        draft = NoteDraft(
            title=slip["title"],
            body=slip["body"],
            para_folder=para,
            tags=slip["tags"],
            links=slip["links"],
        )
        return draft.to_markdown()

    # ------------------------------------------------------------------ #
    # Write to vault
    # ------------------------------------------------------------------ #
    def write_to_vault(self, content: str, filename: str,
                       vault_path: str | Path | None = None, *,
                       para_folder: str = "Resources",
                       overwrite: bool = False) -> Path:
        """Write a formatted note to the Obsidian vault.

        Creates the file under ``<vault>/<para_folder>/<filename>``. Never
        overwrites an existing file unless ``overwrite=True``.
        """
        root = Path(vault_path).expanduser() if vault_path else self.vault.vault_path
        target = root / para_folder / filename
        if target.exists() and not overwrite:
            raise FileExistsError(
                f"Note already exists: {target} (pass overwrite=True to replace)"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    # ------------------------------------------------------------------ #
    # Indexing / search
    # ------------------------------------------------------------------ #
    def on_vault_event(self, event: VaultEvent) -> None:
        """Watcher callback: (re-)index a note when it is created/modified."""
        try:
            text = event.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return
        title = event.path.stem
        self.vectors.index_note(
            text,
            path=str(event.path),
            title=title,
            local_only=event.local_only,
            modified=datetime.now().strftime("%Y-%m-%d"),
        )

    def index_vault(self) -> int:
        """Index every markdown note currently in the (non-private) vault."""
        count = 0
        for path in self.vault.list_notes():
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            self.vectors.index_note(text, path=str(path), title=path.stem)
            count += 1
        return count

    def search(self, query: str, *, limit: int = 5) -> list:
        """Semantic search across indexed notes + emails."""
        return self.vectors.semantic_search(query, n_results=limit)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_json(raw: str) -> dict | None:
        """Best-effort extraction of a JSON object from an LLM response."""
        if not raw:
            return None
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _fallback_title(text: str) -> str:
        first_line = text.strip().splitlines()[0] if text.strip() else "Untitled note"
        words = first_line.split()
        return " ".join(words[:8]) if words else "Untitled note"

    @staticmethod
    def _slugify(title: str) -> str:
        slug = re.sub(r"[^\w\s-]", "", title.lower()).strip()
        slug = re.sub(r"[\s_-]+", "-", slug)
        return slug or "note"
