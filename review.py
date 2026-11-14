import yaml
g = yaml.safe_load(open("output/YKQQZV93.yaml", encoding="utf-8"))
nm = {n["id"]: n["name"] for n in g["nodes"]}
for e in g["edges"]:
    print(f'{nm[e["subject"]]}  --{e["predicate"]}/{e["causal_language"]}-->  {nm[e["object"]]}')
    print("   ", e["original_sentence"][:160])