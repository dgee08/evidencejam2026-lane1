import csv, hashlib, json, os, re, sys, threading, time, yaml
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from openai import OpenAI
from linkml.validator import Validator
from linkml.validator.plugins import JsonschemaValidationPlugin

SCHEMA = "schema/causalmosaic.yaml"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "output")
WORKERS = int(os.environ.get("CAMO_WORKERS", "6"))
BUDGET_MIN = float(os.environ.get("CAMO_BUDGET_MIN", "90"))
LIMIT = int(os.environ.get("CAMO_LIMIT", "0"))
FORCE = os.environ.get("CAMO_FORCE") == "1"
MAX_SENT = 20
DEADLINE = time.time() + BUDGET_MIN * 60
LOCK = threading.Lock()
STATS = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "articles": 0, "claims": 0}

schema = yaml.safe_load(open(SCHEMA, encoding="utf-8"))
def enum(n): return set(schema["enums"][n]["permissible_values"])
PRED = {"causes", "contributes_to", "enables"} & enum("CausalPredicateEnum")
LANG = enum("ClaimStrengthEnum") - {"associational"}   # 'associational' fails validation in v0.10.0
ACC, QUAL, ETYPE = enum("PhilosophicalAccountEnum"), enum("StateOrChangeQualifierEnum"), enum("EntityTypeEnum")
VALIDATOR = Validator(SCHEMA, validation_plugins=[JsonschemaValidationPlugin(closed=True)])

client = OpenAI(base_url=os.environ["BRINE_URL"], api_key=os.environ["BRINE_API_KEY"], timeout=300, max_retries=0)
MODEL = os.environ["BRINE_MODEL"]
PROMPT = Path("prompts/extract_system.txt").read_text(encoding="utf-8")
INDEX = {r["slug"]: r for r in csv.DictReader(open("corpus/index.csv", encoding="utf-8"))}

SKIP_HEAD = re.compile(r"^(introduction|background|methods?|materials|study (site|area|system|species)|experimental|references?|literature cited|acknowledg|appendix|supplement)", re.I)
CUES = re.compile(r"\b(affect|effect|increas|decreas|reduc|suppress|promot|enhanc|inhibit|limit|caus|result(ed|s)? in|lead|led|due to|because|driv|respond|response|declin|improv|benefit|facilitat|prevent|depress|greater|higher|lower|fewer|significan|effective|strateg|suggest|mitigat|rose\b|rise|indicat|harm|kill|no (significant )?(difference|change|effect))|P\s*[<=>]", re.I)

def errors(g): return [r.message for r in VALIDATOR.validate(g, "CausalGraph").results]
def h(*parts): return hashlib.sha1("|".join(parts).encode()).hexdigest()[:10]
def clean(s): return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s).replace("*", "")).strip()

def split_sentences(body):
    body = re.sub(r"(?<![.!?:\"')\]])\n\s*\n(?=[a-z])", " ", body)
    out = []
    for para in re.split(r"\n\s*\n", body):
        if para.count("|") > 4:
            continue                      # table rows
        for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", para.replace("\n", " ")):
            if 40 <= len(s) <= 600 and CUES.search(s):
                out.append(s.strip())
    return out

def selected_sentences(text):
    keep, buf, has_head = True, [], False
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            has_head = True
            keep = not SKIP_HEAD.match(re.sub(r"^[#\s\d.*]+", "", line))
            continue
        if keep:
            buf.append(line)
    sents = split_sentences("\n".join(buf)) if has_head else []
    return sents if len(sents) >= 3 else split_sentences(text)   # fall back if headings are unreliable

def parse_claims(txt):
    start = txt.find("{")
    if start < 0:
        return None
    s = txt[start:]
    try:
        d = json.loads(s[: s.rfind("}") + 1])
        return d.get("claims", []) if isinstance(d, dict) else None
    except json.JSONDecodeError:
        pass
    # truncated reply: keep every complete claim before the cut
    for m in reversed(list(re.finditer(r"\}\s*,", s))):
        try:
            d = json.loads(s[: m.start() + 1] + "]}")
            return d.get("claims", []) if isinstance(d, dict) else None
        except json.JSONDecodeError:
            continue
    return None

