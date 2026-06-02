"""
Vector Store Manager - Qdrant-backed embeddings store for RAG.
Each knowledge base = one Qdrant collection.
"""
import os
import logging
from typing import List, Dict, Optional
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


class VectorStoreManager:
    _instance: Optional["VectorStoreManager"] = None

    def __init__(self):
        self.qdrant_url = os.getenv("QDRANT_URL", "http://localhost:6333")
        self.qdrant_api_key = os.getenv("QDRANT_API_KEY")
        self.openai = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.client: Optional[AsyncQdrantClient] = None
        VectorStoreManager._instance = self

    @classmethod
    def instance(cls) -> "VectorStoreManager":
        if cls._instance is None:
            cls._instance = VectorStoreManager()
        return cls._instance

    async def initialize(self):
        self.client = AsyncQdrantClient(url=self.qdrant_url, api_key=self.qdrant_api_key)
        logger.info(f"Qdrant connected at {self.qdrant_url}")

    async def create_collection(self, kb_id: str, embedding_model: str = "text-embedding-3-small"):
        # text-embedding-3-small = 1536 dims; text-embedding-3-large = 3072
        dims = 3072 if "large" in embedding_model else 1536

        try:
            await self.client.create_collection(
                collection_name=kb_id,
                vectors_config=models.VectorParams(size=dims, distance=models.Distance.COSINE),
            )
            logger.info(f"Created KB collection {kb_id}")
        except Exception as e:
            if "already exists" in str(e).lower():
                logger.info(f"Collection {kb_id} already exists")
            else:
                raise

    async def delete_collection(self, kb_id: str):
        try:
            await self.client.delete_collection(kb_id)
        except Exception as e:
            logger.warning(f"Failed to delete collection: {e}")

    async def add_chunks(self, kb_id: str, doc_id: str, chunks: List[Dict]):
        """Embed each chunk and upsert into Qdrant."""
        if not chunks:
            return

        # Batch embeddings
        texts = [c["text"] for c in chunks]
        embeddings = await self._embed_batch(texts)

        points = []
        for i, (chunk, vector) in enumerate(zip(chunks, embeddings)):
            points.append(
                models.PointStruct(
                    id=self._generate_point_id(doc_id, i),
                    vector=vector,
                    payload={
                        "doc_id": doc_id,
                        "text": chunk["text"],
                        "source": chunk.get("source"),
                        "chunk_index": chunk["chunk_index"],
                    },
                )
            )

        await self.client.upsert(collection_name=kb_id, points=points)
        logger.info(f"Added {len(points)} chunks to {kb_id} (doc {doc_id})")

    async def search(self, kb_id: str, query: str, top_k: int = 5) -> List[Dict]:
        embedding = (await self._embed_batch([query]))[0]

        results = await self.client.search(
            collection_name=kb_id,
            query_vector=embedding,
            limit=top_k,
        )
        return [
            {
                "text": r.payload["text"],
                "source": r.payload.get("source"),
                "score": r.score,
            }
            for r in results
        ]

    async def delete_by_doc_id(self, kb_id: str, doc_id: str):
        await self.client.delete(
            collection_name=kb_id,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))]
                )
            ),
        )

    async def _embed_batch(self, texts: List[str]) -> List[List[float]]:
        # OpenAI allows up to 2048 inputs per request
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

    @staticmethod
    def _generate_point_id(doc_id: str, chunk_index: int) -> str:
        import uuid
        return str(uuid.uuid5(uuid.NAMESPACE_OID, f"{doc_id}:{chunk_index}"))
