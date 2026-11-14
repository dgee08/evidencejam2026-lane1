#!/usr/bin/env python3
"""
CAMO annotation pipeline (Step 1: end-to-end skeleton).

    ./run.sh corpus/ output/                      # whole corpus
    python annotate.py corpus/ output/ --limit 3  # first 3 articles
    python annotate.py corpus/ output/ --mock     # offline test, no API calls

Reads   corpus/*.md + corpus/index.csv
Writes  output/camo_graph.yaml, output/camo_graph.json   (ONE merged CausalGraph)
        output/run_stats.json                            (tokens, drops, timings)

Pipeline per article:
  clean markdown -> keep abstract/results/discussion/conclusion -> chunk
  -> one LLM call per chunk (findings with a verbatim quote, nodes, edges)
  -> code verifies each quote against the text (drops invented claims)
  -> code repairs rule violations deterministically (no extra tokens)
  -> code builds ids, source_spans, source_document
  -> validate against the LinkML schema, dropping any finding that fails
Then merge all articles into one graph and validate the whole thing.

Environment (exactly these names): BRINE_URL, BRINE_MODEL, BRINE_API_KEY.
"""
import argparse
import concurrent.futures as cf
import csv
import datetime
import difflib
import hashlib
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
SCHEMA_PATH = Path(os.environ.get("CAMO_SCHEMA", HERE / "schema" / "causalmosaic.yaml"))
CACHE_DIR = HERE / ".cache"
EXPORTER = "evidencejam-lane1-0.1"

CHUNK_CHARS = 6000          # max characters of article text per LLM call
CONTEXT_CHARS = 1000        # study-site paragraph given as context only
MAX_FINDINGS_PER_CHUNK = 12
KEEP_SECTIONS = ("abstract", "results", "discussion", "conclusion")

with open(SCHEMA_PATH, encoding="utf-8") as fh:
    SCHEMA = yaml.safe_load(fh)
ENUMS, CLASSES, PREFIXES = SCHEMA["enums"], SCHEMA["classes"], SCHEMA["prefixes"]


def enum_values(name):
    return list(ENUMS[name]["permissible_values"])


VALID = {
    "entity_type": set(enum_values("EntityTypeEnum")),
    "qualifier": set(enum_values("StateOrChangeQualifierEnum")),
    "life_stage": set(enum_values("LifeStageEnum")) - {"not_specified"},
    "predicate": set(enum_values("CausalPredicateEnum")),
    "language": set(enum_values("ClaimStrengthEnum")),
    "accounts": set(enum_values("PhilosophicalAccountEnum")),
    "section": set(enum_values("DocumentSectionEnum")),
}
NON_CAUSAL_PREDICATES = {"associated_with", "correlated_with", "precedes"}


# =============================================================================
# 1. Reading and cleaning articles
# =============================================================================

