#!/usr/bin/env python3
"""
Phase 1: one finding sentence -> validated CAMO graph (Causal Mosaic 0.10.0).

    export BRINE_API_KEY=sk-KTB9jY2m12o9ijQ-aUxIT          # your key
    export BRINE_URL=https://brine.sharcnet.ca/v1     # OpenAI-compatible endpoint (name from the Evidence Jam instructions)
    export BRINE_MODEL=Qwen3.8-27B      # as configured by your team
    python phase1.py                    # annotates the built-in Murphy f1 sentence
    python phase1.py --text "Your finding sentence here."
    python phase1.py --mock             # no API call: tests the plumbing offline

Pipeline:  sentence -> LLM (JSON draft) -> code builds ids/provenance/wrapper
           -> checks (schema-driven + CAMO invariants + LinkML if installed)
           -> repair loop (errors go back to the LLM) -> YAML file.

The LLM never writes ids, provenance or schema_version. Code does.
Phase 1 covers Tier 1 fields only (see the CAMO Annotation Essentials guide).
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.environ.get("CAMO_SCHEMA", os.path.join(HERE, "causalmosaic.yaml"))
MAX_REPAIRS = 3
EXPORTER = "phase1-0.1"

DEFAULT_FINDING = (
    "In a field additive-series experiment in a degraded woodlot near Waterloo, "
    "Ontario, as of 2000 (transplanted 1997), initial Sanguinaria canadensis "
    "densities of as little as 5 ramets/m2 suppressed Alliaria petiolata."
)

# DEMO ONLY. Phase 3 replaces this with a real NCBI Taxonomy lookup. IDs are the
# ones given in the CAMO Annotation Essentials guide for Murphy (2005).
KNOWN_TAXA = {
    "sanguinaria canadensis": "NCBITaxon:3472",
    "alliaria petiolata": "NCBITaxon:126270",
}

with open(SCHEMA_PATH, encoding="utf-8") as fh:
    SCHEMA = yaml.safe_load(fh)
ENUMS = SCHEMA["enums"]
CLASSES = SCHEMA["classes"]


def enum_values(name):
    """Allowed values straight from the schema, so prompts never drift from it."""
    return list(ENUMS[name]["permissible_values"])


# ----------------------------------------------------------------------------
# 1. Prompt
# ----------------------------------------------------------------------------

EXAMPLE_SENTENCE = (
    "Alliaria petiolata number of seedlings in early spring was not affected "
    "by initial Sanguinaria canadensis density."
)
EXAMPLE_JSON = {
    "nodes": [
        {"key": "n1", "name": "Increased initial Sanguinaria canadensis ramet density",
         "entity_type": "taxon", "entity_term": "Sanguinaria canadensis",
         "measured_attribute": "initial planting density",
         "state_or_change_qualifier": "increased", "life_stage": "ramet_or_clonal_unit"},
        {"key": "n2", "name": "Alliaria petiolata seedling number",
         "entity_type": "taxon", "entity_term": "Alliaria petiolata",
         "measured_attribute": "number of individuals",
         "life_stage": "germinant_or_seedling"},
    ],
    "edges": [
        {"subject": "n1", "predicate": "causes", "object": "n2",
         "causal_language": "no_relationship", "negated": True,
         "philosophical_accounts": ["interventionist"]},
    ],
}


def build_system_prompt():
    return f"""You annotate causal claims from ecology papers using the Causal Mosaic (CAMO) schema.
You receive ONE finding sentence. Return ONE JSON object with "nodes" and "edges". Return JSON only.

NODES. A node is a state or change of a property of a thing, never just a thing:
  entity_term (the thing) + measured_attribute (the property) + state_or_change_qualifier (what happened).
Fields per node:
  key                       short local label you invent, e.g. "n1" (edges refer to it)
  name                      readable label combining the three parts, e.g. "Decreased Alliaria petiolata seedling number"
  entity_type               one of {enum_values('EntityTypeEnum')}
  entity_term               for taxa, the scientific name only (a lookup adds the ID later). Never invent ontology IDs.
  measured_attribute        free text, e.g. "number of individuals", "initial planting density"
  state_or_change_qualifier one of {enum_values('StateOrChangeQualifierEnum')}  (omit if the text gives no direction)
  life_stage                optional, one of {enum_values('LifeStageEnum')}
Rules: life stage goes in life_stage, NOT in measured_attribute. One node per separately reported
outcome. Record dose levels on neither node; Phase 1 ignores them.

