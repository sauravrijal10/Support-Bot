"""
app/services/vector_knowledge_base.py

Semantic (embedding-based) retrieval over the FAQ docs, backed by
pgvector, replacing knowledge_base.py's TF-IDF keyword matching.
TF-IDF only matches literal word overlap, so a question phrased very
differently from the docs' wording could miss the right chunk; cosine
similarity over embeddings catches that by meaning instead.
"""

import psycopg2
import psycopg2.extras
from pgvector.psycopg2 import register_vector

from app.services.document_loader import load_chunks
from app.services.embeddings import embed_documents, embed_query


class VectorKnowledgeBase:
    def __init__(self, database_url):
        self.database_url = database_url

    def _ensure_schema(self):
        """Create the pgvector extension and documents table if they
        don't exist yet. Self-provisioned here (rather than relying on
        the Alembic migrations) so the app works on a fresh database
        with no manual setup step beyond DATABASE_URL pointing at
        Postgres. The "vector" type must exist before register_vector()
        can be called on a connection, so this runs on a plain
        connection before _connect() is used anywhere else."""
        conn = psycopg2.connect(self.database_url)
        try:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS documents (
                        id SERIAL PRIMARY KEY,
                        source TEXT NOT NULL,
                        content TEXT NOT NULL,
                        embedding VECTOR(384) NOT NULL
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS documents_embedding_idx "
                    "ON documents USING hnsw (embedding vector_cosine_ops)"
                )
            conn.commit()
        finally:
            conn.close()

    def _connect(self):
        conn = psycopg2.connect(self.database_url)
        register_vector(conn)
        return conn

    def load_folder(self, folder_path):
        """Chunk every .md/.txt file in folder_path and (re)index them
        into pgvector. Re-indexes from scratch each call so the table
        always matches what's currently in the folder."""
        self._ensure_schema()
        chunks, sources = load_chunks(folder_path)
        if not chunks:
            return

        embeddings = embed_documents(chunks)

        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM documents")
            for source, content, vector in zip(sources, chunks, embeddings):
                cur.execute(
                    "INSERT INTO documents (source, content, embedding) "
                    "VALUES (%s, %s, %s)",
                    (source, content, vector),
                )

    def retrieve(self, query, k=3):
        """Return the top-k most semantically similar chunks for a query."""
        query_vector = embed_query(query)
        with self._connect() as conn, conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(
                """
                SELECT source, content,
                       1 - (embedding <=> %s::vector) AS score
                FROM documents
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (query_vector, query_vector, k),
            )
            rows = cur.fetchall()

        return [
            {"text": row["content"], "source": row["source"], "score": float(row["score"])}
            for row in rows
        ]
