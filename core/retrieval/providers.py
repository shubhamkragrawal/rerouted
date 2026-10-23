"""Optional local retrieval providers and stable provider metadata."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Protocol, cast

from .models import IndexedDocument


@dataclass(frozen=True)
class EmbeddingMetadata:
    provider: str
    model_id: str
    model_version: str
    pooling: str
    normalize: bool
    query_instruction: str
    document_instruction: str

    def cache_key(self, document: IndexedDocument) -> str:
        payload = {
            "document_hash": hashlib.sha256(document.text.encode("utf-8")).hexdigest(),
            "model_id": self.model_id,
            "model_version": self.model_version,
            "pooling": self.pooling,
            "normalize": self.normalize,
            "query_instruction": self.query_instruction,
            "document_instruction": self.document_instruction,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class EmbeddingProvider(Protocol):
    metadata: EmbeddingMetadata

    def embed_query(self, text: str) -> Sequence[float]: ...

    def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


class CrossEncoderProvider(Protocol):
    model_id: str
    model_version: str

    def score(self, query: str, documents: Sequence[str]) -> Sequence[float]: ...


class HashEmbeddingProvider:
    """Dependency-free deterministic provider used for offline runs and fallback."""

    metadata = EmbeddingMetadata(
        provider="hash",
        model_id="blake2b-token-hash-v1",
        model_version="1",
        pooling="signed-token-sum",
        normalize=True,
        query_instruction="",
        document_instruction="",
    )

    def __init__(self, dimensions: int = 256) -> None:
        if dimensions < 1:
            raise ValueError("dimensions must be positive")
        self.dimensions = dimensions
        self.metadata = EmbeddingMetadata(
            provider="hash",
            model_id="blake2b-token-hash-v1",
            model_version="1",
            pooling=f"signed-token-sum:{dimensions}",
            normalize=True,
            query_instruction="",
            document_instruction="",
        )

    def embed_query(self, text: str) -> Sequence[float]:
        from .hybrid import hashing_embedding

        return hashing_embedding(text, dimensions=self.dimensions)

    def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return [self.embed_query(text) for text in texts]


class SentenceTransformersEmbeddingProvider:
    """Lazy local adapter for BGE; construction never downloads model files."""

    def __init__(
        self,
        *,
        model_id: str = "BAAI/bge-small-en-v1.5",
        model_version: str = "unspecified-local-revision",
        pooling: str = "sentence-transformers-default",
        normalize: bool = True,
        query_instruction: str = "Represent this sentence for searching relevant passages: ",
        document_instruction: str = "",
    ) -> None:
        self.metadata = EmbeddingMetadata(
            provider="sentence-transformers",
            model_id=model_id,
            model_version=model_version,
            pooling=pooling,
            normalize=normalize,
            query_instruction=query_instruction,
            document_instruction=document_instruction,
        )
        self._model: object | None = None

    def _load(self) -> object:
        if self._model is None:
            try:
                module = cast(Any, import_module("sentence_transformers"))
            except ImportError as exc:
                raise RuntimeError("sentence-transformers is not installed") from exc
            self._model = module.SentenceTransformer(
                self.metadata.model_id, local_files_only=True
            )
        return self._model

    def _encode(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        model = cast(Any, self._load())
        encode = model.encode
        values = encode(
            list(texts),
            normalize_embeddings=self.metadata.normalize,
            convert_to_numpy=False,
        )
        return [[float(value) for value in vector] for vector in values]

    def embed_query(self, text: str) -> Sequence[float]:
        return self._encode([self.metadata.query_instruction + text])[0]

    def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        prefixed = [self.metadata.document_instruction + text for text in texts]
        return self._encode(prefixed)


class SentenceTransformersCrossEncoder:
    """Lazy local CrossEncoder adapter. No checkpoint is selected by default."""

    def __init__(self, model_id: str, *, model_version: str = "unspecified-local-revision") -> None:
        if not model_id:
            raise ValueError("model_id must be selected from a reviewed benchmark")
        self.model_id = model_id
        self.model_version = model_version
        self._model: object | None = None

    def _load(self) -> object:
        if self._model is None:
            try:
                module = cast(Any, import_module("sentence_transformers"))
            except ImportError as exc:
                raise RuntimeError("sentence-transformers is not installed") from exc
            self._model = module.CrossEncoder(self.model_id, local_files_only=True)
        return self._model

    def score(self, query: str, documents: Sequence[str]) -> Sequence[float]:
        model = cast(Any, self._load())
        predict = model.predict
        values = predict([(query, document) for document in documents])
        return [float(value) for value in values]


def corpus_hash(documents: Sequence[IndexedDocument]) -> str:
    """Hash ordered retrieval inputs and source versions deterministically."""
    digest = hashlib.sha256()
    for document in sorted(documents, key=lambda item: item.document_id):
        record = {
            "document_id": document.document_id,
            "text_hash": hashlib.sha256(document.text.encode("utf-8")).hexdigest(),
            "authority": document.authority,
            "doc_type": document.doc_type,
            "source_url": document.source_url,
            "effective_from": document.effective_from.isoformat(),
            "effective_to": document.effective_to.isoformat() if document.effective_to else None,
            "carrier": document.carrier,
            "jurisdiction": document.jurisdiction,
        }
        digest.update(json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()
