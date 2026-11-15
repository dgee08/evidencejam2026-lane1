import re

GENERA = {"sanguinaria", "alliaria"}   # Murphy2005 only: the key expands "S." and "A." to full names

def toks(s):
    s = re.sub(r"<[^>]+>", "", s).replace("*", "").lower()
    return {t for t in re.findall(r"[a-z0-9]+", s) if len(t) > 1 and t not in GENERA}

def sim(a, b):
    return len(a & b) / max(1, len(a | b))

def best(sentence, pool):
    t = toks(sentence)
    return max((sim(t, toks(p)) for p in pool), default=0.0)