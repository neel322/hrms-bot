from collections import OrderedDict
import logging
import os
import time
import warnings
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple
from qdrant_client import QdrantClient
from qdrant_client.http import models

from app.config import get_settings
from app.embeddings import embed_query

logger = logging.getLogger("hrms-bot.vectorstore")
warnings.filterwarnings("ignore", category=UserWarning, module="qdrant_client")

_ACTIVE_MODE: Optional[str] = None


@lru_cache()
def get_qdrant_client() -> QdrantClient:
    """Initializes and returns the Qdrant client.

    If QDRANT_URL is set and not empty, connects to Qdrant Cloud or remote cluster with prefer_grpc=True.
    Otherwise, falls back to local disk storage mode at settings.qdrant_local_path.
    """
    global _ACTIVE_MODE
    settings = get_settings()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if settings.qdrant_url and settings.qdrant_url.strip():
            _ACTIVE_MODE = f"Cloud/Remote ({settings.qdrant_url}) [prefer_grpc=True]"
            logger.info(f"Qdrant active mode: {_ACTIVE_MODE}")
            return QdrantClient(
                url=settings.qdrant_url.strip(),
                api_key=settings.qdrant_api_key.strip() if settings.qdrant_api_key else None,
                prefer_grpc=True,
                timeout=60,
            )

        # Local mode fallback
        os.makedirs(settings.qdrant_local_path, exist_ok=True)
        _ACTIVE_MODE = f"Embedded Local (path={settings.qdrant_local_path})"
        logger.info(f"Qdrant active mode: {_ACTIVE_MODE}")
        return QdrantClient(path=settings.qdrant_local_path, timeout=60)


def get_qdrant_mode() -> str:
    """Returns a string describing the active Qdrant mode."""
    global _ACTIVE_MODE
    if _ACTIVE_MODE is None:
        get_qdrant_client()
    return _ACTIVE_MODE or "Embedded Local"


def init_vectorstore() -> None:
    """Initializes vectorstore collections: hr_policies and user_memories (384 dims, Cosine)."""
    settings = get_settings()
    client = get_qdrant_client()

    collections = [
        settings.hr_policies_collection,
        settings.user_memories_collection,
    ]

    existing_collections_res = client.get_collections()
    existing_names = {c.name for c in existing_collections_res.collections}

    for col in collections:
        if col not in existing_names:
            client.create_collection(
                collection_name=col,
                vectors_config=models.VectorParams(
                    size=settings.embedding_dim,
                    distance=models.Distance.COSINE,
                ),
            )

    # Ensure payload index for user_id in user_memories
    try:
        client.create_payload_index(
            collection_name=settings.user_memories_collection,
            field_name="user_id",
            field_schema=models.PayloadSchemaType.KEYWORD,
        )
    except Exception:
        pass

    # Ensure payload index for source in hr_policies
    try:
        client.create_payload_index(
            collection_name=settings.hr_policies_collection,
            field_name="source",
            field_schema=models.PayloadSchemaType.KEYWORD,
        )
    except Exception:
        pass


# Policy Search LRU Cache (200 entries, 10-minute / 600s TTL)
# Key: (normalized_query, top_k) -> (results, timestamp)
_POLICY_SEARCH_CACHE: OrderedDict[Tuple[str, int], Tuple[List[Dict[str, Any]], float]] = OrderedDict()
POLICY_CACHE_MAX_ENTRIES = 200
POLICY_CACHE_TTL = 600.0  # 10 minutes


def clear_policy_cache() -> None:
    """Clears the policy search LRU cache. Called after any ingest or document modification."""
    _POLICY_SEARCH_CACHE.clear()
    logger.info("Cleared policy search LRU cache.")


class QdrantTimeout(Exception):
    """Raised when a Qdrant vectorstore operation times out."""
    pass


def upsert_chunks(
    collection_name: str,
    points: List[models.PointStruct],
) -> None:
    """Upserts points into the specified Qdrant collection in batches of 64 with wait=True and up to 3 retries."""
    if not points:
        return
    client = get_qdrant_client()
    batch_size = 64
    for i in range(0, len(points), batch_size):
        batch = points[i : i + batch_size]
        max_retries = 3
        for attempt in range(max_retries):
            try:
                client.upsert(
                    collection_name=collection_name,
                    points=batch,
                    wait=True,
                )
                break
            except Exception as exc:
                is_timeout = (
                    isinstance(exc, (TimeoutError, QdrantTimeout))
                    or "timed out" in str(exc).lower()
                    or "timeout" in str(exc).lower()
                )
                if attempt < max_retries - 1:
                    backoff = 0.5 * (2 ** attempt)
                    logger.warning(
                        f"Qdrant upsert batch failed (attempt {attempt + 1}/{max_retries}), retrying in {backoff}s: {exc}"
                    )
                    time.sleep(backoff)
                else:
                    logger.error(f"Qdrant upsert batch failed after {max_retries} attempts: {exc}")
                    if is_timeout:
                        raise QdrantTimeout(f"Qdrant upsert timed out after {max_retries} attempts: {exc}") from exc
                    raise

    settings = get_settings()
    if collection_name == settings.hr_policies_collection:
        clear_policy_cache()


