import csv, hashlib, json, os, re, sys, yaml
from pathlib import Path
from openai import OpenAI
from linkml.validator import validate

SCHEMA = "schema/causalmosaic.yaml"
schema = yaml.safe_load(open(SCHEMA, encoding="utf-8"))
def enum(name): return set(schema["enums"][name]["permissible_values"])
PRED, LANG = enum("CausalPredicateEnum"), enum("ClaimStrengthEnum")
ACC, QUAL, ETYPE = enum("PhilosophicalAccountEnum"), enum("StateOrChangeQualifierEnum"), enum("EntityTypeEnum")

client = OpenAI(base_url=os.environ["BRINE_URL"], api_key=os.environ["BRINE_API_KEY"])
MODEL = os.environ["BRINE_MODEL"]
PROMPT = Path("prompts/extract_system.txt").read_text(encoding="utf-8")

def norm(s): return re.sub(r"\s+", " ", s.replace("\u00ad", "")).strip().lower()

def chunks(text, size=6000):
    text = re.split(r"\n#+\s*(references|literature cited|acknowledg)", text, flags=re.I)[0]
    out, cur = [], ""
    for p in text.split("\n\n"):
        if len(cur) + len(p) > size and cur:
            out.append(cur); cur = ""
        cur += p + "\n\n"
    return out + [cur] if cur.strip() else out

def ask(chunk):
    r = client.chat.completions.create(model=MODEL, temperature=0, messages=[
        {"role": "system", "content": PROMPT}, {"role": "user", "content": chunk}])
    txt = re.sub(r"<think>.*?</think>", "", r.choices[0].message.content, flags=re.S)
    m = re.search(r"\{.*\}", txt, flags=re.S)
    return json.loads(m.group(0)).get("claims", []) if m else []

def h(*parts): return hashlib.sha1("|".join(parts).encode()).hexdigest()[:10]

def node(slug, n):
    q, a, t = n.get("qualifier") or "", n.get("measured_attribute") or "", n["entity_term"]
    d = {"id": f"causal_mosaic:{slug.lower()}_{h(slug, n.get('entity_type',''), t.lower(), a.lower(), q)}",
         "name": " ".join(x for x in [q, a, "of" if a else "", t] if x),
         "entity_term": t}
    if n.get("entity_type") in ETYPE: d["entity_type"] = n["entity_type"]
    if a: d["measured_attribute"] = a
    if q in QUAL: d["state_or_change_qualifier"] = q
    return d

def claim_graph(slug, c):
    if c["causal_language"] == "no_relationship" and c["effect"].get("qualifier") == "unchanged":
        c["effect"]["qualifier"] = ""
    s, o = node(slug, c["cause"]), node(slug, c["effect"])
    lang = c["causal_language"]
    edge = {"id": f"causal_mosaic:edge_{h(s['id'], o['id'], c['predicate'], c['original_sentence'])}",
            "subject": s["id"], "predicate": c["predicate"], "object": o["id"],
            "causal_language": lang,
            "philosophical_accounts": [a for a in c["philosophical_accounts"] if a in ACC],
            "original_sentence": c["original_sentence"],
            "source_spans": [{"text": c["original_sentence"]}],
            "source_document": slug}
    if lang == "no_relationship":
        edge["negated"] = True
    return {"graph_id": f"{slug}-{edge['id'][-10:]}", "schema_version": "0.10.0",
            "provenance": {"causal_mosaic_version": "0.10.0"}, "nodes": [s, o], "edges": [edge]}

def errors(g): return [r.message for r in validate(g, SCHEMA, "CausalGraph").results]

def source_doc(slug):
    row = {r["slug"]: r for r in csv.DictReader(open("corpus/index.csv", encoding="utf-8"))}[slug]
    d = {"document_id": slug, "doi": row["DOI"] if "DOI" in row else row.get("doi"), "title": row.get("title")}
    if str(row.get("year", "")).isdigit(): d["year"] = int(row["year"])
    return {k: v for k, v in d.items() if v}

def run(path):
    path = Path(path); slug = path.stem
    text = path.read_text(encoding="utf-8"); ntext = norm(text)
    nodes, edges, log = {}, {}, {"kept": 0, "not_grounded": 0, "bad_shape": 0, "invalid": []}
    for ch in chunks(text):
        for c in ask(ch):
            try:
                if norm(c["original_sentence"]) not in ntext:
                    log["not_grounded"] += 1; continue
                if (c["predicate"] not in PRED or c["causal_language"] not in LANG
                        or bool(c.get("negated")) != (c["causal_language"] == "no_relationship")):
                    log["bad_shape"] += 1; continue
                g = claim_graph(slug, c)
            except (KeyError, TypeError):
                log["bad_shape"] += 1; continue
            errs = errors(g)
            if errs:
                log["invalid"].append({"sentence": c["original_sentence"], "errors": errs}); continue
            for n in g["nodes"]: nodes[n["id"]] = n
            for e in g["edges"]: edges[e["id"]] = e
            log["kept"] += 1
    graph = {"graph_id": slug, "schema_version": "0.10.0",
             "provenance": {"causal_mosaic_version": "0.10.0"},
             "source_documents": [source_doc(slug)],
             "nodes": list(nodes.values()), "edges": list(edges.values())}
    print("final graph errors:", errors(graph))
    Path("output").mkdir(exist_ok=True)
    yaml.safe_dump(graph, open(f"output/{slug}.yaml", "w", encoding="utf-8"), sort_keys=False, allow_unicode=True)
    json.dump(graph, open(f"output/{slug}.json", "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    print(json.dumps(log, indent=2)[:3000])

if __name__ == "__main__":
    run(sys.argv[1])