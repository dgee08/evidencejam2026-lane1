import yaml

m = yaml.safe_load(open("example/_merged.yaml", encoding="utf-8"))
e = m["edges"][19]
print("edge id:", e.get("id"))
print("original_sentence:", e.get("original_sentence"))
print("dose:", e.get("dose"))