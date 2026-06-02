"""
Document Processor - extract text from PDF/DOCX/TXT/HTML/URL, chunk semantically.
"""
import os
import logging
import re
from typing import List, Dict
import aiohttp

logger = logging.getLogger(__name__)


class DocumentProcessor:
    def __init__(self, chunk_size: int = 800, chunk_overlap: int = 100):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    async def process_file(self, path: str) -> List[Dict]:
        ext = os.path.splitext(path)[1].lower()
        if ext == ".pdf":
            text = self._extract_pdf(path)
        elif ext == ".docx":
            text = self._extract_docx(path)
        elif ext in (".txt", ".md"):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        elif ext == ".csv":
            text = self._extract_csv(path)
        elif ext == ".html":
            text = self._extract_html_file(path)
        elif ext == ".json":
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        else:
            raise ValueError(f"Unsupported extension: {ext}")

        return self._chunk_text(text, source=os.path.basename(path))

    async def process_url(self, url: str) -> List[Dict]:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=30) as r:
                html = await r.text()
        text = self._html_to_text(html)
        return self._chunk_text(text, source=url)

    @staticmethod
    def _extract_pdf(path: str) -> str:
        import pypdf
        text_parts = []
        with open(path, "rb") as f:
            reader = pypdf.PdfReader(f)
            for page in reader.pages:
                text_parts.append(page.extract_text() or "")
        return "\n\n".join(text_parts)

    @staticmethod
    def _extract_docx(path: str) -> str:
        from docx import Document
        doc = Document(path)
        return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())

    @staticmethod
    def _extract_csv(path: str) -> str:
        import csv
        rows = []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.reader(f)
            headers = next(reader, [])
            for row in reader:
                pairs = [f"{h}: {v}" for h, v in zip(headers, row)]
                rows.append(" | ".join(pairs))
        return "\n".join(rows)

    @staticmethod
    def _extract_html_file(path: str) -> str:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return DocumentProcessor._html_to_text(f.read())

    @staticmethod
    def _html_to_text(html: str) -> str:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)

    def _chunk_text(self, text: str, source: str) -> List[Dict]:
        """Split into roughly chunk_size-token chunks with overlap, respecting paragraph boundaries."""
        # Approximate tokens with words * 1.3 — good enough for chunking
        text = re.sub(r"\n{3,}", "\n\n", text.strip())
        paragraphs = text.split("\n\n")

        chunks = []
        current = ""
        word_count = 0
        target_words = int(self.chunk_size / 1.3)
        overlap_words = int(self.chunk_overlap / 1.3)

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            para_words = len(para.split())
            if word_count + para_words <= target_words:
                current += para + "\n\n"
                word_count += para_words
            else:
                if current:
                    chunks.append(current.strip())
                # Carry over last `overlap_words` for context
                tail_words = current.split()[-overlap_words:] if overlap_words else []
                current = " ".join(tail_words) + "\n\n" + para + "\n\n" if tail_words else para + "\n\n"
                word_count = len(current.split())

        if current.strip():
            chunks.append(current.strip())

        return [
            {"text": c, "source": source, "chunk_index": i}
            for i, c in enumerate(chunks)
        ]