def ask(numbered):
    r = None
    for attempt in range(3):
        try:
            r = client.chat.completions.create(
                model=MODEL, temperature=0, max_tokens=3000,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                messages=[{"role": "system", "content": PROMPT}, {"role": "user", "content": numbered}])
            break
        except Exception as e:
            print("  request failed:", type(e).__name__, "attempt", attempt + 1, flush=True)
            time.sleep(5 * (attempt + 1))
    if r is None:
        return None
    choice = r.choices[0]
    with LOCK:
        STATS["calls"] += 1
        if r.usage:
            STATS["prompt_tokens"] += r.usage.prompt_tokens
            STATS["completion_tokens"] += r.usage.completion_tokens
        if choice.finish_reason == "length":
            STATS["truncated"] = STATS.get("truncated", 0) + 1
    txt = re.sub(r"<think>.*?</think>", "", choice.message.content or "", flags=re.S)
    claims = parse_claims(txt)
    if claims is None:
        print(f"  reply was not valid JSON (finish={choice.finish_reason})", flush=True)
    return claims

def in_sentence(term, sent):
    low = sent.lower()
    stems = [w[:5] for w in re.findall(r"[a-z]+", term.lower()) if len(w) >= 4]
    return any(s in low for s in stems)

def check(c, sents):
    """None if the claim is usable, else the reason it is dropped."""
    if not (isinstance(c.get("id"), int) and 0 <= c["id"] < len(sents)): return "bad_id"
    if c.get("pred") not in PRED or c.get("lang") not in LANG: return "bad_enum"
    if bool(c.get("neg")) != (c["lang"] == "no_relationship"): return "bad_null_pairing"
    if not any(a in ACC for a in c.get("acc", [])): return "no_accounts"
    for side in ("cause", "effect"):
        term = c[side].get("term") or ""
        if not term or not in_sentence(term, sents[c["id"]]): return side + "_not_in_sentence"
    return None

def node(slug, n):
    q, a, t = (n.get("q") or ""), (n.get("attr") or ""), n["term"].strip()
    d = {"id": f"causal_mosaic:{slug.lower()}_{h(slug, n.get('type', ''), t.lower(), a.lower(), q)}",
         "name": " ".join(x for x in [q, a, "of" if a else "", t] if x),
         "entity_term": t}
    if n.get("type") in ETYPE: d["entity_type"] = n["type"]
    if a: d["measured_attribute"] = a
    if q in QUAL: d["state_or_change_qualifier"] = q
    return d

def build(slug, sents, c):
    if c["lang"] == "no_relationship" and c["effect"].get("q") == "unchanged":
        c["effect"]["q"] = ""
    sent = sents[c["id"]]
    s, o = node(slug, c["cause"]), node(slug, c["effect"])
    edge = {"id": f"causal_mosaic:edge_{h(s['id'], o['id'], c['pred'], sent)}",
            "subject": s["id"], "predicate": c["pred"], "object": o["id"],
            "causal_language": c["lang"],
            "philosophical_accounts": [a for a in c["acc"] if a in ACC],
            "original_sentence": sent,           # copied by code, verbatim from the article
            "source_spans": [{"text": sent}],
            "source_document": slug}
    if c["lang"] == "no_relationship":
        edge["negated"] = True
    return [s, o], edge

def source_doc(slug):
    row = INDEX.get(slug, {})
    d = {"document_id": slug, "doi": row.get("DOI") or row.get("doi"), "title": row.get("title")}
    if str(row.get("year", "")).isdigit(): d["year"] = int(row["year"])
    return {k: v for k, v in d.items() if v}

def assemble(slug, sents, claims):
    nodes, edges = {}, {}
    for c in claims:
        ns, e = build(slug, sents, c)
        for n in ns: nodes[n["id"]] = n
        edges[e["id"]] = e
    return {"graph_id": slug, "schema_version": "0.10.0",
            "provenance": {"causal_mosaic_version": "0.10.0"},
            "source_documents": [source_doc(slug)],
            "nodes": list(nodes.values()), "edges": list(edges.values())}

