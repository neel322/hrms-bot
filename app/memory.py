from datetime import datetime, timezone
import time
from typing import Any, Dict, List, Optional
import uuid
from qdrant_client.http import models

from app.config import get_settings
from app.embeddings import embed_texts
from app.utils import is_sensitive_memory
from app.vectorstore import (
    get_qdrant_client,
    search_user_memories,
    upsert_chunks,
)

# 5-minute cache for user non-sensitive memories
# Key: user_id -> (List[str], timestamp)
_USER_MEMORIES_CACHE: Dict[str, tuple[List[str], float]] = {}
USER_MEMORIES_CACHE_TTL = 300.0  # 5 minutes


def get_user_memories_cached(user_id: str, limit: int = 5) -> List[str]:
    """Retrieves cached non-sensitive memories for user_id (top 5, 5-minute TTL).

    Injects the cached memories directly, avoiding repeated embedding & vector search on every message.
    """
    now = time.time()
    clean_uid = user_id.strip().lower()
    if clean_uid in _USER_MEMORIES_CACHE:
        cached_facts, cached_ts = _USER_MEMORIES_CACHE[clean_uid]
        if now - cached_ts < USER_MEMORIES_CACHE_TTL:
            return cached_facts[:limit]
        else:
            del _USER_MEMORIES_CACHE[clean_uid]

    all_mems = list_user_memories(user_id=user_id, limit=20)
    safe_facts = [
        m["text"] for m in all_mems
        if m.get("text") and not is_sensitive_memory(m["text"])
    ]
    _USER_MEMORIES_CACHE[clean_uid] = (safe_facts, now)
    return safe_facts[:limit]


def invalidate_user_memories_cache(user_id: Optional[str] = None) -> None:
    """Invalidates the memory cache for a specific user or all users."""
    if user_id:
        _USER_MEMORIES_CACHE.pop(user_id.strip().lower(), None)
    else:
        _USER_MEMORIES_CACHE.clear()


def save_user_memory(
    user_id: str,
    fact: str,
    similarity_threshold: float = 0.92,
) -> Dict[str, Any]:
    """Saves a durable user memory fact, skipping near-duplicates (similarity > similarity_threshold).

    Returns a dict with status: 'saved' | 'skipped_duplicate' | 'rejected' and the fact details.
    """
    clean_fact = fact.strip()
    if not clean_fact:
        return {"status": "skipped_empty", "fact": fact}

    # Sensitivity check
    if is_sensitive_memory(clean_fact):
        return {
            "status": "rejected",
            "reason": "Sensitive financial, personal identity, health, or grievance details cannot be stored in durable vector memory.",
            "fact": clean_fact,
        }

    # Compute vector once to reuse for both duplicate check and upsert
    vector = embed_texts([clean_fact])[0]

    # Check for near-duplicates using search_user_memories with reused vector
    existing_matches = search_user_memories(
        query=clean_fact,
        top_k=1,
        user_id=user_id,
        query_vector=vector,
    )

    if existing_matches:
        top_match = existing_matches[0]
        if top_match["score"] >= similarity_threshold:
            return {
                "status": "skipped_duplicate",
                "fact": clean_fact,
                "existing_text": top_match["text"],
                "similarity": top_match["score"],
            }

    settings = get_settings()
    created_at = datetime.now(timezone.utc).isoformat()
    point_id = str(uuid.uuid4())

    point = models.PointStruct(
        id=point_id,
        vector=vector,
        payload={
            "user_id": user_id,
            "text": clean_fact,
            "created_at": created_at,
        },
    )

    upsert_chunks(settings.user_memories_collection, [point])
    invalidate_user_memories_cache(user_id)

    return {
        "status": "saved",
        "id": point_id,
        "fact": clean_fact,
        "created_at": created_at,
        "user_id": user_id,
    }


def search_memory(
    user_id: str,
    query: str,
    top_k: int = 3,
    query_vector: Optional[List[float]] = None,
) -> List[Dict[str, Any]]:
    """Searches user memories for the given user_id."""
    return search_user_memories(query=query, top_k=top_k, user_id=user_id, query_vector=query_vector)


def list_user_memories(user_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    """Returns all stored memories for a specific user_id."""
    settings = get_settings()
    client = get_qdrant_client()
    records, _ = client.scroll(
        collection_name=settings.user_memories_collection,
        scroll_filter=models.Filter(
            must=[
                models.FieldCondition(
                    key="user_id",
                    match=models.MatchValue(value=user_id),
                )
            ]
        ),
        limit=limit,
        with_payload=True,
    )
    results = []
    for rec in records:
        payload = rec.payload or {}
        results.append({
            "id": str(rec.id),
            "user_id": payload.get("user_id", user_id),
            "text": payload.get("text", ""),
            "created_at": payload.get("created_at"),
        })
    return results


def clear_user_memories(user_id: str) -> None:
    """Clears all memories for a specific user_id."""
    settings = get_settings()
    client = get_qdrant_client()
    client.delete(
        collection_name=settings.user_memories_collection,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="user_id",
                        match=models.MatchValue(value=user_id),
                    )
                ]
            )
        ),
    )
    invalidate_user_memories_cache(user_id)
