"""Independent CPU embeddings and an isolated pgvector public-only corpus."""
from functools import lru_cache
import hashlib
import json
from pathlib import Path


class RetrievalUnavailable(Exception):
    pass


@lru_cache(maxsize=1)
def embedding_model(path, revision):
    if not path or not revision or not Path(path).is_dir():
        raise RetrievalUnavailable()
    from sentence_transformers import SentenceTransformer
    import torch
    torch.set_num_threads(2)
    model = SentenceTransformer(path, device="cpu", local_files_only=True, trust_remote_code=False)
    if model.get_embedding_dimension() != 384:
        raise RetrievalUnavailable()
    return model


class VectorRetrieval:
    def __init__(self, dsn, model_path, revision):
        self.dsn, self.model_path, self.revision = dsn, model_path, revision

    def search(self, question, docs, source_ids):
        if not self.dsn:
            raise RetrievalUnavailable()
        try:
            import psycopg2
            from pgvector.psycopg2 import register_vector
            model = embedding_model(self.model_path, self.revision)
            version = hashlib.sha256(json.dumps({"docs": docs, "model": self.revision, "chunker": "one-public-record-v1"}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            with psycopg2.connect(self.dsn, connect_timeout=3) as db:
                with db.cursor() as cur:
                    cur.execute("SELECT current_database(),current_user")
                    database, user = cur.fetchone()
                    if database not in ("product_retrieval", "product_retrieval_test") or user != "product_retrieval":
                        raise RetrievalUnavailable()
                    cur.execute("SET LOCAL statement_timeout='10000'")
                    cur.execute("SELECT pg_advisory_xact_lock(8372019)")
                    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                    register_vector(db)
                    cur.execute("CREATE TABLE IF NOT EXISTS public_chunks (id TEXT PRIMARY KEY, version TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL, text TEXT NOT NULL, embedding vector(384) NOT NULL)")
                    cur.execute("SELECT DISTINCT version FROM public_chunks")
                    versions = cur.fetchall()
                    if versions != [(version,)]:
                        vectors = model.encode(['passage: ' + d['text'] for d in docs], normalize_embeddings=True, show_progress_bar=False)
                        cur.execute("DELETE FROM public_chunks")
                        for doc, vector in zip(docs, vectors):
                            cur.execute("INSERT INTO public_chunks VALUES (%s,%s,%s,%s,%s,%s)", (doc['id'], version, doc['title'], doc['url'], doc['text'], vector))
                    vector = model.encode(['query: ' + question], normalize_embeddings=True, show_progress_bar=False)[0]
                    cur.execute("SELECT id,title,url,text,1-(embedding <=> %s) AS similarity FROM public_chunks WHERE version=%s AND id=ANY(%s) ORDER BY embedding <=> %s,id LIMIT 3", (vector, version, list(source_ids), vector))
                    rows = cur.fetchall()
                    return [{"id": r[0], "title": r[1], "url": r[2], "text": r[3], "similarity": float(r[4])} for r in rows if r[4] >= .65]
        except RetrievalUnavailable:
            raise
        except Exception:
            # Never expose DSN, source/user text or model/library bodies to HTTP/logs.
            raise RetrievalUnavailable() from None
