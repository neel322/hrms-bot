import hashlib
import io
import re
from typing import Any, Dict, List, Optional, Tuple
from pypdf import PdfReader
from qdrant_client.http import models

from app.config import get_settings
from app.embeddings import embed_texts
from app.vectorstore import delete_chunks_by_source, upsert_chunks


import logging

logger = logging.getLogger("hrms-bot.ingest")


class IngestionError(Exception):
    """Base class for ingestion errors."""
    pass


class PdfEncrypted(IngestionError):
    pass


class PdfNoText(IngestionError):
    pass


class PdfCorrupt(IngestionError):
    pass


class EmptyDocument(IngestionError):
    pass


class ExtractResult(tuple):
    """Container for extract_text_and_pages result: behaves like (text, pages) tuple."""
    def __new__(cls, text: str, pages: int, pages_data: Optional[List[Dict[str, Any]]] = None):
        res = super().__new__(cls, (text, pages))
        res._pages_data = pages_data or []
        return res

    @property
    def text(self) -> str:
        return self[0]

    @property
    def pages(self) -> int:
        return self[1]

    @property
    def pages_data(self) -> List[Dict[str, Any]]:
        return self._pages_data


class IngestionResult(tuple):
    """Container for ingestion result, behaves like (chunks, pages) and an int (chunks)."""
    def __new__(cls, chunks: int, pages: int = 1, pages_data: Optional[List[Dict[str, Any]]] = None):
        res = super().__new__(cls, (chunks, pages))
        res._pages_data = pages_data or []
        return res

    @property
    def chunks(self) -> int:
        return self[0]

    @property
    def pages(self) -> int:
        return self[1]

    @property
    def pages_data(self) -> List[Dict[str, Any]]:
        return self._pages_data

    def __int__(self) -> int:
        return self[0]

    def __add__(self, other):
        return self[0] + other

    def __radd__(self, other):
        return other + self[0]

    def __eq__(self, other):
        if isinstance(other, IngestionResult):
            return super().__eq__(other)
        return self[0] == other

    def __lt__(self, other):
        return self[0] < (other[0] if isinstance(other, IngestionResult) else other)

    def __le__(self, other):
        return self[0] <= (other[0] if isinstance(other, IngestionResult) else other)

    def __gt__(self, other):
        return self[0] > (other[0] if isinstance(other, IngestionResult) else other)

    def __ge__(self, other):
        return self[0] >= (other[0] if isinstance(other, IngestionResult) else other)


def generate_deterministic_id(source: str, chunk_index: int) -> str:
    """Generates a deterministic MD5 hex string based on source and chunk_index."""
    unique_key = f"{source}:{chunk_index}"
    return hashlib.md5(unique_key.encode("utf-8")).hexdigest()


def create_virtual_pages(text: str, target_size: int = 3000) -> List[Dict[str, Any]]:
    """Splits markdown or text into virtual pages of about target_size characters at paragraph or heading boundaries."""
    if not text or not text.strip():
        return []

    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text).strip()
    if not cleaned:
        return []

    # Split by headings or paragraphs: double newlines or heading starts
    raw_blocks = re.split(r"(?:\n\s*\n|(?<=\n)(?=#+\s))", cleaned)
    blocks = [b.strip() for b in raw_blocks if b.strip()]

    pages: List[str] = []
    current_blocks: List[str] = []
    current_len = 0

    for b in blocks:
        b_len = len(b)
        sep_len = 2 if current_blocks else 0
        if current_len + sep_len + b_len <= target_size or not current_blocks:
            current_blocks.append(b)
            current_len += sep_len + b_len
        else:
            pages.append("\n\n".join(current_blocks).strip())
            current_blocks = [b]
            current_len = b_len

    if current_blocks:
        pages.append("\n\n".join(current_blocks).strip())

    final_pages: List[str] = []
    for p in pages:
        if len(p) <= target_size * 1.5 or len(p) <= target_size:
            final_pages.append(p)
        else:
            for s_idx in range(0, len(p), target_size):
                chunk = p[s_idx : s_idx + target_size].strip()
                if chunk:
                    final_pages.append(chunk)

    if not final_pages:
        final_pages = [cleaned]

    return [
        {
            "page_number": idx + 1,
            "text": p_text,
            "char_count": len(p_text),
        }
        for idx, p_text in enumerate(final_pages)
    ]


