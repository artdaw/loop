"""Vector store — local ChromaDB + Ollama embeddings.

Wraps ChromaDB in local persistent mode (data lives under
``settings.chroma_persist_dir``, default ``data/chroma``) and embeds text with
the ``nomic-embed-text`` model served by Ollama.

Two collections are kept side by side:
    - ``loop_knowledge`` — Obsidian notes captured by the Knowledge Specialist.
    - ``loop_emails``    — email summaries indexed by the Email Specialist.

All embedding happens locally (Ollama), so nothing here ever reaches a cloud
service — safe for private notes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import httpx

from config.settings import Settings, get_settings

KNOWLEDGE_COLLECTION = "loop_knowledge"
EMAILS_COLLECTION = "loop_emails"


@dataclass
class SearchResult:
    """A single semantic-search hit."""

    id: str
    document: str
    metadata: dict[str, Any]
    distance: float

    @property
    def kind(self) -> str:
        """'note' or 'email' (falls back to metadata 'kind')."""
        return str(self.metadata.get("kind", "note"))


class VectorStore:
    """Local persistent ChromaDB wrapper with Ollama embeddings."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._http = httpx.Client(timeout=60.0)
        self._client: Any | None = None

    # ------------------------------------------------------------------ #
    # Chroma client / collections
    # ------------------------------------------------------------------ #
    def _get_client(self) -> Any:
        if self._client is None:
            import chromadb  # local import: chromadb import is heavy

            self._client = chromadb.PersistentClient(path=self.settings.chroma_persist_dir)
        return self._client

    def _collection(self, name: str) -> Any:
        return self._get_client().get_or_create_collection(
            name=name, metadata={"hnsw:space": "cosine"}
        )

    # ------------------------------------------------------------------ #
    # Embeddings (Ollama, local only)
    # ------------------------------------------------------------------ #
    def embed(self, text: str) -> list[float]:
        """Embed a single text with nomic-embed-text via Ollama."""
        url = f"{self.settings.ollama_base_url.rstrip('/')}/api/embeddings"
        resp = self._http.post(
            url,
            json={"model": self.settings.ollama_embed_model, "prompt": text},
        )
        resp.raise_for_status()
        data = resp.json()
        embedding = data.get("embedding")
        if not embedding:
            raise RuntimeError("Ollama returned an empty embedding")
        return embedding

    # ------------------------------------------------------------------ #
    # Write path
    # ------------------------------------------------------------------ #
    def embed_and_store(self, text: str, metadata: dict[str, Any] | None = None,
                        *, collection: str = KNOWLEDGE_COLLECTION,
                        doc_id: str | None = None) -> str:
        """Embed ``text`` and upsert it into ``collection``. Returns the doc id."""
        meta = dict(metadata or {})
        meta.setdefault("kind", "email" if collection == EMAILS_COLLECTION else "note")
        identifier = doc_id or self._make_id(text, meta)
        embedding = self.embed(text)
        self._collection(collection).upsert(
            ids=[identifier],
            embeddings=[embedding],
            documents=[text],
            metadatas=[self._clean_metadata(meta)],
        )
        return identifier

    def index_note(self, text: str, *, path: str, title: str,
                   local_only: bool = False, modified: str | None = None) -> str:
        """Index (or re-index) an Obsidian note in the knowledge collection."""
        metadata = {
            "kind": "note",
            "title": title,
            "path": path,
            "local_only": local_only,
        }
        if modified:
            metadata["date"] = modified
        return self.embed_and_store(text, metadata, collection=KNOWLEDGE_COLLECTION,
                                    doc_id=f"note::{path}")

    def index_email(self, subject: str, summary: str, sender: str, date: str) -> str:
        """Index an email summary in the emails collection."""
        document = f"{subject}\n\n{summary}"
        metadata = {
            "kind": "email",
            "title": subject,
            "subject": subject,
            "sender": sender,
            "date": date,
        }
        return self.embed_and_store(document, metadata, collection=EMAILS_COLLECTION,
                                    doc_id=f"email::{sender}::{subject}::{date}")

    # ------------------------------------------------------------------ #
    # Read path
    # ------------------------------------------------------------------ #
    def semantic_search(self, query: str, n_results: int = 5, *,
                        collections: tuple[str, ...] | None = None) -> list[SearchResult]:
        """Embed ``query`` and return the top-n results across collections."""
        targets = collections or (KNOWLEDGE_COLLECTION, EMAILS_COLLECTION)
        query_embedding = self.embed(query)
        hits: list[SearchResult] = []

        for name in targets:
            collection = self._collection(name)
            count = collection.count()
            if count == 0:
                continue
            res = collection.query(
                query_embeddings=[query_embedding],
                n_results=min(n_results, count),
                include=["documents", "metadatas", "distances"],
            )
            ids = res.get("ids", [[]])[0]
            docs = res.get("documents", [[]])[0]
            metas = res.get("metadatas", [[]])[0]
            dists = res.get("distances", [[]])[0]
            for i, doc_id in enumerate(ids):
                hits.append(
                    SearchResult(
                        id=doc_id,
                        document=docs[i] if i < len(docs) else "",
                        metadata=metas[i] if i < len(metas) else {},
                        distance=dists[i] if i < len(dists) else 0.0,
                    )
                )

        hits.sort(key=lambda h: h.distance)
        return hits[:n_results]

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _make_id(text: str, metadata: dict[str, Any]) -> str:
        digest = hashlib.sha256()
        digest.update(text.encode("utf-8"))
        digest.update(str(sorted(metadata.items())).encode("utf-8"))
        return digest.hexdigest()[:32]

    @staticmethod
    def _clean_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
        """ChromaDB only accepts str/int/float/bool metadata values."""
        cleaned: dict[str, Any] = {}
        for key, value in metadata.items():
            if isinstance(value, (str, int, float, bool)):
                cleaned[key] = value
            elif value is None:
                continue
            else:
                cleaned[key] = str(value)
        return cleaned
