import json, random, sys
from pathlib import Path

slug = sys.argv[1] if len(sys.argv) > 1 else None
if slug:
    files = [Path(f"output/articles/{slug}.json")]
else:
    files = [p for p in Path("output/articles").glob("*.json") if not p.name.endswith(".log.json")]
rows = []
for f in files:
    g = json.loads(f.read_text(encoding="utf-8"))
    nm = {n["id"]: n["name"] for n in g["nodes"]}
    for e in g["edges"]:
        line = f"{nm[e['subject']]}  --{e['predicate']}/{e['causal_language']}-->  {nm[e['object']]}"
        rows.append((f.stem, line, e["original_sentence"]))
if not slug:
    random.seed(1); random.shuffle(rows); rows = rows[:15]
for s, line, sent in rows:
    print(f"[{s}] {line}\n    \"{sent[:220]}\"\n")