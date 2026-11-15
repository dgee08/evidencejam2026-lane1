import yaml
from pathlib import Path

d = yaml.safe_load(open("example/Murphy2005.yaml", encoding="utf-8"))
gs = d["graphs"]
print("number of findings:", len(gs))

e0 = gs[0]
print("finding_id:", e0["finding_id"])
print("fit:", e0["fit"])
print("sentence:", e0["sentence"])
print("inner graph keys:", list(e0["graph"].keys()))

out = Path("example/_graphs")
out.mkdir(exist_ok=True)
for i, e in enumerate(gs):
    with open(out / f"{i:02d}.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(e["graph"], f, sort_keys=False, allow_unicode=True)

print("\nother top-level keys:")
for k in ("article", "citation", "camo", "format"):
    print(k, "->", d.get(k))