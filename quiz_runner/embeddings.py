"""Embeddings: gemini-embedding-001 at 768 dimensions, unit length.

The runner needs only `embed(texts, task)`, so tests and other providers can
pass any object with that method.
"""

import math

EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 768  # under pgvector's 2,000-dimension HNSW limit; Google's recommended smaller size
BATCH = 100
TASKS = {"document": "RETRIEVAL_DOCUMENT", "query": "RETRIEVAL_QUERY"}


def unit(vector) -> list[float]:
    """Google returns unit vectors only at 3,072 dimensions; normalise the rest."""
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


def cosine(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))  # both are unit vectors


def question_text(q: dict) -> str:
    """What a question means: its text plus its correct answer."""
    parts = [q["question"]]
    if "options" in q and isinstance(q.get("answer"), int):
        parts.append(q["options"][q["answer"]])
    elif "answers" in q:
        parts += [q["options"][i] for i in q["answers"]]
    elif "pairs" in q:
        parts += [f"{a} - {b}" for a, b in q["pairs"]]
    elif isinstance(q.get("answer"), (str, bool)):
        parts.append(str(q["answer"]))
    return "\n".join(parts)


class GeminiEmbedder:
    def __init__(self, api_key: str, model: str = EMBED_MODEL, dim: int = EMBED_DIM, timeout_s: float = 60):
        from google import genai
        from google.genai import types

        self._types = types
        self.model, self.dim = model, dim
        self.client = genai.Client(
            api_key=api_key, http_options=types.HttpOptions(timeout=int(timeout_s * 1000))
        )

    def embed(self, texts: list[str], task: str = "document") -> list[list[float]]:
        out = []
        for i in range(0, len(texts), BATCH):
            result = self.client.models.embed_content(
                model=self.model,
                contents=texts[i : i + BATCH],
                config=self._types.EmbedContentConfig(task_type=TASKS[task], output_dimensionality=self.dim),
            )
            out += [unit(e.values) for e in result.embeddings]
        return out