def load_index(corpus):
    """slug -> row. Column names are matched case-insensitively."""
    path = Path(corpus) / "index.csv"
    rows = {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            if row.get("slug"):
                rows[row["slug"]] = row
    return rows


_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_TAG = re.compile(r"</?[A-Za-z][^>]*>")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")


def clean_markdown(text):
    """Remove PDF-conversion artefacts: image placeholders, link wrappers, inline tags."""
    text = _IMG.sub("", text)
    text = _LINK.sub(r"\1", text)
    text = _TAG.sub("", text)                    # <sup>, <sub>, ...
    text = text.replace("\\*", "*")
    text = re.sub(r"[ \t]+", " ", text)
    return text


def classify_heading(title):
    """Map a heading to a DocumentSectionEnum value, 'skip', 'context' or None (inherit)."""
    t = re.sub(r"[^a-z ]", " ", title.lower())
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\b(a b s t r a c t)\b", "abstract", t)
    if re.search(r"\b(references|bibliography|literature cited|acknowledg|author contribution|"
                 r"conflict of interest|funding|appendix|supplementary|supporting information)\b", t):
        return "skip"
    if "a b s t r a c t" in title.lower() or re.fullmatch(r"abstract|summary", t):
        return "abstract"
    if re.search(r"\bresults?\b", t):
        return "results"                         # also "results and discussion"
    if re.search(r"\bdiscussion\b", t):
        return "discussion"
    if re.search(r"\b(conclusions?|concluding|management implications|implications)\b", t):
        return "conclusion"
    if re.search(r"\b(study site|study area|site description|study system)\b", t):
        return "context"
    if re.search(r"\b(introduction|background)\b", t):
        return "introduction"
    if re.search(r"\b(methods?|materials|experimental design|data analysis|statistical analysis|"
                 r"sampling|measurements?)\b", t):
        return "methods"
    return None


def split_sections(text):
    """Return [(section, [paragraphs])] in reading order; stops at References."""
    sections, current, paras, buf = [], "front", [], []

    def flush_para():
        if buf:
            paras.append(" ".join(buf).strip())
            buf.clear()

    def flush_section():
        flush_para()
        if paras:
            sections.append((current, list(paras)))
        paras.clear()

    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            kind = classify_heading(m.group(1))
            if kind == "skip":
                flush_section()
                current = "skip"
                break                            # references etc. end the article body
            if kind is not None:
                flush_section()
                current = kind
            continue                             # unknown heading: inherit, drop heading text
        if not line.strip():
            flush_para()
        else:
            buf.append(line.strip())
    else:
        flush_section()
    return sections


def is_noise(par):
    if len(par) < 80:
        return True
    if re.match(r"^\**(fig|figure|table)\b\.?\s*\d", par, re.I):     # captions
        return True
    letters = sum(c.isalpha() for c in par)
    if letters / max(len(par), 1) < 0.5 or par.count("|") > 6:       # broken tables
        return True
    if re.search(r"©|all rights reserved|corresponding author|e-?mail address", par, re.I):
        return True
    return False


def make_chunks(text):
    """-> (context_text, [{section, text}]). Only claim-bearing sections are chunked."""
    sections = split_sections(clean_markdown(text))
    context = ""
    for sec, paras in sections:
        if sec == "context" and paras and not context:
            context = paras[0][:CONTEXT_CHARS]
    kept = [(s, [p for p in ps if not is_noise(p)]) for s, ps in sections if s in KEEP_SECTIONS]
    if not any(ps for _, ps in kept):            # headings lost in conversion: fall back
        kept = [("results", [p for _, ps in sections for p in ps
                             if not is_noise(p)])]
    chunks = []
    for sec, paras in kept:
        buf, size = [], 0
        for p in paras:
            if buf and size + len(p) > CHUNK_CHARS:
                chunks.append({"section": sec, "text": "\n\n".join(buf)})
                buf, size = [], 0
            buf.append(p)
            size += len(p) + 2
        if buf:
            chunks.append({"section": sec, "text": "\n\n".join(buf)})
    return context, chunks


# =============================================================================
# 2. Prompt
# =============================================================================

EXAMPLE_TEXT = ("Alliaria petiolata number of seedlings in early spring was not affected by initial "
                "Sanguinaria canadensis density. Initial S. canadensis densities of as little as 5 "
                "ramets/m2 suppressed A. petiolata.")
EXAMPLE_OUT = {"findings": [
    {"quote": "Alliaria petiolata number of seedlings in early spring was not affected by initial "
              "Sanguinaria canadensis density.",
     "nodes": [
         {"key": "a", "name": "Increased Sanguinaria canadensis initial planting density",
          "entity_type": "taxon", "entity_term": "Sanguinaria canadensis",
          "measured_attribute": "initial planting density", "state_or_change_qualifier": "increased"},
         {"key": "b", "name": "Alliaria petiolata seedling number", "entity_type": "taxon",
          "entity_term": "Alliaria petiolata", "measured_attribute": "number of individuals",
          "life_stage": "germinant_or_seedling"}],
     "edges": [{"subject": "a", "predicate": "causes", "object": "b",
                "causal_language": "no_relationship",
                "philosophical_accounts": ["interventionist"]}]},
    {"quote": "Initial S. canadensis densities of as little as 5 ramets/m2 suppressed A. petiolata.",
     "nodes": [
         {"key": "a", "name": "Increased Sanguinaria canadensis initial planting density",
          "entity_type": "taxon", "entity_term": "Sanguinaria canadensis",
          "measured_attribute": "initial planting density", "state_or_change_qualifier": "increased"},
         {"key": "b", "name": "Decreased Alliaria petiolata performance", "entity_type": "taxon",
          "entity_term": "Alliaria petiolata", "measured_attribute": "performance",
          "state_or_change_qualifier": "decreased"}],
     "edges": [{"subject": "a", "predicate": "causes", "object": "b",
                "causal_language": "direct_causal",
                "philosophical_accounts": ["interventionist", "counterfactual"]}]}]}


def system_prompt():
    return f"""You extract causal claims from an ecological restoration paper into the Causal Mosaic (CAMO) schema.
You get one excerpt. Return ONE JSON object: {{"findings": [...]}}. JSON only, no commentary. Return {{"findings": []}} if the excerpt has no causal claim.

FINDING = one sentence (or two adjacent ones) where the AUTHORS claim that something affects something else, from their own results or conclusions.
Include hedged claims ("may", "suggests") and null results ("no significant effect"). Skip background claims credited to cited studies, methods, and pure description with no cause.
Each finding has:
  quote   the sentence(s), COPIED EXACTLY from the excerpt, character for character
  nodes   the variables involved
  edges   the causal links between them (use node keys)
Cap: {MAX_FINDINGS_PER_CHUNK} findings. One node per separately reported outcome. Reuse a node's exact name when the same variable appears again.

NODE = a state or change of a property of a thing, never just a thing. Fields:
  key                        short label, unique within the finding ("a", "b", ...)
  name                       readable, e.g. "Decreased Sphagnum cover"
  entity_type                one of {sorted(VALID['entity_type'])}
                             (taxon = property of one taxon; environmental_variable = water table, soil pH, light;
                              composite_variable = community or guild; environmental_process = competition, leafing;
                              management_intervention = a deliberate action: planting, burning, rewetting)
  entity_term                the thing. Taxa: scientific name only. Otherwise a short plain noun phrase ("methane", "water table", "restored peatland"). Never write ontology IDs.
  measured_attribute         the property: "cover", "flux", "depth", "number of individuals"
  state_or_change_qualifier  one of {sorted(VALID['qualifier'])}; omit if the text gives no direction
  life_stage                 optional, one of {sorted(VALID['life_stage'])}; never put a life stage in measured_attribute
EDGE fields:
  subject, object            node keys
  predicate                  one of {sorted(VALID['predicate'])}; usually causes, contributes_to, prevents or associated_with
  causal_language            the AUTHORS' wording, not the strength of the evidence:
                               direct_causal = unhedged causal verb (resulted in, suppressed, reduced)
                               uncertain_causal = hedged (may, appears, suggests, likely)
                               associational = co-occurrence/correlation without a causal verb
                               no_relationship = explicit null result (no significant effect, not affected)
  philosophical_accounts     list, at least one of {sorted(VALID['accounts'])}
                               interventionist = something was manipulated; counterfactual = with vs without, more vs less;
                               mechanistic = a pathway/how; probabilistic = usually/tends to; agency = advice to managers
RULES
- Direction goes on the node qualifier, not the predicate: "restoration reduced CH4" = causes -> "Decreased CH4 flux".
- Null result: keep the tested predicate (causes), causal_language no_relationship, and no "unchanged" qualifier.
- No double negatives: never "prevents" with a "decreased" object.
- associated_with / correlated_with / precedes only take associational or no_relationship; causes never takes associational.
- Record only what the text says. Do not guess.

EXAMPLE
Excerpt: {EXAMPLE_TEXT}
Output: {json.dumps(EXAMPLE_OUT)}"""


def user_prompt(chunk, context):
    ctx = f"Study context (background only, do not extract claims from it):\n{context}\n\n" if context else ""
    return f"{ctx}Excerpt ({chunk['section']} section):\n{chunk['text']}\n\nOutput JSON only."


# =============================================================================
# 3. LLM client (OpenAI-compatible endpoint), with on-disk cache and token stats
# =============================================================================

class LLM:
    def __init__(self, thinking=False, use_cache=True):
        self.thinking, self.use_cache = thinking, use_cache
        self.model = os.environ.get("BRINE_MODEL", "")
        self.temperature = float(os.environ.get("BRINE_TEMPERATURE", "0.2"))
        self.max_tokens = int(os.environ.get("BRINE_MAX_TOKENS", "6000"))
        self._client = None
        self._lock = threading.Lock()

    def client(self):
        with self._lock:
            if self._client is None:
                from openai import OpenAI
                self._client = OpenAI(base_url=os.environ["BRINE_URL"],
                                      api_key=os.environ["BRINE_API_KEY"], timeout=300)
            return self._client

    def complete(self, system, user):
        """-> (text, usage dict). Raises on persistent failure."""
        key = hashlib.sha256(json.dumps([self.model, system, user, self.thinking,
                                         self.temperature]).encode()).hexdigest()
        path = CACHE_DIR / f"{key}.json"
        if self.use_cache and path.exists():
            hit = json.loads(path.read_text(encoding="utf-8"))
            return hit["text"], {"prompt": 0, "completion": 0, "cached": 1}
        last = None
        for attempt in range(3):
            try:
                kwargs = dict(model=self.model, temperature=self.temperature, max_tokens=self.max_tokens,
                              messages=[{"role": "system", "content": system},
                                        {"role": "user", "content": user}])
                try:
                    r = self.client().chat.completions.create(
                        **kwargs, extra_body={"chat_template_kwargs": {"enable_thinking": self.thinking}})
                except Exception as exc:
                    if "chat_template" not in str(exc) and "extra" not in str(exc).lower():
                        raise
                    r = self.client().chat.completions.create(**kwargs)
                text = strip_thinking(r.choices[0].message.content or "")
                u = getattr(r, "usage", None)
                usage = {"prompt": getattr(u, "prompt_tokens", 0) or 0,
                         "completion": getattr(u, "completion_tokens", 0) or 0, "cached": 0}
                parse_json(text)                  # make sure it parses before caching/returning
                if self.use_cache:
                    CACHE_DIR.mkdir(exist_ok=True)
                    path.write_text(json.dumps({"text": text}), encoding="utf-8")
                return text, usage
            except Exception as exc:              # network error, bad JSON, truncated thinking...
                last = exc
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"LLM call failed after 3 attempts: {last}")


