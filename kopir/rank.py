"""BM25 over small in-memory corpora. Deterministic, no index on disk."""
import math
from collections import Counter

from .textnorm import tokens

K1 = 1.2
B = 0.75
REF_DOCS = 250


class BM25:
    def __init__(self, docs: list[list[str]]):
        self.tf = [Counter(d) for d in docs]
        self.len = [len(d) for d in docs]
        self.avg = (sum(self.len) / len(docs)) if docs else 0.0
        df = Counter()
        for c in self.tf:
            df.update(c.keys())
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def score(self, query: list[str]) -> list[tuple[float, int, int]]:
        """(score, distinct matched terms, doc index), best first."""
        q = set(query)
        out = []
        for i, c in enumerate(self.tf):
            s, hit = 0.0, 0
            norm = K1 * (1 - B + B * self.len[i] / (self.avg or 1))
            for t in q:
                f = c.get(t)
                if not f:
                    continue
                hit += 1
                s += self.idf[t] * f * (K1 + 1) / (f + norm)
            if hit:
                out.append((s, hit, i))
        out.sort(key=lambda x: (-x[0], x[2]))
        return out


def search(query: str, docs: list[list[str]], *, limit: int, min_score: float,
           min_hits: int) -> list[tuple[float, int]]:
    """Ranked (score, index) pairs that clear the thresholds.

    A single shared word is rarely a real match in a corpus this mixed, so a
    hit needs `min_hits` distinct query terms unless the query itself is that
    short.
    """
    q = tokens(query)
    if not q or not docs:
        return []
    need = min(min_hits, len(set(q)))
    # IDF, and so every score, grows with the corpus; `min_score` is tuned
    # for a few hundred records and scaled down for smaller ones.
    floor = min_score * min(1.0, math.log1p(len(docs)) / math.log1p(REF_DOCS))
    ranked = BM25(docs).score(q)
    if not ranked:
        return []
    top = ranked[0][0]
    out = []
    for s, hit, i in ranked:
        if s < floor or hit < need or s < 0.4 * top:
            continue
        out.append((s, i))
        if len(out) >= limit:
            break
    return out
