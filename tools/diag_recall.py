import re, yaml
from pathlib import Path
from pipeline import selected_sentences
from tools.evalutil import best

gold = [e["sentence"] for e in yaml.safe_load(open("example/Murphy2005.yaml", encoding="utf-8"))["graphs"]]
text = Path("corpus/YKQQZV93.md").read_text(encoding="utf-8")
allsents = [s for para in re.split(r"\n\s*\n", text)
            for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", para.replace("\n", " ")) if len(s) > 30]
sel = selected_sentences(text)
for g in gold:
    a, b = best(g, allsents), best(g, sel)
    stage = "ok" if b >= 0.5 else ("LOST by filter" if a >= 0.5 else "not found in article")
    print(f"{stage:20} all={a:.2f} sel={b:.2f} | {g[:80]}")