def process(path):
    path = Path(path); slug = path.stem
    cache = OUT / "articles" / f"{slug}.json"
    if cache.exists():
        return slug, {"cached": True}
    if time.time() > DEADLINE:
        return slug, {"skipped": "time budget"}
    sents = selected_sentences(path.read_text(encoding="utf-8"))
    log = {"sentences_sent": len(sents), "kept": 0, "dropped": {}, "invalid": []}
    claims, seen, failed = [], set(), False
    for start in range(0, len(sents), MAX_SENT):
        end = min(start + MAX_SENT, len(sents))
        batch = "\n".join(f"[{i}] {clean(sents[i])}" for i in range(start, end))
        res = ask(batch)
        if res is None:
            failed = True
            continue
        for c in res:
            try:
                reason = check(c, sents)
                if reason is None and c["id"] in seen:
                    reason = "duplicate_sentence"
            except (AttributeError, TypeError, KeyError):
                reason = "malformed"
            if reason:
                log["dropped"][reason] = log["dropped"].get(reason, 0) + 1
                continue
            seen.add(c["id"]); claims.append(c)
    if failed:
        return slug, {"failed": True}          # not cached, so it is retried
    graph = assemble(slug, sents, claims)
    if claims and errors(graph):
        good = []
        for c in claims:
            errs = errors(assemble(slug, sents, [c]))
            if errs:
                log["invalid"].append({"sentence": sents[c["id"]][:200], "errors": errs[:2]})
            else:
                good.append(c)
        claims, graph = good, assemble(slug, sents, good)
    log["kept"] = len(claims)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")
    (OUT / "articles" / f"{slug}.log.json").write_text(json.dumps(log, indent=1, ensure_ascii=False), encoding="utf-8")
    with LOCK:
        STATS["articles"] += 1
        STATS["claims"] += len(claims)
    return slug, log

def merge(all_slugs):
    nodes, edges = {}, {}
    for f in sorted((OUT / "articles").glob("*.json")):
        if f.name.endswith(".log.json"): continue
        g = json.loads(f.read_text(encoding="utf-8"))
        for n in g["nodes"]: nodes[n["id"]] = n
        for e in g["edges"]: edges[e["id"]] = e
    graph = {"graph_id": "evidencejam2026-lane1", "schema_version": "0.10.0",
             "provenance": {"causal_mosaic_version": "0.10.0"},
             "source_documents": [source_doc(s) for s in all_slugs],
             "nodes": list(nodes.values()), "edges": list(edges.values())}
    errs = errors(graph)
    print("merged graph:", len(nodes), "nodes,", len(edges), "edges | validation errors:", errs[:3])
    (OUT / "graph.json").write_text(json.dumps(graph, indent=1, ensure_ascii=False), encoding="utf-8")
    yaml.safe_dump(graph, open(OUT / "graph.yaml", "w", encoding="utf-8"), sort_keys=False, allow_unicode=True)

def main(corpus):
    t0 = time.time()
    files = sorted(Path(corpus).glob("*.md"))
    if LIMIT: files = files[:LIMIT]
    OUT.mkdir(parents=True, exist_ok=True)
    if FORCE:
        for f in (OUT / "articles").glob("*.json"): f.unlink()
    for rnd in (1, 2):
        todo = [f for f in files if not (OUT / "articles" / f"{f.stem}.json").exists()]
        if not todo or time.time() > DEADLINE:
            break
        print(f"pass {rnd}: {len(todo)} articles", flush=True)
        with ThreadPoolExecutor(WORKERS) as ex:
            futs = [ex.submit(process, f) for f in todo]
            for i, fu in enumerate(as_completed(futs), 1):
                try:
                    slug, log = fu.result()
                    print(f"[{i}/{len(todo)}] {slug}", {k: (len(v) if isinstance(v, list) else v) for k, v in log.items()}, flush=True)
                except Exception as e:
                    print(f"[{i}/{len(todo)}] error:", type(e).__name__, str(e)[:120], flush=True)
    merge([f.stem for f in files])
    STATS["wall_seconds"] = round(time.time() - t0)
    (OUT / "run_stats.json").write_text(json.dumps(STATS, indent=1), encoding="utf-8")
    print(STATS)

if __name__ == "__main__":
    target = Path(sys.argv[1])
    if target.is_file():
        (OUT / "articles" / f"{target.stem}.json").unlink(missing_ok=True)
        print(process(target))
    else:
        main(target)