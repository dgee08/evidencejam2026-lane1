import json, yaml
from tools.evalutil import toks, sim

gold = [e["sentence"] for e in yaml.safe_load(open("example/Murphy2005.yaml", encoding="utf-8"))["graphs"]]
g = json.load(open("output/articles/YKQQZV93.json", encoding="utf-8"))
ours = [e["original_sentence"] for e in g["edges"]]

matched, extra = set(), []
for o in ours:
    sims = [sim(toks(o), toks(x)) for x in gold]
    i = max(range(len(gold)), key=lambda k: sims[k])
    if sims[i] >= 0.5:
        matched.add(i)
    else:
        extra.append(o)

print(f"recall: {len(matched)}/{len(gold)} | our edges: {len(ours)} | not in key: {len(extra)}")
print("\nMISSED:")
for i, s in enumerate(gold):
    if i not in matched:
        print(" -", s[:150])
print("\nNOT IN KEY (check by hand):")
for s in extra:
    print(" -", s[:200])