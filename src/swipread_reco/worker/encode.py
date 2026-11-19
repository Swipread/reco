"""Encoding of new articles into embeddings (spec 8.4)."""

import html
import logging
import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

import numpy as np

from swipread_reco.params import EMBEDDING_MODEL
from swipread_reco.store import Store

HF_MODEL = f"intfloat/{EMBEDDING_MODEL}"
# Commit of the model on Hugging Face: the same weights everywhere, and the one the Docker image
# bakes in. Changing it changes the vectors: re-encode every article (new EMBEDDING_MODEL).
HF_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
# The files SentenceTransformer loads; the repository also holds ONNX, OpenVINO and .bin copies.
HF_FILES = [
    "modules.json", "sentence_bert_config.json",
    "1_Pooling/config.json", "config.json", "model.safetensors",
    "sentencepiece.bpe.model", "special_tokens_map.json", "tokenizer.json",
    "tokenizer_config.json",
]
MAX_TOKENS = 512           # longer texts are truncated (spec 8.4)
DEFAULT_BATCH = 64         # articles encoded per pass (spec 8.4)

_TAG = re.compile(r"<[^>]*>")
_SPACES = re.compile(r"\s+")

log = logging.getLogger(__name__)


class Encoder(Protocol):
    def encode(self, texts: Sequence[str]) -> np.ndarray:
        """(len(texts), d) float32 array, each row of norm 1."""
        ...


class E5Encoder:
    """multilingual-e5 through sentence-transformers on CPU, loaded on the first encode()."""

    def __init__(self, model_name: str = HF_MODEL, revision: str = HF_REVISION) -> None:
        self.model_name = model_name
        self.revision = revision
        self._model: Any = None

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # heavy: torch

            self._model = SentenceTransformer(
                self.model_name, revision=self.revision, device="cpu"
            )
            self._model.max_seq_length = MAX_TOKENS
        vectors = self._model.encode(
            list(texts), normalize_embeddings=True, convert_to_numpy=True,
            show_progress_bar=False,
        )
        return np.asarray(vectors, dtype=np.float32)


def _squash(text: str) -> str:
    return _SPACES.sub(" ", text).strip()


def article_text(title: str, summary: str) -> str:
    """`query: {title}. {summary}`, summary without HTML tags nor entities, single spaces."""
    title = _squash(title)
    summary = _squash(html.unescape(_TAG.sub("", summary)))
    head = title if title.endswith((".", "!", "?")) else f"{title}."
    return f"query: {head} {summary}" if summary else f"query: {head}"


def encode_pending(
    store: Store, encoder: Encoder, now: datetime, batch: int = DEFAULT_BATCH
) -> int:
    """Encode up to `batch` articles lacking an embedding of EMBEDDING_MODEL; the count encoded."""
    rows = store.articles_to_embed(EMBEDDING_MODEL, batch)
    if not rows:
        return 0
    vectors = encoder.encode([article_text(title, summary) for _, title, summary in rows])
    store.write_embeddings(
        [(article_id, vectors[k]) for k, (article_id, _, _) in enumerate(rows)],
        EMBEDDING_MODEL, now,
    )
    log.info("encoded %d articles", len(rows))
    return len(rows)
