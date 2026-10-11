"""Semantic retrieval over ingested documents — the RAG layer.

WHY THIS IS NOT DECORATION. The exact `(carrier, clause_ref)` map classifies clauses
in wordings a human has read. For a wording nobody has read, that map returns nothing
and the engine can only report "unaccounted". Retrieval is what lets the system find
the *relevant passage* in an unfamiliar document so that (a) a reader can be shown
real evidence for a finding and (b) the LLM has grounded text to explain rather than
its own recollection of what insurance policies usually say.

THE PROVENANCE RULE. Every chunk carries `(document_id, page_no, char_start,
char_end)`. A retrieved passage is therefore a *span*, not a paragraph of text that
came from somewhere. That is what makes a citation checkable: the same span-fidelity
gate that guards findings can re-verify a retrieved quote byte-for-byte against the
immutable stored page.

NO VECTOR DATABASE. A policy pack is a few hundred chunks. An in-memory numpy matrix
with cosine similarity answers in under a millisecond, and adding FAISS or Chroma
would be an install risk on demo night in exchange for nothing. If the corpus ever
reaches millions of chunks, swap the index behind `Retriever.search` — the interface
does not change.

FALLBACK IS A FEATURE. With no API key, retrieval uses BM25 lexical scoring in pure
Python. It is genuinely worse at synonyms and genuinely fine at finding the clause
that contains "forcible entry", so the product still returns real evidence and says
which mode produced it.
"""
from __future__ import annotations

import math
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Optional

from . import config
from .lang import tokenizer_for_document, tokenizer_for_query

# Chunking. Policy prose is dense and clause bodies are short, so small chunks with
# overlap retrieve better than page-sized blocks: a page-level chunk buries the one
# sentence that matters among forty that do not.
CHUNK_CHARS = 700
CHUNK_OVERLAP = 140
MIN_CHUNK_CHARS = 60


@dataclass
class Chunk:
    """One retrievable passage, anchored to real offsets in a real page."""

    id: int
    document_id: str
    document_kind: str
    filename: str
    page_no: int
    char_start: int
    char_end: int
    text: str
    tokens: tuple[str, ...] = field(default_factory=tuple)
    # A corpus manifest hash is immutable source metadata.  An empty value means a
    # caller-uploaded or otherwise unclassified document and is health-restricted.
    document_sha256: str = ""

    def as_citation(self) -> dict:
        return {
            "document_id": self.document_id,
            "document_kind": self.document_kind,
            "filename": self.filename,
            "page_no": self.page_no,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "quote": self.text,
        }


@dataclass
class Hit:
    chunk: Chunk
    score: float
    method: str

    def as_dict(self) -> dict:
        out = self.chunk.as_citation()
        out["score"] = round(self.score, 4)
        out["retrieval_method"] = self.method
        return out


def _tokenise(text: str) -> tuple[str, ...]:
    """Tokenise a document through the tokenizer selected by its Unicode script."""
    return tokenizer_for_document(text).tokenize(text)


def _tokenise_query(text: str) -> tuple[str, ...]:
    return tokenizer_for_query(text).tokenize(text)


def chunk_page(text: str, *, page_no: int, document_id: str, document_kind: str,
               filename: str, start_id: int, document_sha256: str = "") -> list[Chunk]:
    """Split one page into overlapping chunks, preserving real character offsets.

    Offsets are into the stored page text, so a chunk is a verifiable span. Splitting
    on a sentence boundary near the target size keeps clauses intact where possible
    without letting a page with no full stops produce one giant chunk.
    """
    chunks: list[Chunk] = []
    if not text or not text.strip():
        return chunks
    length = len(text)
    cursor = 0
    next_id = start_id
    while cursor < length:
        end = min(cursor + CHUNK_CHARS, length)
        if end < length:
            # Prefer a sentence or line break within the last third of the window.
            window_start = cursor + (CHUNK_CHARS * 2) // 3
            boundary = max(text.rfind(". ", window_start, end),
                           text.rfind("\n", window_start, end))
            if boundary > cursor:
                end = boundary + 1
        body = text[cursor:end]
        if len(body.strip()) >= MIN_CHUNK_CHARS:
            chunks.append(Chunk(
                id=next_id, document_id=document_id, document_kind=document_kind,
                filename=filename, page_no=page_no, char_start=cursor,
                char_end=end, text=body.strip(), tokens=_tokenise(body),
                document_sha256=document_sha256))
            next_id += 1
        if end >= length:
            break
        cursor = max(end - CHUNK_OVERLAP, cursor + 1)
    return chunks


def build_chunks(conn: sqlite3.Connection) -> list[Chunk]:
    """Chunk every stored page of every ingested document in this case."""
    rows = conn.execute(
        "SELECT p.document_id AS document_id, p.page_no AS page_no, p.text AS text, "
        "d.kind AS kind, d.filename AS filename, d.sha256 AS sha256 "
        "FROM page_text p JOIN document d ON d.id = p.document_id "
        "ORDER BY d.kind, p.document_id, p.page_no").fetchall()
    chunks: list[Chunk] = []
    for row in rows:
        chunks.extend(chunk_page(
            row["text"], page_no=row["page_no"], document_id=row["document_id"],
            document_kind=row["kind"], filename=row["filename"],
            start_id=len(chunks), document_sha256=row["sha256"]))
        if len(chunks) >= config.MAX_CHUNKS_TO_EMBED * 2:
            break
    return chunks