def extract_text_and_pages(file_bytes: bytes, filename: str) -> ExtractResult:
    """Extracts raw text and page count from PDF, Markdown, or plain text bytes.

    Extracts each PDF page inside try/except, skips bad pages, strips NUL and control
    characters, and attempts empty-password decryption for encrypted PDFs.
    Raises typed errors on failures. Returns ExtractResult(text, pages, pages_data).
    """
    if not file_bytes or (isinstance(file_bytes, (bytes, bytearray)) and len(file_bytes.strip()) == 0):
        lower_name = filename.lower()
        if lower_name.endswith(".pdf"):
            raise PdfCorrupt("Empty PDF byte stream.")
        raise EmptyDocument("Empty document bytes.")

    lower_name = filename.lower()
    if lower_name.endswith(".pdf"):
        try:
            reader = PdfReader(io.BytesIO(file_bytes))
        except Exception as exc:
            raise PdfCorrupt(f"Corrupt or invalid PDF: {exc}") from exc

        if reader.is_encrypted:
            try:
                decrypt_res = reader.decrypt("")
                if not decrypt_res:
                    raise PdfEncrypted("The PDF is encrypted with a password.")
            except Exception as exc:
                if isinstance(exc, PdfEncrypted):
                    raise
                raise PdfEncrypted(f"The PDF is encrypted: {exc}") from exc

        try:
            total_pages = len(reader.pages)
        except Exception as exc:
            if "decrypt" in str(exc).lower():
                raise PdfEncrypted(f"The PDF is encrypted: {exc}") from exc
            raise PdfCorrupt(f"Corrupt PDF structure: {exc}") from exc

        if total_pages == 0:
            raise PdfNoText("PDF has 0 pages.")

        pages_data: List[Dict[str, Any]] = []
        bad_pages = 0

        for idx, page in enumerate(reader.pages, start=1):
            cleaned = ""
            try:
                extracted = page.extract_text()
                if extracted:
                    # Strip NUL (\x00) and control characters except \t (\x09) and \n (\x0a)
                    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", extracted).strip()
            except Exception as page_exc:
                bad_pages += 1
                logger.warning(f"Skipping bad page in {filename}: {page_exc}")

            pages_data.append({
                "page_number": idx,
                "text": cleaned,
                "char_count": len(cleaned),
            })

        full_text = "\n\n".join(p["text"] for p in pages_data if p["text"]).strip()
        if not full_text:
            raise PdfNoText("No readable text found. This looks like a scanned PDF; upload a text-based PDF or OCR it first.")

        return ExtractResult(full_text, total_pages, pages_data)

    # Non-PDF documents (Markdown, Plain Text)
    try:
        decoded = file_bytes.decode("utf-8", errors="replace")
    except Exception as exc:
        raise EmptyDocument(f"Failed to decode text document: {exc}") from exc

    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", decoded).strip()
    if not cleaned:
        raise EmptyDocument("Document contains no readable text.")

    pages_data = create_virtual_pages(cleaned, target_size=3000)
    return ExtractResult(cleaned, len(pages_data), pages_data)


def extract_text_from_bytes(file_bytes: bytes, filename: str) -> str:
    """Extracts raw text from PDF, Markdown, or plain text bytes."""
    text, _ = extract_text_and_pages(file_bytes, filename)
    return text


def split_into_sentences(text: str) -> List[str]:
    """Splits text into sentences or logical units while preserving structure."""
    normalized = re.sub(r"\r\n", "\n", text)
    raw_sentences = re.split(r"(?<=[.!?\n])\s+", normalized)
    return [s.strip() for s in raw_sentences if s.strip()]


def parse_markdown_sections(text: str) -> List[Tuple[str, str]]:
    """Splits markdown text into blocks associated with their nearest heading hierarchy.

    Returns a list of (section_hierarchy, block_text) tuples.
    Example section_hierarchy: 'Acme Technologies - Leave Policy > 2. Leave Categories > 2.3 Earned Leave (EL)'
    """
    lines = text.split("\n")
    h1 = ""
    h2 = ""
    h3 = ""

    sections: List[Tuple[str, str]] = []
    current_lines: List[str] = []

    def get_current_section() -> str:
        parts = [p for p in [h1, h2, h3] if p]
        return " > ".join(parts) if parts else "General"

    for line in lines:
        stripped = line.strip()
        h1_match = re.match(r"^#\s+(.+)$", stripped)
        h2_match = re.match(r"^##\s+(.+)$", stripped)
        h3_match = re.match(r"^###\s+(.+)$", stripped)

        if h1_match or h2_match or h3_match:
            if current_lines:
                block_content = "\n".join(current_lines).strip()
                if block_content:
                    sections.append((get_current_section(), block_content))
                current_lines = []

            if h1_match:
                h1 = h1_match.group(1).strip()
                h2 = ""
                h3 = ""
            elif h2_match:
                h2 = h2_match.group(1).strip()
                h3 = ""
            elif h3_match:
                h3 = h3_match.group(1).strip()
        else:
            current_lines.append(line)

    if current_lines:
        block_content = "\n".join(current_lines).strip()
        if block_content:
            sections.append((get_current_section(), block_content))

    if not sections:
        sections.append(("General", text.strip()))

    return sections


