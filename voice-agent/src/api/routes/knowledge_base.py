"""
Knowledge Base routes - Upload PDFs/DOCX/TXT/URLs, chunk, embed, and store
"""
import os
import shutil
from typing import List, Optional
from uuid import uuid4
from datetime import datetime
from fastapi import APIRouter, HTTPException, UploadFile, File, Depends, BackgroundTasks
from pydantic import BaseModel, Field

from utils.db import get_db
from kb.document_processor import DocumentProcessor
from kb.vector_store import VectorStoreManager

router = APIRouter()
# Created lazily on first upload (see upload handler), not at import time —
# importing a route module must not require write access to UPLOAD_DIR.
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "./uploads")

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".csv", ".html", ".json"}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(..., description="KB collection name (e.g. 'product_catalog')")
    description: Optional[str] = None
    embedding_model: str = Field("text-embedding-3-small", description="OpenAI/HF embedding model")
    chunk_size: int = Field(800, description="Tokens per chunk")
    chunk_overlap: int = Field(100, description="Overlapping tokens between chunks")


class KnowledgeBaseResponse(BaseModel):
    id: str
    name: str
    description: Optional[str]
    embedding_model: str
    chunk_size: int
    chunk_overlap: int
    document_count: int
    chunk_count: int
    status: str  # ready | indexing | failed
    created_at: datetime


class DocumentInfo(BaseModel):
    id: str
    filename: str
    size_bytes: int
    chunk_count: int
    status: str
    uploaded_at: datetime


@router.post("", response_model=KnowledgeBaseResponse, status_code=201)
async def create_knowledge_base(payload: KnowledgeBaseCreate, db=Depends(get_db)):
    """Create an empty knowledge-base collection."""
    kb_id = f"kb_{uuid4().hex[:12]}"
    record = {
        "id": kb_id,
        **payload.model_dump(),
        "document_count": 0,
        "chunk_count": 0,
        "status": "ready",
        "created_at": datetime.utcnow(),
    }
    await db.knowledge_bases.insert_one(record)

    # Create the underlying vector collection
    vs: VectorStoreManager = VectorStoreManager.instance()
    await vs.create_collection(kb_id, embedding_model=payload.embedding_model)

    return KnowledgeBaseResponse(**record)


@router.get("", response_model=List[KnowledgeBaseResponse])
async def list_knowledge_bases(db=Depends(get_db)):
    return [KnowledgeBaseResponse(**doc) async for doc in db.knowledge_bases.find({})]