class _BM25:
    """Okapi BM25 over the chunk corpus. Pure Python, no dependency.

    This is the no-key path and it is deliberately a real ranking function rather
    than a substring match, so the fallback returns useful evidence instead of
    whatever happens to contain the query's first word.
    """

    K1 = 1.5
    B = 0.75

    def __init__(self, chunks: list[Chunk]):
        self.chunks = chunks
        self.doc_freq: Counter[str] = Counter()
        self.term_freq: list[Counter[str]] = []
        for chunk in chunks:
            counts = Counter(chunk.tokens)
            self.term_freq.append(counts)
            self.doc_freq.update(counts.keys())
        self.n = max(len(chunks), 1)
        self.avg_len = (sum(len(c.tokens) for c in chunks) / self.n) if chunks else 1.0

    def search(self, query: str, top_k: int) -> list[Hit]:
        terms = _tokenise_query(query)
        if not terms or not self.chunks:
            return []
        scored: list[tuple[float, int]] = []
        for index, counts in enumerate(self.term_freq):
            doc_len = max(len(self.chunks[index].tokens), 1)
            score = 0.0
            for term in terms:
                tf = counts.get(term, 0)
                if not tf:
                    continue
                df = self.doc_freq.get(term, 0)
                idf = math.log(1 + (self.n - df + 0.5) / (df + 0.5))
                denom = tf + self.K1 * (1 - self.B + self.B * doc_len / self.avg_len)
                score += idf * (tf * (self.K1 + 1)) / denom
            if score > 0:
                scored.append((score, index))
        scored.sort(reverse=True)
        return [Hit(self.chunks[i], s, "LEXICAL_BM25") for s, i in scored[:top_k]]


class Retriever:
    """Chunk store plus an index. Embeddings when a key is available, BM25 otherwise.

    Built once per case and cached on the session, because embedding the same
    unchanged document on every question would be slow and would cost money for no
    new information.
    """

    def __init__(self, chunks: list[Chunk]):
        self.chunks = chunks
        self._bm25 = _BM25(chunks)
        self._matrix = None            # numpy array, or None
        self._embedded = False
        self.embed_error: Optional[str] = None

    # ── construction ───────────────────────────────────────────────────────
    @classmethod
    def from_connection(cls, conn: sqlite3.Connection) -> "Retriever":
        return cls(build_chunks(conn))

    @property
    def method(self) -> str:
        return "SEMANTIC_EMBEDDINGS" if self._embedded else "LEXICAL_BM25"

    def stats(self) -> dict:
        pages = {(c.document_id, c.page_no) for c in self.chunks}
        return {
            "chunks": len(self.chunks),
            "pages_indexed": len(pages),
            "documents_indexed": len({c.document_id for c in self.chunks}),
            "method": self.method,
            "embedded": self._embedded,
            "embed_error": self.embed_error,
        }

    # ── embeddings ─────────────────────────────────────────────────────────
    def ensure_embeddings(self) -> bool:
        """Embed the corpus once. Returns True when the semantic index is live.

        Any failure — no key, no numpy, rate limit, network — leaves BM25 in place and
        records why. Retrieval never becomes unavailable; it becomes honest about
        which mode produced the result.
        """
        if self._embedded:
            return True
        # Remote embedding calls would transmit complete document chunks.  The shared
        # model boundary deliberately accepts only redacted prompts, so retrieval stays
        # lexical until a redaction-preserving embedding representation is implemented.
        # This is a conservative capability reduction, not a silent fallback: stats()
        # reports LEXICAL_BM25 and this reason to every caller.
        self.embed_error = (
            "Remote embeddings are disabled so raw document text never leaves the "
            "evidence boundary; using lexical retrieval.")
        return False

    def _embed_query(self, query: str):
        """No remote query embedding path: retrieval remains lexical and evidence-local."""
        _ = query
        return None

    # ── search ─────────────────────────────────────────────────────────────
    def search(self, query: str, top_k: int = config.MAX_CONTEXT_CHUNKS,
               kinds: Optional[Iterable[str]] = None) -> list[Hit]:
        """Rank chunks against a query. Semantic when available, lexical otherwise.

        `kinds` restricts the search to document kinds, which matters because a
        question about sums insured should look at the schedule and a question about
        exclusions should look at the wording.
        """
        if not self.chunks:
            return []
        allowed = set(kinds) if kinds else None

        if self.ensure_embeddings() and self._matrix is not None:
            vector = self._embed_query(query)
            if vector is not None:
                import numpy as np
                scores = self._matrix @ vector
                order = np.argsort(-scores)
                hits: list[Hit] = []
                for index in order:
                    chunk = self.chunks[int(index)]
                    if allowed and chunk.document_kind not in allowed:
                        continue
                    score = float(scores[int(index)])
                    if score <= 0:
                        break
                    hits.append(Hit(chunk, score, "SEMANTIC_EMBEDDINGS"))
                    if len(hits) >= top_k:
                        break
                if hits:
                    return hits

        lexical = self._bm25.search(query, top_k * 3 if allowed else top_k)
        if allowed:
            lexical = [h for h in lexical if h.chunk.document_kind in allowed]
        return lexical[:top_k]

    def verify_hit(self, conn: sqlite3.Connection, hit: Hit) -> bool:
        """Re-verify a retrieved quote against the immutable stored page.

        The point of the whole architecture: a retrieved passage is only evidence if
        it demonstrably exists where it claims to. This is the same check the
        span-fidelity gate applies to findings, applied to retrieval output, so a
        citation the UI shows cannot be a paraphrase or a fabrication.
        """
        from .gates import norm_ws
        from .ingest import page_text

        source = page_text(conn, hit.chunk.document_id, hit.chunk.page_no)
        segment = source[hit.chunk.char_start:hit.chunk.char_end]
        return norm_ws(segment) == norm_ws(hit.chunk.text)