def chunk_text(
    text: str,
    target_size: int = 500,
    overlap: int = 50,
) -> List[str]:
    """Chunks text into segments of at most target_size chars with overlap.

    Guarantees:
    - No chunk exceeds target_size characters under any circumstance.
    - Sentences longer than target_size are cleanly divided along word boundaries.
    """
    if not text:
        return []

    sentences = split_into_sentences(text)
    if not sentences:
        return []

    # Break any sentences longer than target_size along word boundaries
    refined_sentences: List[str] = []
    for s in sentences:
        if len(s) <= target_size:
            refined_sentences.append(s)
        else:
            words = s.split()
            current_piece: List[str] = []
            current_len = 0
            for w in words:
                # If word itself is longer than target_size, hard truncate or slice
                if len(w) > target_size:
                    if current_piece:
                        refined_sentences.append(" ".join(current_piece))
                        current_piece = []
                        current_len = 0
                    for chunk_start in range(0, len(w), target_size):
                        refined_sentences.append(w[chunk_start:chunk_start + target_size])
                    continue

                piece_len = len(w) + (1 if current_piece else 0)
                if current_len + piece_len > target_size and current_piece:
                    refined_sentences.append(" ".join(current_piece))
                    current_piece = [w]
                    current_len = len(w)
                else:
                    current_piece.append(w)
                    current_len += piece_len
            if current_piece:
                refined_sentences.append(" ".join(current_piece))

    chunks: List[str] = []
    current_chunk_sentences: List[str] = []
    current_chunk_len = 0

    for sentence in refined_sentences:
        sentence_len = len(sentence)
        sep_len = 1 if current_chunk_sentences else 0

        # Check if adding this sentence exceeds target_size
        if current_chunk_len + sep_len + sentence_len <= target_size:
            current_chunk_sentences.append(sentence)
            current_chunk_len += sep_len + sentence_len
        else:
            if current_chunk_sentences:
                chunks.append(" ".join(current_chunk_sentences))

            # Build overlap context from trailing sentences, ensuring it never causes
            # the new chunk + sentence to exceed target_size
            overlap_sentences: List[str] = []
            overlap_accum = 0
            max_allowed_overlap = min(overlap, target_size - sentence_len - 1) if target_size > sentence_len else 0

            if max_allowed_overlap > 0:
                for prev_s in reversed(current_chunk_sentences):
                    prev_len = len(prev_s) + (1 if overlap_sentences else 0)
                    if overlap_accum + prev_len <= max_allowed_overlap:
                        overlap_sentences.insert(0, prev_s)
                        overlap_accum += prev_len
                    else:
                        break

            current_chunk_sentences = overlap_sentences + [sentence]
            current_chunk_len = sum(len(s) for s in current_chunk_sentences) + (len(current_chunk_sentences) - 1)

    if current_chunk_sentences:
        chunks.append(" ".join(current_chunk_sentences))

    # Defensive check: ensure every chunk strictly satisfies <= target_size
    valid_chunks: List[str] = []
    for c in chunks:
        if len(c) <= target_size:
            valid_chunks.append(c)
        else:
            # Slices if edge case somehow exceeded
            for start_idx in range(0, len(c), target_size):
                valid_chunks.append(c[start_idx:start_idx + target_size])

    return valid_chunks


