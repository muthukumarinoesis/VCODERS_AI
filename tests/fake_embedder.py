"""Bag-of-words vectors: texts sharing words are close. Question words like
"what" and "which" are ignored, so rewordings of one question match."""

import hashlib
import math
import re

DIM = 768
IGNORED = {"what", "which", "is", "the", "of", "a", "an", "does", "say"}


class FakeEmbedder:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def embed(self, texts, task="document"):
        self.calls.append((list(texts), task))
        if self.fail:
            raise ConnectionError("embedding service down")
        return [self._vector(t) for t in texts]

    @staticmethod
    def _vector(text):
        v = [0.0] * DIM
        for word in re.findall(r"\w+", text.casefold()):
            if word not in IGNORED:
                v[int(hashlib.md5(word.encode()).hexdigest(), 16) % DIM] += 1
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]
