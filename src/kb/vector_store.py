"""
Vector Store Manager - MongoDB-backed embeddings store for RAG.

Embeddings live in the same MongoDB the rest of the app uses (collection
`kb_vectors`), and similarity search is plain cosine in NumPy. This keeps the
whole stack on one datastore — no separate vector DB to run — which is the
right trade-off for the per-agent knowledge bases here (tens to low-thousands
of chunks each). For very large corpora you'd swap this for Atlas Vector Search
or a dedicated vector DB; the public method signatures below are the seam.

One Mongo document per chunk:
    { _id, kb_id, doc_id, chunk_index, text, source, embedding: [float, ...] }
"""
import os
import logging
from typing import List, Dict, Optional

import numpy as np
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

VECTORS_COLLECTION = "kb_vectors"


class VectorStoreManager:
    _instance: Optional["VectorStoreManager"] = None

    def __init__(self):
        self.openai = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self._col = None  # Mongo collection, set in initialize()
        # tiny in-process cache of {kb_id: (ids, texts, sources, matrix)} so we
        # don't reload every chunk from Mongo on every single search.
        self._cache: Dict[str, tuple] = {}
        VectorStoreManager._instance = self

    @classmethod
    def instance(cls) -> "VectorStoreManager":
        if cls._instance is None:
            cls._instance = VectorStoreManager()
        return cls._instance

    async def initialize(self):
        from utils.db import get_db_instance
        db = get_db_instance()
        if db is None:
            logger.warning("No MongoDB — knowledge base / RAG is disabled.")
            self._col = None
            return
        self._col = db[VECTORS_COLLECTION]
        await self._col.create_index("kb_id")
        await self._col.create_index([("kb_id", 1), ("doc_id", 1)])
        logger.info("Vector store ready (MongoDB-backed)")

    def _require(self):
        if self._col is None:
            raise RuntimeError("Vector store needs MongoDB; set MONGO_URL.")

    async def create_collection(self, kb_id: str, embedding_model: str = "text-embedding-3-small"):
        # No-op for the Mongo store — a "collection" is just chunks tagged with
        # kb_id. Kept for interface compatibility with the KB routes.
        return

    async def delete_collection(self, kb_id: str):
        self._require()
        await self._col.delete_many({"kb_id": kb_id})
        self._cache.pop(kb_id, None)
        logger.info(f"Deleted all vectors for KB {kb_id}")

    async def add_chunks(self, kb_id: str, doc_id: str, chunks: List[Dict]):
        """Embed each chunk and store in Mongo."""
        self._require()
        if not chunks:
            return

        texts = [c["text"] for c in chunks]
        embeddings = await self._embed_batch(texts)

        docs = []
        for chunk, vector in zip(chunks, embeddings):
            docs.append({
                "kb_id": kb_id,
                "doc_id": doc_id,
                "chunk_index": chunk["chunk_index"],
                "text": chunk["text"],
                "source": chunk.get("source"),
                "embedding": vector,
            })
        if docs:
            await self._col.insert_many(docs)
        self._cache.pop(kb_id, None)  # invalidate cache
        logger.info(f"Added {len(docs)} chunks to KB {kb_id} (doc {doc_id})")

    async def search(self, kb_id: str, query: str, top_k: int = 5) -> List[Dict]:
        self._require()
        query_vec = (await self._embed_batch([query]))[0]
        ids, texts, sources, matrix = await self._load_kb(kb_id)
        if matrix is None or len(texts) == 0:
            return []

        q = np.asarray(query_vec, dtype=np.float32)
        q /= (np.linalg.norm(q) + 1e-9)
        # matrix rows are pre-normalized -> dot product == cosine similarity
        sims = matrix @ q
        top_idx = np.argsort(-sims)[:top_k]
        return [
            {"text": texts[i], "source": sources[i], "score": float(sims[i])}
            for i in top_idx
        ]

    async def delete_by_doc_id(self, kb_id: str, doc_id: str):
        self._require()
        await self._col.delete_many({"kb_id": kb_id, "doc_id": doc_id})
        self._cache.pop(kb_id, None)

    # ----- internals -----

    # Cap how many KB matrices we hold in memory (each can be large). Evicts the
    # oldest when exceeded — keeps memory bounded across many agents/KBs.
    _MAX_CACHED_KBS = 16

    async def _load_kb(self, kb_id: str):
        """Load (and cache) all chunk vectors for a KB as a normalized matrix."""
        if kb_id in self._cache:
            return self._cache[kb_id]

        # Evict oldest cached KB if at capacity (dicts preserve insertion order).
        if len(self._cache) >= self._MAX_CACHED_KBS:
            oldest = next(iter(self._cache))
            self._cache.pop(oldest, None)

        ids, texts, sources, vecs = [], [], [], []
        cursor = self._col.find(
            {"kb_id": kb_id},
            {"text": 1, "source": 1, "embedding": 1},
        )
        async for d in cursor:
            ids.append(str(d["_id"]))
            texts.append(d["text"])
            sources.append(d.get("source"))
            vecs.append(d["embedding"])

        if not vecs:
            self._cache[kb_id] = ([], [], [], None)
            return self._cache[kb_id]

        matrix = np.asarray(vecs, dtype=np.float32)
        # normalize rows once so search is a single matmul
        norms = np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9
        matrix = matrix / norms
        self._cache[kb_id] = (ids, texts, sources, matrix)
        return self._cache[kb_id]

    async def _embed_batch(self, texts: List[str]) -> List[List[float]]:
        BATCH = 96
        out = []
        for i in range(0, len(texts), BATCH):
            batch = texts[i : i + BATCH]
            resp = await self.openai.embeddings.create(
                model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
                input=batch,
            )
            out.extend([d.embedding for d in resp.data])
        return out
