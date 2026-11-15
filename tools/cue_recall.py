import yaml
from pipeline_v1 import CUES

d = yaml.safe_load(open("example/Murphy2005.yaml", encoding="utf-8"))
sents = [e["sentence"] for e in d["graphs"]]
miss = [s for s in sents if not CUES.search(s)]
print(f"{len(sents) - len(miss)}/{len(sents)} annotated sentences pass the cue filter")
for s in miss:
    print("MISSED:", s)