def delete_chunks_by_source(collection_name: str, source: str) -> None:
    """Deletes all points in collection_name whose payload 'source' equals source."""
    if not source:
        return
    client = get_qdrant_client()
    try:
        client.delete(
            collection_name=collection_name,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="source",
                            match=models.MatchValue(value=source),
                        )
                    ]
                )
            ),
        )
        settings = get_settings()
        if collection_name == settings.hr_policies_collection:
            clear_policy_cache()
    except Exception as exc:
        warnings.warn(f"Failed to delete chunks for source '{source}' in collection '{collection_name}': {exc}")


def search_policies(
    query: str,
    top_k: int = 5,
    query_vector: Optional[List[float]] = None,
) -> List[Dict[str, Any]]:
    """Searches the `hr_policies` collection for matching policy chunks.

    Uses an LRU cache (200 entries, 10-min TTL) keyed by normalized query.
    Reuses query_vector if passed.
    """
    settings = get_settings()
    normalized_query = " ".join(query.strip().lower().split())
    cache_key = (normalized_query, top_k)

    now = time.time()
    if cache_key in _POLICY_SEARCH_CACHE:
        cached_results, cached_time = _POLICY_SEARCH_CACHE[cache_key]
        if now - cached_time < POLICY_CACHE_TTL:
            # Move to end for LRU order
            _POLICY_SEARCH_CACHE.move_to_end(cache_key)
            return cached_results
        else:
            del _POLICY_SEARCH_CACHE[cache_key]

    t0 = time.perf_counter()
    client = get_qdrant_client()
    if query_vector is None:
        query_vector = embed_query(query)

    results = client.query_points(
        collection_name=settings.hr_policies_collection,
        query=query_vector,
        limit=top_k,
        with_payload=True,
    )
    search_ms = (time.perf_counter() - t0) * 1000
    logger.info(f"Qdrant search in '{settings.hr_policies_collection}' completed in {search_ms:.1f}ms (mode: {get_qdrant_mode()})")

    formatted_results = []
    for pt in results.points:
        payload = pt.payload or {}
        res_dict = {
            "id": str(pt.id),
            "text": payload.get("text", ""),
            "source": payload.get("source", ""),
            "section": payload.get("section", "General"),
            "chunk_index": payload.get("chunk_index", 0),
            "score": pt.score,
        }
        if "page" in payload and payload["page"] is not None:
            res_dict["page"] = payload["page"]
        formatted_results.append(res_dict)

    # Cache result in LRU cache
    if len(_POLICY_SEARCH_CACHE) >= POLICY_CACHE_MAX_ENTRIES:
        _POLICY_SEARCH_CACHE.popitem(last=False)
    _POLICY_SEARCH_CACHE[cache_key] = (formatted_results, now)

    return formatted_results


def search_user_memories(
    query: str,
    top_k: int = 5,
    user_id: Optional[str] = None,
    query_vector: Optional[List[float]] = None,
) -> List[Dict[str, Any]]:
    """Searches the `user_memories` collection for matching chunks, optionally filtered by user_id."""
    settings = get_settings()
    client = get_qdrant_client()

    t0 = time.perf_counter()
    if query_vector is None:
        query_vector = embed_query(query)

    query_filter = None
    if user_id:
        query_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="user_id",
                    match=models.MatchValue(value=user_id),
                )
            ]
        )

    results = client.query_points(
        collection_name=settings.user_memories_collection,
        query=query_vector,
        query_filter=query_filter,
        limit=top_k,
        with_payload=True,
    )
    search_ms = (time.perf_counter() - t0) * 1000
    logger.info(f"Qdrant search in '{settings.user_memories_collection}' completed in {search_ms:.1f}ms (mode: {get_qdrant_mode()})")

    formatted_results = []
    for pt in results.points:
        payload = pt.payload or {}
        formatted_results.append({
            "id": str(pt.id),
            "text": payload.get("text", ""),
            "user_id": payload.get("user_id", ""),
            "created_at": payload.get("created_at"),
            "score": pt.score,
        })
    return formatted_results
