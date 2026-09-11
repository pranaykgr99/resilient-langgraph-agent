"""Persisted lexical embeddings: SQLite storage, TF-IDF + cosine retrieval."""

import hashlib
import json
import sqlite3
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


class LocalVectorStore:
    def __init__(self, directory):
        source = Path(__file__).with_name("documents.json").read_text()
        self.docs = json.loads(source)
        fingerprint = hashlib.sha256(source.encode()).hexdigest()
        Path(directory).mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(Path(directory) / "knowledge.sqlite")) as db:
            db.execute("CREATE TABLE IF NOT EXISTS embeddings (version TEXT PRIMARY KEY, payload TEXT)")
            row = db.execute("SELECT payload FROM embeddings WHERE version=?", (fingerprint,)).fetchone()
            if row:
                saved = json.loads(row[0])
                self.vectorizer = TfidfVectorizer(stop_words="english", vocabulary=saved["vocabulary"])
                self.vectorizer.idf_ = np.array(saved["idf"])
                self.vectors = np.array(saved["vectors"])
            else:
                self.vectorizer = TfidfVectorizer(stop_words="english")
                self.vectors = self.vectorizer.fit_transform([d["text"] for d in self.docs]).toarray()
                saved = {
                    "vocabulary": self.vectorizer.vocabulary_,
                    "idf": self.vectorizer.idf_.tolist(),
                    "vectors": self.vectors.tolist(),
                }
                db.execute("INSERT OR REPLACE INTO embeddings VALUES (?,?)", (fingerprint, json.dumps(saved)))

    def search(self, query):
        scores = cosine_similarity(self.vectorizer.transform([query]), self.vectors)[0]
        hits = sorted(zip(scores, self.docs), key=lambda x: x[0], reverse=True)[:3]
        return [dict(doc, score=round(float(score), 4)) for score, doc in hits if score >= 0.12]
