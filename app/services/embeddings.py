"""
app/services/embeddings.py

Wraps a small local embedding model used to embed both FAQ chunks (at
index time) and customer questions (at query time) for semantic search.

Uses fastembed (ONNX runtime) rather than sentence-transformers/torch --
much lighter to install and no GPU needed, which matters since this
runs on every app startup and per query. It's fully local: no API
calls, no rate limits, no dependency on a third-party provider being
up (unlike the HF Inference API used for chat completions).
"""

from fastembed import TextEmbedding

MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIM = 384

_model = None


def _get_model():
    global _model
    if _model is None:
        _model = TextEmbedding(model_name=MODEL_NAME)
    return _model


def embed_documents(texts):
    """Embed FAQ chunks for indexing. BGE models are trained with
    asymmetric query/document instructions -- passage_embed() is the
    fastembed API for the document side. For BAAI/bge-small-en-v1.5
    specifically, fastembed doesn't currently apply a different prefix
    than plain embed(), so this is equivalent today, but using the
    named method keeps this correct automatically if that changes, or
    if MODEL_NAME above is swapped for one where it does matter."""
    return [vector.tolist() for vector in _get_model().passage_embed(texts)]


def embed_query(text):
    """Embed a customer's question for search (see embed_documents)."""
    return next(iter(_get_model().query_embed([text]))).tolist()