def chunk_document_with_sections(
    raw_text: str,
    target_size: int = 500,
    overlap: int = 50,
    pages_data: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Chunks a document into section-aware chunks.

    Prepends the nearest heading hierarchy (e.g. 'Leave Policy > Carry Forward') to each
    chunk's embedded text, while ensuring the combined text never exceeds target_size.
    If pages_data is provided, assigns the starting page number to each chunk.
    Returns list of dicts: [{'text': prepended_chunk, 'section': section_hierarchy, 'page': int (optional)}]
    """
    sections = parse_markdown_sections(raw_text)
    output_chunks: List[Dict[str, Any]] = []

    search_page_idx = 0
    for section_name, section_text in sections:
        # Calculate prefix to prepend
        prefix = f"{section_name}\n" if section_name and section_name != "General" else ""
        prefix_len = len(prefix)

        # Allocate body budget so prefix + body <= target_size
        body_budget = max(100, target_size - prefix_len)

        body_chunks = chunk_text(
            text=section_text,
            target_size=body_budget,
            overlap=min(overlap, 40),
        )

        for b_chunk in body_chunks:
            full_chunk_text = f"{prefix}{b_chunk}".strip()
            # Guarantee strict cap
            if len(full_chunk_text) > target_size:
                full_chunk_text = full_chunk_text[:target_size].rstrip()

            chunk_dict: Dict[str, Any] = {
                "text": full_chunk_text,
                "section": section_name,
            }

            if pages_data:
                first_words = " ".join(b_chunk.split()[:5]).strip()
                matched_page = None
                if first_words:
                    for p_idx in range(search_page_idx, len(pages_data)):
                        p = pages_data[p_idx]
                        if first_words in p.get("text", ""):
                            matched_page = p.get("page_number", 1)
                            search_page_idx = p_idx
                            break
                    if matched_page is None:
                        for p in pages_data:
                            if first_words in p.get("text", ""):
                                matched_page = p.get("page_number", 1)
                                break
                chunk_dict["page"] = matched_page if matched_page is not None else 1

            output_chunks.append(chunk_dict)

    return output_chunks


def process_and_ingest_document(
    file_bytes: bytes,
    filename: str,
    source: Optional[str] = None,
    collection_name: Optional[str] = None,
    replace: bool = False,
) -> IngestionResult:
    """Extracts, chunks with section headings, embeds in batches of 64, and upserts a document to Qdrant.

    Defaults to `hr_policies` collection.
    Order for replace=True: extract, chunk and embed FIRST; only then delete old chunks and old pages and upsert new ones.
    Payload: {text, source, chunk_index, section, page (optional)}.
    Deterministic ID: MD5 hash of f"{source}:{chunk_index}".
    Returns IngestionResult(chunks, pages, pages_data).
    """
    settings = get_settings()
    target_collection = collection_name or settings.hr_policies_collection
    doc_source = source or filename

    # 1. Extract text and page count (raises PdfEncrypted, PdfNoText, PdfCorrupt, EmptyDocument)
    ext_res = extract_text_and_pages(file_bytes, filename)
    raw_text = ext_res[0]
    pages = ext_res[1]
    pages_data = getattr(ext_res, "pages_data", None) or []

    is_pdf = filename.lower().endswith(".pdf")

    # 2. Chunk text (with page tracking for PDFs)
    chunk_items = chunk_document_with_sections(
        raw_text=raw_text,
        target_size=settings.chunk_size,
        overlap=settings.chunk_overlap,
        pages_data=pages_data if is_pdf else None,
    )

    if not chunk_items:
        lower_name = filename.lower()
        if lower_name.endswith(".pdf"):
            raise PdfNoText("No readable text chunks generated from PDF.")
        raise EmptyDocument("No readable text chunks generated from document.")

    # 3. Embed in batches of 64
    texts_to_embed = [item["text"] for item in chunk_items]
    embeddings: List[List[float]] = []
    EMBED_BATCH_SIZE = 64
    for i in range(0, len(texts_to_embed), EMBED_BATCH_SIZE):
        batch_texts = texts_to_embed[i : i + EMBED_BATCH_SIZE]
        batch_vecs = embed_texts(batch_texts)
        embeddings.extend(batch_vecs)

    # 4. Build points
    points: List[models.PointStruct] = []
    for idx, (item, vector) in enumerate(zip(chunk_items, embeddings)):
        point_id = generate_deterministic_id(doc_source, idx)
        payload = {
            "text": item["text"],
            "source": doc_source,
            "chunk_index": idx,
            "section": item.get("section", "General"),
        }
        if "page" in item and item["page"] is not None:
            payload["page"] = item["page"]

        points.append(
            models.PointStruct(
                id=point_id,
                vector=vector,
                payload=payload,
            )
        )

    # 5. For replace=True: ONLY delete old chunks and old pages after successful extract, chunk, and embed!
    from app.db import delete_policy_pages, record_policy_pages
    if replace:
        delete_chunks_by_source(target_collection, doc_source)
        delete_policy_pages(doc_source)

    # 6. Upsert the new points and record pages
    upsert_chunks(target_collection, points)
    if pages_data:
        record_policy_pages(doc_source, pages_data)

    return IngestionResult(chunks=len(points), pages=pages, pages_data=pages_data)