def strip_thinking(text):
    if "</think>" in text:
        return text.split("</think>")[-1].strip()
    if text.lstrip().startswith("<think>"):
        raise ValueError("reply is an unfinished <think> block (raise BRINE_MAX_TOKENS)")
    return text.strip()


def parse_json(text):
    text = re.sub(r"^```(?:json)?\s*|\s*```\s*$", "", text.strip())
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b == -1:
        raise ValueError(f"no JSON object in reply: {text[:120]!r}")
    return json.loads(text[a:b + 1])


# =============================================================================
# 4. Grounding: verify quotes against the article text
# =============================================================================

def norm(s):
    return re.sub(r"\s+", " ", s.replace("−", "-").replace("–", "-")).strip().lower()


def split_sentences(text):
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z(\[])", text.replace("\n", " "))
    return [p.strip() for p in parts if p.strip()]


def ground_quote(quote, chunk_text):
    """Return the exact text from the chunk matching the model's quote, or None.
    Exact (whitespace-insensitive) match first, then a close-sentence match >= 0.88."""
    q = norm(quote)
    if len(q) < 20:
        return None
    hay = norm(chunk_text)
    if q in hay:
        # the shortest sentence window containing the quote, with the paper's own spacing
        hits = [w for w in sentence_windows(chunk_text) if q in norm(w)]
        return min(hits, key=len) if hits else quote.strip()
    best, best_r = None, 0.0
    for window in sentence_windows(chunk_text):
        r = difflib.SequenceMatcher(None, q, norm(window)).ratio()
        if r > best_r:
            best, best_r = window, r
    return best if best_r >= 0.88 else None


