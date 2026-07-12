"""Single shared embedding model. Previously matcher.py AND explainer.py each
loaded their own SentenceTransformer (~2x memory, ~2x load time)."""
from functools import lru_cache
from sentence_transformers import SentenceTransformer
from .config import EMBED_MODEL


@lru_cache(maxsize=1)
def get_model():
    return SentenceTransformer(EMBED_MODEL)