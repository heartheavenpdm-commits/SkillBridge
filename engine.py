"""WHSG-DRA engine (thesis Ch. 1.4). Pure functions: no database, no I/O."""
import math
from collections import deque

WEIGHTS = {"F": 0.30, "P": 0.40, "C": 0.30}
MASTERY_CUT = 80
DEVELOPING_CUT = 60
TARGET_LIMIT = 3


def band(p):
    if p >= MASTERY_CUT:
        return "Mastered"
    if p >= DEVELOPING_CUT:
        return "Developing"
    return "Needs Review"


def is_valid_attempt(answered, administered):
    return administered > 0 and 2 * answered >= administered


def _mean(values):
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def _downstream_influence(k, deficit, children):
    total, seen, queue = 0.0, set(), deque([(k, 0)])
    while queue:
        node, depth = queue.popleft()
        for child in children.get(node, ()):
            if child in seen:
                continue
            seen.add(child)
            total += deficit[child] * 0.5 ** (depth + 1)
            queue.append((child, depth + 1))
    return total


def analyze(answers, key, cats, skills, edges, modules, weights=None, distractor_map=None):
    """answers: one letter per item, '-' for blank. key: correct letters.
    cats: F/P/C per item. skills: [(name, module, subtopic, [item indices])].
    edges: [(prereq, dependent)] skill indices. distractor_map: {(item, letter): 'F'|'P'|'C'}.
    """
    if weights is None:
        weights = WEIGHTS
        
    n = len(key)
    if len(answers) != n:
        raise ValueError("answers must have one entry per item")
    corr = [answers[i] == key[i] for i in range(n)]
    sk = []
    
    # Unpack 4 values here to match the new subtopic structure
    for name, mod, sub, items in skills:
        num = den = 0.0
        err = {"F": 0, "P": 0, "C": 0, "U": 0}
        blank = 0
        for i in items:
            w = weights[cats[i]]
            den += w
            if corr[i]:
                num += w
            elif answers[i] == "-":
                blank += 1
            else:
                src = cats[i] if distractor_map is None else distractor_map.get((i, answers[i]))
                err[src or "U"] += 1
        sk.append({"name": name, "mod": mod, "sub": sub, "P": 100 * num / den, "err": err, "blank": blank})

    mods = [{"name": m, "P": _mean(s["P"] for s in sk if s["mod"] == j)} for j, m in enumerate(modules)]
    course = _mean(s["P"] for s in sk)
    children = {}
    for a, b in edges:
        children.setdefault(a, []).append(b)
    deficit = [max(0.0, MASTERY_CUT - s["P"]) for s in sk]
    influence = [_downstream_influence(k, deficit, children) for k in range(len(sk))]
    need = [deficit[k] + 0.5 * influence[k] for k in range(len(sk))]
    root = [deficit[k] + influence[k] for k in range(len(sk))]
    total_err = {c: sum(s["err"][c] for s in sk) for c in ("F", "P", "C", "U")}
    return {"raw": sum(corr), "sk": sk, "mods": mods, "course": course,
            "need": need, "root": root, "err": total_err,
            "blank": sum(s["blank"] for s in sk)}


def targets(result, limit=TARGET_LIMIT):
    idx = [k for k, v in enumerate(result["need"]) if v > 0]
    return sorted(idx, key=lambda k: -result["need"][k])[:limit]


def retest_items(tgt, skills_list):
    return sorted(i for k in tgt for i in skills_list[k][3])


def apply_retest(pre, retest, tgt):
    post = [retest["sk"][k]["P"] if k in tgt else s["P"] for k, s in enumerate(pre["sk"])]
    before = _mean(pre["sk"][k]["P"] for k in tgt)
    after = _mean(retest["sk"][k]["P"] for k in tgt)
    return {"post": post, "course": _mean(post), "before": before, "after": after, "gain": after - before}


def rank_resources(need, resources, top_n=4):
    nn = math.sqrt(sum(x * x for x in need))
    out = []
    for rid, title, cov in resources:
        rn = math.sqrt(sum(w * w for w in cov.values()))
        if nn == 0 or rn == 0:
            continue
        cosine = sum(need[k] * w for k, w in cov.items()) / (nn * rn)
        if cosine > 0:
            out.append({"id": rid, "title": title, "cosine": cosine,
                        "covers": max(cov, key=lambda k: need[k])})
    out.sort(key=lambda r: -r["cosine"])
    return out[:top_n]