def sentence_windows(text):
    sents = split_sentences(text)
    for i, s in enumerate(sents):
        yield s
        if i + 1 < len(sents):
            yield s + " " + sents[i + 1]


# =============================================================================
# 5. Sanitize one finding: coerce, repair rule violations, build schema objects
# =============================================================================

def slug_text(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:48] or "node"


def short_hash(*parts, n=6):
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:n]


def clean_str(v):
    return re.sub(r"\s+", " ", v).strip() if isinstance(v, str) and v.strip() else None


def build_node(raw, slug, quote, section):
    name = clean_str(raw.get("name"))
    term = clean_str(raw.get("entity_term")) or name
    if not term:
        return None
    node = {"name": name or term, "entity_term": term}
    et = clean_str(raw.get("entity_type"))
    if et in VALID["entity_type"]:
        node["entity_type"] = et
    attr = clean_str(raw.get("measured_attribute"))
    if attr:
        node["measured_attribute"] = attr
    q = clean_str(raw.get("state_or_change_qualifier"))
    if q in VALID["qualifier"]:
        node["state_or_change_qualifier"] = q
    ls = clean_str(raw.get("life_stage"))
    if ls in VALID["life_stage"]:
        node["life_stage"] = ls
    node["id"] = "causal_mosaic:%s_%s" % (
        slug_text(node["name"]),
        short_hash(slug, node["entity_term"], node.get("measured_attribute", ""),
                   node.get("state_or_change_qualifier", ""), node.get("life_stage", "")))
    node["source_spans"] = [{"text": quote, "section": section}]
    return node