EDGES. Fields per edge:
  subject, object           node keys
  predicate                 one of {enum_values('CausalPredicateEnum')}  (usually causes, contributes_to, prevents or associated_with)
  causal_language           one of {enum_values('ClaimStrengthEnum')}, copied from the authors' wording:
                              direct_causal = unhedged causal verb; uncertain_causal = hedged ("may", "appears");
                              associational = co-occurrence only; no_relationship = explicit null result
  negated                   true if and only if causal_language is no_relationship
  philosophical_accounts    list, at least one, from {enum_values('PhilosophicalAccountEnum')}
                              (interventionist = something was manipulated; counterfactual = with vs without / more vs less;
                               mechanistic = a pathway or how; probabilistic = "usually"/"tends to"; agency = a means for managers)
Rules:
- A null result ("not affected", "no effect") keeps the positive predicate (e.g. causes), sets
  causal_language no_relationship and negated true. Never use the qualifier "unchanged" for a null result.
- No double negatives: do not use "prevents" with a "decreased" object. Use prevents -> X, or causes -> decreased X.
- associated_with, correlated_with and precedes only take associational or no_relationship.
  causes never takes associational.
- Only record what the sentence says. Do not guess.

EXAMPLE
Sentence: {EXAMPLE_SENTENCE}
Output: {json.dumps(EXAMPLE_JSON)}"""


def build_user_prompt(sentence):
    return f"Sentence: {sentence}\nOutput JSON only."


# ----------------------------------------------------------------------------
# 2. LLM call (OpenAI-compatible endpoint)
# ----------------------------------------------------------------------------

def strip_thinking(text):
    """Qwen3.x thinking mode starts replies with <think>...</think>; drop it."""
    if "</think>" in text:
        return text.split("</think>")[-1].strip()
    if text.lstrip().startswith("<think>"):
        raise ValueError("Reply is only an unfinished <think> block (hit max_tokens). "
                         "Raise BRINE_MAX_TOKENS or lower reasoning effort.")
    return text.strip()


def parse_json(text):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object found in model reply: {text[:200]!r}")
    return json.loads(text[start:end + 1])


_client = None


def ask(messages, thinking=True):
    """Call the model. Needs `pip install openai` and BRINE_* environment variables."""
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(base_url=os.environ.get("BRINE_URL") or os.environ["BRINE_BASE_URL"], api_key=os.environ["BRINE_API_KEY"])
    kwargs = dict(
        model=os.environ["BRINE_MODEL"],
        messages=messages,
        temperature=float(os.environ.get("BRINE_TEMPERATURE", "0.6")),
        max_tokens=int(os.environ.get("BRINE_MAX_TOKENS", "8000")),
    )
    try:
        resp = _client.chat.completions.create(
            **kwargs, extra_body={"chat_template_kwargs": {"enable_thinking": thinking}})
    except Exception as exc:  # some hosted APIs reject chat_template_kwargs
        print(f"[warn] retrying without enable_thinking switch: {exc}", file=sys.stderr)
        resp = _client.chat.completions.create(**kwargs)
    return strip_thinking(resp.choices[0].message.content or "")


# ----------------------------------------------------------------------------
# 3. Build the CausalGraph wrapper (code, not the LLM, writes ids/provenance)
# ----------------------------------------------------------------------------

def slug(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40]


def ground_entity_term(node):
    """Phase 1 stub. Phase 3: replace with a real NCBI Taxonomy lookup."""
    if node.get("entity_type") == "taxon":
        curie = KNOWN_TAXA.get(str(node.get("entity_term", "")).strip().lower())
        if curie:
            node["entity_term"] = curie
    return node


def build_graph(draft, sentence, finding_id, doc_id):
    key_to_id, nodes = {}, []
    for n in draft.get("nodes", []):
        n = dict(n)
        key = n.pop("key", None)
        n = {k: v for k, v in n.items() if v not in (None, "", [])}
        n = ground_entity_term(n)
        h = hashlib.sha1(f"{finding_id}|{n.get('name', '')}".encode()).hexdigest()[:4]
        n["id"] = f"causal_mosaic:{slug(n.get('name', 'node'))}_{h}"
        key_to_id[key] = n["id"]
        nodes.append(n)

    edges = []
    for i, e in enumerate(draft.get("edges", []), start=1):
        e = {k: v for k, v in dict(e).items() if v not in (None, "", [])}
        # keep unresolved keys visible so the checker can report them
        e["subject"] = key_to_id.get(e.get("subject"), e.get("subject"))
        e["object"] = key_to_id.get(e.get("object"), e.get("object"))
        e["id"] = f"{finding_id}-e{i}"
        e["original_sentence"] = sentence
        e["source_document"] = doc_id
        edges.append(e)

    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
    return {
        "graph_id": f"causal_mosaic:graph_{finding_id}",
        "schema_version": SCHEMA["version"],
        "provenance": {
            "causal_mosaic_version": SCHEMA["version"],
            "created": now,
            "project": "hackathon",
            "exporter_version": f"{EXPORTER} ({os.environ.get('BRINE_MODEL', 'unknown-model')})",
        },
        "source_documents": [{"document_id": doc_id}],
        "nodes": nodes,
        "edges": edges,
    }


# ----------------------------------------------------------------------------
# 4. Checks. Schema-driven first, then the CAMO rules LinkML cannot enforce.
# ----------------------------------------------------------------------------

STAGE_WORDS = re.compile(r"\b(seedlings?|rosettes?|germinants?|juveniles?|adults?|seed bank)\b", re.I)


def check_object(obj, cls, where):
    """Generic checks for any node/edge dict, driven by the schema."""
    errs, attrs = [], CLASSES[cls]["attributes"]
    for key, val in obj.items():
        if key not in attrs:
            errs.append(f"{where}: unknown field '{key}'. Remove it.")
            continue
        spec = attrs[key] if isinstance(attrs[key], dict) else {}
        rng = spec.get("range")
        if spec.get("multivalued") and not isinstance(val, list):
            errs.append(f"{where}.{key}: must be a list.")
            continue
        if rng in ENUMS:
            allowed = enum_values(rng)
            for v in (val if isinstance(val, list) else [val]):
                if v not in allowed:
                    errs.append(f"{where}.{key}: '{v}' is not allowed. Allowed: {allowed}")
    for key, spec in attrs.items():
        if isinstance(spec, dict) and spec.get("required") and key not in obj:
            errs.append(f"{where}: missing required field '{key}'.")
    return errs


def check_camo_rules(graph):
    errs, by_id = [], {n.get("id"): n for n in graph["nodes"]}
    if not graph["nodes"]:
        errs.append("graph has no nodes.")
    if not graph["edges"]:
        errs.append("graph has no edges.")
    for n in graph["nodes"]:
        w = f"node '{n.get('name', '?')}'"
        errs += check_object(n, "CausalNode", w)
        if STAGE_WORDS.search(str(n.get("measured_attribute", ""))):
            errs.append(f"{w}: life stage found in measured_attribute. Use 'number of individuals' "
                        "(or similar) and put the stage in life_stage.")
    for e in graph["edges"]:
        w = f"edge {e.get('id', '?')}"
        errs += check_object(e, "CausalEdge", w)
        for end in ("subject", "object"):
            if e.get(end) not in by_id:
                errs.append(f"{w}: {end} '{e.get(end)}' is not a node key from your nodes list.")
        if not e.get("philosophical_accounts"):
            errs.append(f"{w}: philosophical_accounts needs at least one value.")
        lang, neg, pred = e.get("causal_language"), e.get("negated", False), e.get("predicate")
        if neg != (lang == "no_relationship"):
            errs.append(f"{w}: negated must be true if and only if causal_language is no_relationship.")
        if pred in ("associated_with", "correlated_with", "precedes") and lang not in ("associational", "no_relationship"):
            errs.append(f"{w}: predicate '{pred}' may only use associational or no_relationship.")
        if pred == "causes" and lang == "associational":
            errs.append(f"{w}: 'causes' cannot use associational language.")
        obj = by_id.get(e.get("object"), {})
        if (neg or lang == "no_relationship") and obj.get("state_or_change_qualifier") == "unchanged":
            errs.append(f"{w}: null result must not use qualifier 'unchanged' on the object node. Omit the qualifier.")
        if pred == "prevents" and obj.get("state_or_change_qualifier") == "decreased":
            errs.append(f"{w}: double negative ('prevents' + 'decreased'). Use prevents -> X or causes -> decreased X.")
    return errs


def check_linkml(graph):
    """Full schema validation if linkml is installed (pip install linkml). None = skipped."""
    try:
        from linkml.validator import validate
    except ImportError:
        return None
    report = validate(graph, SCHEMA_PATH, "CausalGraph")
    return [f"LinkML: {r.message}" for r in report.results]


def all_errors(graph):
    errs = check_camo_rules(graph)
    lm = check_linkml(graph)
    return errs + (lm or []), lm is not None


# ----------------------------------------------------------------------------
# 5. Annotate with repair loop
# ----------------------------------------------------------------------------

def annotate(sentence, finding_id="f1", doc_id="doc1", llm=ask, verbose=True):
    messages = [{"role": "system", "content": build_system_prompt()},
                {"role": "user", "content": build_user_prompt(sentence)}]
    reply = llm(messages, thinking=True)
    for attempt in range(MAX_REPAIRS + 1):
        try:
            draft = parse_json(reply)
            graph = build_graph(draft, sentence, finding_id, doc_id)
            errs, used_linkml = all_errors(graph)
        except (ValueError, json.JSONDecodeError) as exc:
            graph, errs, used_linkml = None, [f"Could not parse your reply as JSON: {exc}"], False
        if verbose:
            print(f"[attempt {attempt}] {len(errs)} problem(s)"
                  f"{'' if used_linkml else ' (LinkML not installed: custom checks only)'}")
            for e in errs:
                print("   -", e)
        if not errs:
            return graph, 0 if attempt == 0 else attempt
        if attempt == MAX_REPAIRS:
            return graph, -1
        messages += [{"role": "assistant", "content": reply},
                     {"role": "user", "content": "Fix these problems and return the full corrected JSON only:\n"
                      + "\n".join(f"- {e}" for e in errs)}]
        reply = llm(messages, thinking=False)   # cheap repair call, no thinking


# ----------------------------------------------------------------------------
# 6. Mock LLM for offline testing: first reply is deliberately wrong
# ----------------------------------------------------------------------------

def mock_llm_factory():
    bad = {"nodes": [
        {"key": "n1", "name": "Increased initial Sanguinaria canadensis ramet density", "entity_type": "taxon",
         "entity_term": "Sanguinaria canadensis", "measured_attribute": "initial planting density",
         "state_or_change_qualifier": "increased"},
        {"key": "n2", "name": "Decreased Alliaria petiolata seedling number", "entity_type": "taxon",
         "entity_term": "Alliaria petiolata", "measured_attribute": "number of seedlings",
         "state_or_change_qualifier": "decreased"}],
        "edges": [{"subject": "n1", "predicate": "prevents", "object": "n2",
                   "causal_language": "direct_causal", "philosophical_accounts": ["experiment"]}]}
    good = {"nodes": [
        {"key": "n1", "name": "Increased initial Sanguinaria canadensis ramet density", "entity_type": "taxon",
         "entity_term": "Sanguinaria canadensis", "measured_attribute": "initial planting density",
         "state_or_change_qualifier": "increased", "life_stage": "ramet_or_clonal_unit"},
        {"key": "n2", "name": "Decreased Alliaria petiolata performance", "entity_type": "taxon",
         "entity_term": "Alliaria petiolata", "measured_attribute": "performance",
         "state_or_change_qualifier": "decreased"}],
        "edges": [{"subject": "n1", "predicate": "causes", "object": "n2", "causal_language": "direct_causal",
                   "philosophical_accounts": ["interventionist", "counterfactual"]}]}
    replies = iter(["<think>hmm</think>```json\n" + json.dumps(bad) + "\n```", json.dumps(good)])

    def llm(messages, thinking=True):
        return strip_thinking(next(replies))
    return llm


# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--text", default=DEFAULT_FINDING, help="finding sentence to annotate")
    ap.add_argument("--id", default="f1", help="finding id used in node/edge ids")
    ap.add_argument("--out", default="out", help="output folder")
    ap.add_argument("--mock", action="store_true", help="use a fake LLM (no API call)")
    args = ap.parse_args()

    llm = mock_llm_factory() if args.mock else ask
    graph, status = annotate(args.text, finding_id=args.id, llm=llm)
    if graph is None:
        sys.exit("No parseable output from the model.")
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, f"{args.id}.yaml")
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(graph, fh, sort_keys=False, allow_unicode=True)
    print(f"\n{'VALID' if status >= 0 else 'STILL HAS ERRORS'}"
          f"{' after ' + str(status) + ' repair(s)' if status > 0 else ''} -> {path}")
    print("\nRead it aloud:")
    names = {n["id"]: n["name"] for n in graph["nodes"]}
    for e in graph["edges"]:
        neg = "does NOT " if e.get("negated") else ""
        print(f"  {names.get(e['subject'], e['subject'])}  {neg}{e['predicate']}  {names.get(e['object'], e['object'])}")


if __name__ == "__main__":
    main()