@router.post("/{kb_id}/upload", response_model=List[DocumentInfo])
async def upload_documents(
    kb_id: str,
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(..., description="One or more documents"),
    db=Depends(get_db),
):
    """
    Upload one or more documents to a knowledge base.
    Supported: PDF, DOCX, TXT, MD, CSV, HTML, JSON.

    Documents are chunked + embedded asynchronously. Poll GET /{kb_id} for status.
    """
    kb = await db.knowledge_bases.find_one({"id": kb_id})
    if not kb:
        raise HTTPException(404, "Knowledge base not found")

    saved_docs = []
    for upload in files:
        ext = os.path.splitext(upload.filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(400, f"Unsupported file type: {ext}")

        doc_id = f"doc_{uuid4().hex[:12]}"
        save_path = os.path.join(UPLOAD_DIR, kb_id, f"{doc_id}{ext}")
        os.makedirs(os.path.dirname(save_path), exist_ok=True)

        size = 0
        with open(save_path, "wb") as f:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_FILE_SIZE:
                    os.remove(save_path)
                    raise HTTPException(413, f"File {upload.filename} exceeds 50MB limit")
                f.write(chunk)

        record = {
            "id": doc_id,
            "kb_id": kb_id,
            "filename": upload.filename,
            "path": save_path,
            "size_bytes": size,
            "chunk_count": 0,
            "status": "processing",
            "uploaded_at": datetime.utcnow(),
        }
        await db.documents.insert_one(record)
        saved_docs.append(record)

        # Process in background: extract -> chunk -> embed -> store
        background_tasks.add_task(
            _process_document_async, kb_id, doc_id, save_path, kb["chunk_size"], kb["chunk_overlap"]
        )

    return [DocumentInfo(**d) for d in saved_docs]


@router.post("/{kb_id}/url")
async def add_url(
    kb_id: str,
    url: str,
    background_tasks: BackgroundTasks,
    db=Depends(get_db),
):
    """Scrape a URL and add its text content to the KB."""
    kb = await db.knowledge_bases.find_one({"id": kb_id})
    if not kb:
        raise HTTPException(404, "Knowledge base not found")

    doc_id = f"doc_{uuid4().hex[:12]}"
    record = {
        "id": doc_id,
        "kb_id": kb_id,
        "filename": url,
        "path": None,
        "source_url": url,
        "size_bytes": 0,
        "chunk_count": 0,
        "status": "processing",
        "uploaded_at": datetime.utcnow(),
    }
    await db.documents.insert_one(record)

    background_tasks.add_task(
        _process_url_async, kb_id, doc_id, url, kb["chunk_size"], kb["chunk_overlap"]
    )
    return {"document_id": doc_id, "status": "processing"}


@router.get("/{kb_id}", response_model=KnowledgeBaseResponse)
async def get_kb(kb_id: str, db=Depends(get_db)):
    doc = await db.knowledge_bases.find_one({"id": kb_id})
    if not doc:
        raise HTTPException(404, "Not found")
    return KnowledgeBaseResponse(**doc)


@router.get("/{kb_id}/documents", response_model=List[DocumentInfo])
async def list_documents(kb_id: str, db=Depends(get_db)):
    return [DocumentInfo(**d) async for d in db.documents.find({"kb_id": kb_id})]


@router.delete("/{kb_id}/documents/{doc_id}", status_code=204)
async def delete_document(kb_id: str, doc_id: str, db=Depends(get_db)):
    doc = await db.documents.find_one({"id": doc_id, "kb_id": kb_id})
    if not doc:
        raise HTTPException(404, "Document not found")

    # Remove vectors
    vs = VectorStoreManager.instance()
    await vs.delete_by_doc_id(kb_id, doc_id)

    # Remove file
    if doc.get("path") and os.path.exists(doc["path"]):
        os.remove(doc["path"])

    await db.documents.delete_one({"id": doc_id})
    await db.knowledge_bases.update_one(
        {"id": kb_id},
        {"$inc": {"document_count": -1, "chunk_count": -doc.get("chunk_count", 0)}},
    )


@router.delete("/{kb_id}", status_code=204)
async def delete_kb(kb_id: str, db=Depends(get_db)):
    vs = VectorStoreManager.instance()
    await vs.delete_collection(kb_id)
    await db.documents.delete_many({"kb_id": kb_id})
    await db.knowledge_bases.delete_one({"id": kb_id})
    kb_dir = os.path.join(UPLOAD_DIR, kb_id)
    if os.path.exists(kb_dir):
        shutil.rmtree(kb_dir)


@router.post("/{kb_id}/search")
async def search_kb(kb_id: str, query: str, top_k: int = 5):
    """Test KB retrieval - returns top matching chunks for a query."""
    vs = VectorStoreManager.instance()
    results = await vs.search(kb_id, query, top_k=top_k)
    return {"query": query, "results": results}


# ----------------- Background workers -----------------

async def _process_document_async(kb_id: str, doc_id: str, path: str, chunk_size: int, overlap: int):
    """Extract, chunk, embed, and store a document."""
    from utils.db import get_db_instance
    db = get_db_instance()
    try:
        processor = DocumentProcessor(chunk_size=chunk_size, chunk_overlap=overlap)
        chunks = await processor.process_file(path)

        vs = VectorStoreManager.instance()
        await vs.add_chunks(kb_id, doc_id, chunks)

        await db.documents.update_one(
            {"id": doc_id},
            {"$set": {"status": "ready", "chunk_count": len(chunks)}},
        )
        await db.knowledge_bases.update_one(
            {"id": kb_id},
            {"$inc": {"document_count": 1, "chunk_count": len(chunks)}},
        )
    except Exception as e:
        await db.documents.update_one(
            {"id": doc_id}, {"$set": {"status": "failed", "error": str(e)}}
        )


async def _process_url_async(kb_id: str, doc_id: str, url: str, chunk_size: int, overlap: int):
    from utils.db import get_db_instance
    db = get_db_instance()
    try:
        processor = DocumentProcessor(chunk_size=chunk_size, chunk_overlap=overlap)
        chunks = await processor.process_url(url)

        vs = VectorStoreManager.instance()
        await vs.add_chunks(kb_id, doc_id, chunks)

        await db.documents.update_one(
            {"id": doc_id},
            {"$set": {"status": "ready", "chunk_count": len(chunks)}},
        )
        await db.knowledge_bases.update_one(
            {"id": kb_id},
            {"$inc": {"document_count": 1, "chunk_count": len(chunks)}},
        )
    except Exception as e:
        await db.documents.update_one(
            {"id": doc_id}, {"$set": {"status": "failed", "error": str(e)}}
        )