def build_edge(raw, nodes_by_key, slug, doc_id, quote, section, stats):
    s, o = nodes_by_key.get(raw.get("subject")), nodes_by_key.get(raw.get("object"))
    if not s or not o or s["id"] == o["id"]:
        stats["edge_bad_endpoints"] += 1
        return None
    pred, lang = clean_str(raw.get("predicate")), clean_str(raw.get("causal_language"))
    if pred not in VALID["predicate"] or lang not in VALID["language"]:
        stats["edge_bad_enum"] += 1
        return None
    accounts = [a for a in (raw.get("philosophical_accounts") or [])
                if isinstance(a, str) and a in VALID["accounts"]]
    accounts = list(dict.fromkeys(accounts))
    if not accounts:
        stats["edge_no_accounts"] += 1
        return None
    # ---- deterministic repairs (cost no tokens) ----
    if pred in NON_CAUSAL_PREDICATES and lang not in ("associational", "no_relationship"):
        pred = "causes" if lang == "direct_causal" else "contributes_to"
        stats["repair_predicate"] += 1
    if pred == "causes" and lang == "associational":
        pred = "associated_with"
        stats["repair_predicate"] += 1
    if pred == "prevents" and o.get("state_or_change_qualifier") == "decreased":
        pred = "causes"                          # avoid the double negative
        stats["repair_double_negative"] += 1
    if lang == "no_relationship" and o.get("state_or_change_qualifier") == "unchanged":
        o.pop("state_or_change_qualifier")       # null results never use 'unchanged'
        stats["repair_unchanged"] += 1
    edge = {
        "id": "causal_mosaic:edge_" + short_hash(slug, s["id"], pred, o["id"], lang, quote, n=10),
        "source_document": doc_id,
        "subject": s["id"], "predicate": pred, "object": o["id"],
        "negated": lang == "no_relationship",    # canonical rule: negated iff no_relationship
        "causal_language": lang,
        "philosophical_accounts": accounts,
        "original_sentence": quote,
        "source_spans": [{"text": quote, "section": section}],
    }
    return edge


def sanitize_finding(raw, chunk, slug, doc_id, stats):
    """-> {'nodes': [...], 'edges': [...]} or None. Never trusts the model's quote."""
    quote = ground_quote(str(raw.get("quote", "")), chunk["text"])
    if not quote:
        stats["finding_quote_not_found"] += 1
        return None
    section = chunk["section"] if chunk["section"] in VALID["section"] else "results"
    nodes_by_key = {}
    for n in raw.get("nodes") or []:
        if isinstance(n, dict):
            node = build_node(n, slug, quote, section)
            if node and n.get("key") is not None:
                nodes_by_key[n["key"]] = node
    edges = []
    for e in raw.get("edges") or []:
        if isinstance(e, dict):
            edge = build_edge(e, nodes_by_key, slug, doc_id, quote, section, stats)
            if edge:
                edges.append(edge)
    if not edges:
        stats["finding_no_edges"] += 1
        return None
    used = {e["subject"] for e in edges} | {e["object"] for e in edges}
    nodes = [n for n in nodes_by_key.values() if n["id"] in used]
    return {"nodes": nodes, "edges": edges}


