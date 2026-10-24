import os
from functools import lru_cache
from typing import List
from fastembed import TextEmbedding
from app.config import get_settings


@lru_cache()
def get_embedding_model() -> TextEmbedding:
    """Loads and caches the FastEmbed TextEmbedding model on local CPU."""
    settings = get_settings()
    cache_dir = settings.cache_dir
    if cache_dir:
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except Exception:
            cache_dir = None

    return TextEmbedding(
        model_name=settings.embedding_model_name,
        cache_dir=cache_dir,
    )


def embed_texts(texts: List[str]) -> List[List[float]]:
    """Generates 384-dimensional dense vector embeddings for a list of texts."""
    if not texts:
        return []
    model = get_embedding_model()
    embeddings = list(model.embed(texts))
    return [vec.tolist() for vec in embeddings]


def embed_query(query: str) -> List[float]:
    """Generates an embedding vector for a single search query."""
    model = get_embedding_model()
    result = list(model.embed([query]))
    return result[0].tolist()
