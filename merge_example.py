import yaml

d = yaml.safe_load(open("example/Murphy2005.yaml", encoding="utf-8"))
graphs = [e["graph"] for e in d["graphs"]]

merged = {k: v for k, v in graphs[0].items() if k not in ("nodes", "edges")}
nodes, edges = {}, {}
dup_nodes = 0
for g in graphs:
    for n in g.get("nodes", []):
        if n["id"] in nodes:
            dup_nodes += 1
        nodes.setdefault(n["id"], n)
    for e in g.get("edges", []):
        edges.setdefault(e.get("id", id(e)), e)

merged["nodes"] = list(nodes.values())
merged["edges"] = list(edges.values())

print("findings:", len(graphs))
print("unique nodes:", len(nodes), "| repeated node ids:", dup_nodes)
print("edges:", len(edges))

with open("example/_merged.yaml", "w", encoding="utf-8") as f:
    yaml.safe_dump(merged, f, sort_keys=False, allow_unicode=True)