# =============================================================================
# 6. Validation (LinkML)
# =============================================================================

_validator, _validator_lock = None, threading.Lock()


def fast_validator():
    """JSON-Schema validator generated once from the LinkML schema. None if linkml is missing."""
    global _validator
    with _validator_lock:
        if _validator is None:
            try:
                from linkml.generators.jsonschemagen import JsonSchemaGenerator
                import jsonschema
                js = json.loads(JsonSchemaGenerator(str(SCHEMA_PATH), top_class="CausalGraph").serialize())
                _validator = jsonschema.validators.validator_for(js)(js)
            except Exception as exc:
                print(f"[warn] fast LinkML validation unavailable ({exc}); "
                      "findings will not be pre-validated", file=sys.stderr)
                _validator = False
        return _validator or None


def schema_errors(graph):
    v = fast_validator()
    return [e.message for e in v.iter_errors(graph)] if v else []


def official_errors(graph):
    """Same check as `linkml-validate -C CausalGraph`. None if linkml isn't installed."""
    try:
        from linkml.validator import validate
    except ImportError:
        return None
    return [str(r.message) for r in validate(graph, str(SCHEMA_PATH), "CausalGraph").results]


def reference_errors(graph):
    """Cross-reference rules LinkML's JSON Schema does not enforce."""
    errs, node_ids = [], {n["id"] for n in graph["nodes"]}
    doc_ids = {d["document_id"] for d in graph["source_documents"]}
    for e in graph["edges"]:
        if e["subject"] not in node_ids or e["object"] not in node_ids:
            errs.append(f"edge {e['id']}: endpoint is not a node")
        if e.get("source_document") not in doc_ids:
            errs.append(f"edge {e['id']}: source_document not in source_documents")
        if e["negated"] != (e["causal_language"] == "no_relationship"):
            errs.append(f"edge {e['id']}: negated must equal (causal_language == no_relationship)")
    return errs


def wrap(nodes, edges, docs, graph_id="causal_mosaic:camo_graph"):
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
    return {
        "graph_id": graph_id,
        "schema_version": str(SCHEMA["version"]),
        "provenance": {
            "causal_mosaic_version": str(SCHEMA["version"]),
            "created": now,
            "project": "Evidence Jam 2026 - Lane 1",
            "exporter_version": f"{EXPORTER} ({os.environ.get('BRINE_MODEL', 'mock')})",
        },
        "source_documents": docs,
        "nodes": nodes,
        "edges": edges,
    }


# =============================================================================
# 7. One article
# =============================================================================

def source_document(slug, row):
    doi = (row.get("doi") or "").strip()
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", doi, flags=re.I)
    doc = {"document_id": doi or slug}
    if doi:
        doc["doi"] = doi
    if row.get("title"):
        doc["title"] = row["title"]
    authors = row.get("authors", "")
    if authors:
        doc["authors"] = [a.strip() for a in authors.split(";") if a.strip()] or [authors]
    if (row.get("year") or "").strip().isdigit():
        doc["year"] = int(row["year"].strip())
    if row.get("journal"):
        doc["journal"] = row["journal"]
    return doc


def new_stats():
    return {k: 0 for k in ("chunks", "findings_raw", "findings_kept", "finding_quote_not_found",
                           "finding_no_edges", "edge_bad_endpoints", "edge_bad_enum", "edge_no_accounts",
                           "repair_predicate", "repair_double_negative", "repair_unchanged",
                           "finding_schema_invalid", "chunk_errors", "prompt_tokens",
                           "completion_tokens", "cached_calls")}


