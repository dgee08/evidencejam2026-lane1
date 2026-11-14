import re
from pathlib import Path
from pipeline import CUES, SKIP_HEAD
from evalutil import toks, sim

LOST = [
    "Although early- and late-season numbers of Alliaria petiolata seedlings were not reduced",
    "Transplanting Sanguinaria canadensis at 9 or 11 ramets/m2 would generally be cost and labor effective",
]
paras, head, buf = [], "(top)", []

def flush():
    if buf:
        paras.append((head, " ".join(buf)))
        buf.clear()

for line in Path("corpus/YKQQZV93.md").read_text(encoding="utf-8").splitlines():
    if line.lstrip().startswith("#"):
        flush(); head = re.sub(r"^[#\s\d.*]+", "", line)
    elif not line.strip():
        flush()
    else:
        buf.append(line.strip())
flush()

rows = [(h, s) for h, p in paras for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", p)]
for q in LOST:
    h, s = max(rows, key=lambda r: sim(toks(q), toks(r[1])))
    print("section:", h[:40], "| skipped section:", bool(SKIP_HEAD.match(h)),
          "| length:", len(s), "| cue match:", bool(CUES.search(s)))
    print("   ", s[:160])