def annotate_article(path, row, llm):
    slug = path.stem
    t0 = time.time()
    doc = source_document(slug, row)
    stats = new_stats()
    context, chunks = make_chunks(path.read_text(encoding="utf-8", errors="replace"))
    system, findings = system_prompt(), []
    for chunk in chunks:
        stats["chunks"] += 1
        try:
            text, usage = llm.complete(system, user_prompt(chunk, context))
            reply = parse_json(text)
        except Exception as exc:
            stats["chunk_errors"] += 1
            print(f"[{slug}] chunk failed: {exc}", file=sys.stderr)
            continue
        stats["prompt_tokens"] += usage["prompt"]
        stats["completion_tokens"] += usage["completion"]
        stats["cached_calls"] += usage["cached"]
        raw_findings = reply.get("findings") or []
        stats["findings_raw"] += len(raw_findings)
        for raw in raw_findings[:MAX_FINDINGS_PER_CHUNK]:
            if isinstance(raw, dict):
                f = sanitize_finding(raw, chunk, slug, doc["document_id"], stats)
                if f:
                    findings.append(f)

    # drop any finding that fails the schema (validate each in its own mini graph)
    good = []
    for f in findings:
        errs = schema_errors(wrap(f["nodes"], f["edges"], [doc])) + \
               reference_errors(wrap(f["nodes"], f["edges"], [doc]))
        if errs:
            stats["finding_schema_invalid"] += 1
            print(f"[{slug}] dropped a finding: {errs[0][:160]}", file=sys.stderr)
        else:
            good.append(f)

    # merge findings: dedupe nodes by id (keep up to 3 spans), edges by id
    nodes, edges = {}, {}
    for f in good:
        for n in f["nodes"]:
            if n["id"] in nodes:
                spans = nodes[n["id"]]["source_spans"]
                if len(spans) < 3 and n["source_spans"][0] not in spans:
                    spans.append(n["source_spans"][0])
            else:
                nodes[n["id"]] = n
        for e in f["edges"]:
            edges.setdefault(e["id"], e)
    stats["findings_kept"] = len(good)
    stats["seconds"] = round(time.time() - t0, 1)
    return {"slug": slug, "doc": doc, "nodes": list(nodes.values()),
            "edges": list(edges.values()), "stats": stats}


# =============================================================================
# 8. Mock LLM (offline plumbing test): real sentences, plus deliberate mistakes
# =============================================================================

class MockLLM:
    thinking = False

    def complete(self, system, user):
        body = user.split("):\n", 1)[-1].rsplit("\n\nOutput JSON only.", 1)[0]
        cue = re.compile(r"\b(higher|lower|increase[sd]?|decrease[sd]?|reduc\w+|resulted in|correlated|related to|"
                         r"shift\w*|significantly)\b", re.I)
        sents = [s for s in split_sentences(body) if cue.search(s) and 60 < len(s) < 400][:3]
        nodes = [
            {"key": "a", "name": "Restoration of cutover peatland", "entity_type": "management_intervention",
             "entity_term": "restoration", "measured_attribute": "implementation",
             "state_or_change_qualifier": "introduced"},
            {"key": "b", "name": "Altered methane flux", "entity_type": "environmental_variable",
             "entity_term": "methane", "measured_attribute": "flux", "state_or_change_qualifier": "increased"},
        ]
        fs = [{"quote": s, "nodes": nodes, "edges": [
            {"subject": "a", "predicate": "associated_with", "object": "b",      # wrong pairing: code repairs it
             "causal_language": "uncertain_causal", "philosophical_accounts": ["counterfactual"]}]} for s in sents]
        fs.append({"quote": "This sentence was invented by the model and is not in the paper at all.",
                   "nodes": nodes, "edges": [{"subject": "a", "predicate": "causes", "object": "b",
                                              "causal_language": "direct_causal",
                                              "philosophical_accounts": ["interventionist"]}]})
        fs.append({"quote": sents[0] if sents else "x" * 30, "nodes": nodes,
                   "edges": [{"subject": "a", "predicate": "causes", "object": "b",
                              "causal_language": "direct_causal", "philosophical_accounts": ["experiment"]}]})
        return json.dumps({"findings": fs}), {"prompt": 0, "completion": 0, "cached": 0}


# =============================================================================
# 9. Main
# =============================================================================

def main():
    ap = argparse.ArgumentParser(description="Annotate a corpus of articles into one CAMO graph.")
    ap.add_argument("corpus", help="folder with *.md articles and index.csv")
    ap.add_argument("output", help="folder for camo_graph.yaml / .json / run_stats.json")
    ap.add_argument("--limit", type=int, help="only the first N articles (sorted by slug)")
    ap.add_argument("--slugs", nargs="+", help="only these article slugs")
    ap.add_argument("--workers", type=int, default=4, help="articles processed in parallel")
    ap.add_argument("--thinking", action="store_true", help="enable model thinking (more tokens, slower)")
    ap.add_argument("--no-cache", action="store_true", help="ignore the .cache/ of previous replies")
    ap.add_argument("--mock", action="store_true", help="fake LLM, no API calls (plumbing test)")
    ap.add_argument("--strict", action="store_true", help="exit 1 if the final graph has schema errors")
    args = ap.parse_args()

    if not args.mock:
        missing = [k for k in ("BRINE_URL", "BRINE_MODEL", "BRINE_API_KEY") if not os.environ.get(k)]
        if missing:
            sys.exit("Missing environment variables: " + ", ".join(missing))
    index = load_index(args.corpus)
    files = sorted(Path(args.corpus).glob("*.md"))
    if args.slugs:
        files = [f for f in files if f.stem in set(args.slugs)]
    if args.limit:
        files = files[:args.limit]
    if not files:
        sys.exit("No articles to process.")
    llm = MockLLM() if args.mock else LLM(thinking=args.thinking, use_cache=not args.no_cache)

    results, t0 = [], time.time()
    with cf.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futs = {pool.submit(annotate_article, f, index.get(f.stem, {}), llm): f for f in files}
        for i, fut in enumerate(cf.as_completed(futs), 1):
            f = futs[fut]
            try:
                r = fut.result()
                results.append(r)
                s = r["stats"]
                print(f"[{i}/{len(files)}] {f.stem}: {len(r['nodes'])} nodes, {len(r['edges'])} edges "
                      f"({s['prompt_tokens'] + s['completion_tokens']} tokens, {s['seconds']}s)", flush=True)
            except Exception as exc:       # one bad article must not kill the run
                print(f"[{i}/{len(files)}] {f.stem}: FAILED {exc}", file=sys.stderr, flush=True)
                results.append({"slug": f.stem, "doc": source_document(f.stem, index.get(f.stem, {})),
                                "nodes": [], "edges": [], "stats": {**new_stats(), "error": str(exc)}})

    results.sort(key=lambda r: r["slug"])
    nodes, edges, docs = {}, {}, []
    for r in results:
        docs.append(r["doc"])
        for n in r["nodes"]:
            nodes.setdefault(n["id"], n)
        for e in r["edges"]:
            edges.setdefault(e["id"], e)
    graph = wrap(list(nodes.values()), list(edges.values()), docs)

    errors = reference_errors(graph)
    off = official_errors(graph)
    if off is None:
        print("[warn] linkml is not installed: the final graph was NOT validated against the schema.",
              file=sys.stderr)
    else:
        errors += off
    if not graph["edges"]:
        errors.append("graph has no edges (CausalGraph.edges is required)")

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "camo_graph.yaml", "w", encoding="utf-8") as fh:
        yaml.safe_dump(graph, fh, sort_keys=False, allow_unicode=True, width=120)
    with open(out / "camo_graph.json", "w", encoding="utf-8") as fh:
        json.dump(graph, fh, indent=2, ensure_ascii=False)
    totals = {k: sum(r["stats"].get(k, 0) for r in results) for k in new_stats()}
    totals.update(articles=len(results), nodes=len(graph["nodes"]), edges=len(graph["edges"]),
                  seconds=round(time.time() - t0, 1), final_schema_errors=len(errors))
    with open(out / "run_stats.json", "w", encoding="utf-8") as fh:
        json.dump({"totals": totals, "per_article": {r["slug"]: r["stats"] for r in results}}, fh, indent=2)

    print(f"\n{totals['articles']} articles -> {totals['nodes']} nodes, {totals['edges']} edges; "
          f"{totals['prompt_tokens'] + totals['completion_tokens']} tokens; {totals['seconds']}s")
    print(f"Wrote {out / 'camo_graph.yaml'} and {out / 'camo_graph.json'}")
    if errors:
        print(f"\nFINAL GRAPH HAS {len(errors)} PROBLEM(S) (first 10):", file=sys.stderr)
        for e in errors[:10]:
            print("  -", e[:200], file=sys.stderr)
        if args.strict:
            sys.exit(1)
    elif off is None:
        print("Reference checks passed (schema validation skipped: linkml not installed).")
    else:
        print("Final graph passed LinkML schema validation.")


if __name__ == "__main__":
    main()