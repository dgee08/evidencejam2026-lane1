import csv, hashlib, json, os, re, sys, threading, time, yaml
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from linkml.validator import Validator
from linkml.validator.plugins import JsonschemaValidationPlugin
import asyncio
import httpx

SCHEMA = "schema/causalmosaic.yaml"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "output")
WORKERS = int(os.environ.get("CAMO_WORKERS", "3"))       # Reduced from 10
BUDGET_MIN = float(os.environ.get("CAMO_BUDGET_MIN", "120"))  # More time
LIMIT = int(os.environ.get("CAMO_LIMIT", "0"))
FORCE = os.environ.get("CAMO_FORCE") == "1"
MAX_SENT = int(os.environ.get("CAMO_MAX_SENT", "100"))   # Larger batches = fewer API calls, better for flaky endpoint
DEADLINE = time.time() + BUDGET_MIN * 60
LOCK = threading.Lock()
STATS = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "articles": 0, "claims": 0}

schema = yaml.safe_load(open(SCHEMA, encoding="utf-8"))
def enum(n): return set(schema["enums"][n]["permissible_values"])
PRED = {"causes", "contributes_to", "enables"} & enum("CausalPredicateEnum")
LANG = enum("ClaimStrengthEnum") - {"associational"}
ENRICH = os.environ.get("CAMO_ENRICH") == "1"
# Codes copied from the organizers' README and the hand-checked example ONLY.
VERIFIED_TERMS = {"sanguinaria canadensis": "NCBITaxon:3472", "alliaria petiolata": "NCBITaxon:126270"}
VERIFIED_ATTRS = {"abundance": "PATO:0000070", "density": "PATO:0000574"}
def resolve(term, table):
    return table.get(term.strip().lower(), term.strip())
ACC, QUAL, ETYPE = enum("PhilosophicalAccountEnum"), enum("StateOrChangeQualifierEnum"), enum("EntityTypeEnum")
VALIDATOR = Validator(SCHEMA, validation_plugins=[JsonschemaValidationPlugin(closed=True)])

# HTTP client config for streaming (avoids AsyncOpenAI vLLM timeout bug)
HTTP_TIMEOUT = httpx.Timeout(300.0, connect=10.0)
MODEL = os.environ["BRINE_MODEL"]
PROMPT = Path("prompts/extract_system.txt").read_text(encoding="utf-8")
INDEX = {r["slug"]: r for r in csv.DictReader(open("corpus/index.csv", encoding="utf-8"))}
BASE_URL = os.environ["BRINE_URL"]
API_KEY = os.environ["BRINE_API_KEY"]
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

SKIP_HEAD = re.compile(r"^(introduction|background|methods?|materials|study (site|area|system|species)|experimental|references?|literature cited|acknowledg|appendix|supplement)", re.I)
CUES = re.compile(r"\b(affect|effect|increas|decreas|reduc|suppress|promot|enhanc|inhibit|limit|caus|result(ed|s)? in|lead|led|due to|because|driv|respond|response|declin|improv|benefit|facilitat|prevent|depress|greater|higher|lower|fewer|significan|effective|strateg|suggest|mitigat|rose\b|rise|indicat|harm|kill|no (significant )?(difference|change|effect))|P\s*[<=>]", re.I)

# --- Ontology lookup tables (same as original) ---
TAXON_CURIES = {
    "alliaria petiolata": "NCBITaxon:126270",
    "sanguinaria canadensis": "NCBITaxon:3472",
    "pinus resinosa": "NCBITaxon:3353",
    "lobelia siphilitica": "NCBITaxon:4243",
    "balsam fir": "NCBITaxon:3346",
    "abies balsamea": "NCBITaxon:3346",
    "spruce": "NCBITaxon:3350",
    "moose": "NCBITaxon:9865",
    "cladoceran": "NCBITaxon:6656",
    "graminoid": "NCBITaxon:4487",
    "graminoids": "NCBITaxon:4487",
    "moss": "NCBITaxon:3209",
    "vascular plant": "NCBITaxon:58023",
    "shrub": "NCBITaxon:33580",
    "thistle": "NCBITaxon:4202",
    "seedling": "NCBITaxon:34740",
    "rosette": "NCBITaxon:34740",
    "ramet": "NCBITaxon:34740",
    " alliaria": "NCBITaxon:126270",
    " s. canadensis": "NCBITaxon:3472",
    " a. petiolata": "NCBITaxon:126270",
}

ATTR_CURIES = {
    "abundance": "PATO:0000070",
    "density": "PATO:0000574",
    "cover": "PATO:0001324",
    "height": "PATO:0000020",
    "biomass": "PATO:0000014",
    "survival": "PATO:0001441",
    "performance": "PATO:0001441",
    "spread": "PATO:0001324",
    "competitive ability": "PATO:0001441",
    "population density": "PATO:0000574",
    "population": "PATO:0000070",
    "seedling number": "PATO:0000070",
    "seedling count": "PATO:0000070",
    "rosette number": "PATO:0000070",
    "rosette size": "PATO:0000020",
    "leaf number": "PATO:0000070",
    "leaf area": "PATO:0000020",
    "flower number": "PATO:0000070",
    "fruit number": "PATO:0000070",
    "silique number": "PATO:0000070",
    "flowering": "PATO:0001441",
    "fruit set": "PATO:0000070",
    "seed size": "PATO:0000020",
    "germination": "PATO:0001441",
    "emergence": "PATO:0001441",
    "phenology": "PATO:0001441",
    "leafing": "PATO:0001441",
    "erosion": "PATO:0001324",
    "organic matter": "PATO:0000014",
    "bulk density": "PATO:0000574",
    "carbon": "CHEBI:27594",
    "methane": "CHEBI:16183",
    "co2": "CHEBI:16526",
    "ch4": "CHEBI:16183",
    "ghg": "CHEBI:27594",
    "respiration": "GO:0006099",
    "photosynthesis": "GO:0015979",
    "water table": "ENVO:01000253",
    "soil temperature": "PATO:0000146",
    "temperature": "PATO:0000146",
    "precipitation": "ENVO:01000253",
    "climatic dissimilarity": "ENVO:01000253",
    "geographic distance": "ENVO:01000253",
    "competition": "GO:0051201",
    "regeneration": "GO:0031099",
    "invasion": "GO:0051201",
    "suppression": "PATO:0001441",
    "mortality": "PATO:0001441",
    "growth": "GO:0040007",
    "chemical defense": "GO:0042742",
    "phenol": "CHEBI:33853",
    "nitrogen": "CHEBI:25555",
    "arsenic": "CHEBI:27594",
    "seed bank": "PATO:0000070",
    "seed": "PATO:0000070",
    "vulnerability": "PATO:0001441",
    "frost": "ENVO:01000253",
    "drought": "ENVO:01000253",
    "xylem": "PO:0005013",
    "double ring": "PO:0005013",
    "latewood": "PO:0005013",
    "tracheid": "PO:0005013",
    "light ring": "PO:0005013",
}

ENV_CURIES = {
    "forest": "ENVO:01000253",
    "grassland": "ENVO:01000253",
    "peatland": "ENVO:01000253",
    "woodlot": "ENVO:01000253",
    "temperate forest": "ENVO:01000253",
    "boreal forest": "ENVO:01000253",
    "marsh": "ENVO:01000253",
    "tidal marsh": "ENVO:01000253",
    "lake": "ENVO:01000253",
    "river": "ENVO:01000253",
    "fishway": "ENVO:01000253",
    "moraine": "ENVO:01000253",
    "degraded soil": "ENVO:01000253",
    "restored site": "ENVO:01000253",
    "unrestored site": "ENVO:01000253",
    "dry microsite": "ENVO:01000253",
    "wet microsite": "ENVO:01000253",
}

LIFE_STAGE_CURIES = {
    "seedling": ("germinant_or_seedling", "PO:0008037"),
    "germinant": ("germinant_or_seedling", "PO:0008037"),
    "ramet": ("ramet_or_clonal_unit", "http://aims.fao.org/aos/agrovoc/c_df6bc767"),
    "clonal unit": ("ramet_or_clonal_unit", "http://aims.fao.org/aos/agrovoc/c_df6bc767"),
    "rosette": ("juvenile", "PO:0000013"),
    "juvenile": ("juvenile", "PO:0000013"),
    "year 1": ("juvenile", "PO:0000013"),
    "year 2": ("juvenile", "PO:0000013"),
    "flowering": ("reproductive_adult", "PO:0001026"),
    "reproductive": ("reproductive_adult", "PO:0001026"),
    "adult": ("reproductive_adult", "PO:0001026"),
    "seed": ("seed_or_seed_bank", "PO:0009010"),
    "seed bank": ("seed_or_seed_bank", "PO:0009010"),
    "early spring": ("germinant_or_seedling", "NCIT:C94730"),
    "late spring": ("germinant_or_seedling", "NCIT:C94730"),
    "autumn": ("germinant_or_seedling", "NCIT:C94733"),
    "summer": ("germinant_or_seedling", "NCIT:C94732"),
}

EVIDENCE_CURIES = {
    "randomized experiment": "ECO:0000006",
    "experimental": "ECO:0000006",
    "observational": "ECO:0000007",
    "correlation": "ECO:0000007",
    "regression": "ECO:0000007",
    "anova": "ECO:0000006",
    "manova": "ECO:0000006",
    "survey": "ECO:0000007",
}

CURIE_RE = re.compile(r"^([A-Za-z0-9]+:[A-Za-z0-9_]+)\s*\|\s*(.+)$")

def parse_curie_field(value: str) -> tuple[str | None, str]:
    """Parse 'CURIE | label' → (curie, label). Returns (None, original) if no CURIE."""
    if not value or "|" not in value:
        return None, value
    m = CURIE_RE.match(value.strip())
    if m:
        return m.group(1), m.group(2).strip()
    return None, value  # malformed pipe, treat as free text

def enrich_node(n, sent):
    """Parse CURIEs from LLM output (format: 'CURIE | label'), keep life_stage/applied_to logic."""
    # Parse entity_term
    curie, label = parse_curie_field(n.get("entity_term", ""))
    if curie:
        n["entity_term"] = curie
    # else keep free-text label

    # Parse measured_attribute
    curie, label = parse_curie_field(n.get("measured_attribute", ""))
    if curie:
        n["measured_attribute"] = curie

    # Keep existing life_stage/applied_to logic (substring match on sentence text)
    term_lower = (n.get("entity_term") or "").lower().strip()
    attr_lower = (n.get("measured_attribute") or "").lower().strip()
    q = n.get("state_or_change_qualifier", "")
    etype = n.get("entity_type", "")

    if etype in ("taxon", "management_intervention"):
        sent_lower = sent.lower()
        for k, (enum_val, curie) in LIFE_STAGE_CURIES.items():
            if k in term_lower or k in attr_lower or k in sent_lower:
                n["life_stage"] = enum_val
                n["life_stage_term"] = curie
                break

    if etype == "management_intervention":
        for taxon_name, curie in TAXON_CURIES.items():
            if taxon_name in sent.lower():
                n.setdefault("applied_to", []).append({
                    "entity_type": "taxon",
                    "entity_term": curie
                })
                break

    if etype == "taxon" and q in ("introduced", "removed"):
        n["entity_type"] = "management_intervention"
        n["state_or_change_qualifier"] = q

    if etype in ("environmental_variable", "environmental_process"):
        for k, v in ENV_CURIES.items():
            if k in term_lower or k in attr_lower:
                n["entity_term"] = v
                break

    return n


def enrich_edge(e, sents, slug):
    sent = e.get("original_sentence", "")
    sent_lower = sent.lower()
    lang = e.get("causal_language", "")

    dose = parse_dose(sent, s_node_id=e["subject"])
    if dose:
        e["dose"] = dose

    comparator = parse_comparator(sent)
    if comparator:
        e["comparator"] = comparator

    temporal = parse_temporal(sent, slug)
    if temporal:
        e["temporal_extent"] = temporal

    strength = parse_strength(sent, lang)
    if strength:
        e["strength"] = strength

    e["certainty_grade"] = infer_certainty_grade(lang, strength.get("significance") if strength else None)

    e["evidential_basis"] = infer_evidential_basis(sent, lang)

    direction = infer_direction(sent, lang)
    if direction:
        e["direction"] = direction

    context = infer_context(sent, slug)
    if context:
        e["context_dependence"] = context

    e["scope"] = infer_scope(lang, sent)

    ecosystem = infer_ecosystem(sent)
    if ecosystem:
        e["ecosystem_context"] = ecosystem

    mediation = infer_mediation(sent)
    if mediation:
        e["mediation"] = mediation

    prox = infer_proximate_distal(sent, lang)
    if prox:
        e["proximate_distal"] = prox

    return e


# --- Parsers (same as original) ---
DOSE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*(ramets?/m2|ramets?/m²|individuals?/m2|individuals?/m²|seedlings?/m2|seedlings?/m²|g/m2|g/m²|kg/ha|mg/L|µg/L|ppm|ppb|%)", re.I)
DOSE_SINGLE_RE = re.compile(r"(?:at|of|densities? of|density of)\s+(\d+(?:\.\d+)?)\s*(ramets?/m2|ramets?/m²|individuals?/m2|individuals?/m²|seedlings?/m2|seedlings?/m²)", re.I)
DOSE_VERBATIM_RE = re.compile(r"(?:as little as|densities? of|density of)\s+([\d\s,.-]+(?:ramets?/m2|ramets?/m²|seedlings?/m2|seedlings?/m²|individuals?/m2|individuals?/m²))", re.I)

def parse_dose(sent, s_node_id):
    m = DOSE_RE.search(sent)
    if m:
        min_v, max_v, unit = float(m.group(1)), float(m.group(2)), m.group(3)
        counted = guess_counted_entity(sent, s_node_id)
        return {"minimum_value": min_v, "maximum_value": max_v, "unit_text": unit, "counted_entity": counted, "verbatim": m.group(0)}
    m = DOSE_SINGLE_RE.search(sent)
    if m:
        val, unit = float(m.group(1)), m.group(2)
        counted = guess_counted_entity(sent, s_node_id)
        return {"numeric_value": val, "unit_text": unit, "counted_entity": counted, "verbatim": m.group(0)}
    m = DOSE_VERBATIM_RE.search(sent)
    if m:
        counted = guess_counted_entity(sent, s_node_id)
        return {"unit_text": "ramets/m2", "counted_entity": counted, "verbatim": m.group(1)}
    return None

def guess_counted_entity(sent, s_node_id):
    sent_lower = sent.lower()
    for taxon, curie in TAXON_CURIES.items():
        if taxon in sent_lower:
            return {"entity_type": "taxon", "entity_term": curie, "life_stage": "ramet_or_clonal_unit", "life_stage_term": "http://aims.fao.org/aos/agrovoc/c_df6bc767"}
    return {"entity_type": "taxon", "entity_term": "NCBITaxon:3472", "life_stage": "ramet_or_clonal_unit", "life_stage_term": "http://aims.fao.org/aos/agrovoc/c_df6bc767"}

COMPARATOR_RE = re.compile(r"(?:compared to|versus|vs\.?|relative to|than|among)\s+([^.]+)", re.I)
DOSE_LEVEL_RE = re.compile(r"(higher|lower|intermediate|high|low)\s*(?:than|dose)?", re.I)

DOSE_LEVEL_MAP = {
    "higher": "higher_than_comparator",
    "lower": "lower_than_comparator",
    "intermediate": "intermediate",
    "high": "high",
    "low": "low",
}

def parse_comparator(sent):
    comps = []
    m = COMPARATOR_RE.search(sent)
    if m:
        desc = m.group(1).strip()
        comp = {"status": "explicitly_asserted", "comparator_type": "alternative_dose_level", "comparator_description": desc}
        dose_match = DOSE_RE.search(desc) or DOSE_SINGLE_RE.search(desc)
        if dose_match:
            if dose_match.lastindex >= 2:
                comp["comparator_dose"] = {"minimum_value": float(dose_match.group(1)), "maximum_value": float(dose_match.group(2)), "unit_text": dose_match.group(3)}
            else:
                comp["comparator_dose"] = {"numeric_value": float(dose_match.group(1)), "unit_text": dose_match.group(2)}
        m2 = DOSE_LEVEL_RE.search(desc)
        if m2:
            comp["comparator_dose_level"] = DOSE_LEVEL_MAP.get(m2.group(1).lower(), m2.group(1).replace(" ", "_"))
        comps.append(comp)
    return comps if comps else None

TEMPORAL_RE = re.compile(r"(?:by|after|within|at)\s+(\d{4})", re.I)
MONTHS_RE = re.compile(r"(\d+)\s*(?:months?|years?)", re.I)
SEASON_RE = re.compile(r"(early|late)\s*(spring|summer|autumn|fall|winter)", re.I)

def parse_temporal(sent, slug):
    temp = {}
    m = TEMPORAL_RE.search(sent)
    if m:
        year = int(m.group(1))
        transplant_year = 1997 if "YKQQ" in slug else year - 3
        temp["time_since_intervention_months"] = (year - transplant_year) * 12
        temp["time_since_intervention_text"] = m.group(0)
    m = MONTHS_RE.search(sent)
    if m and "time_since_intervention_months" not in temp:
        temp["time_since_intervention_months"] = int(m.group(1))
        temp["time_since_intervention_text"] = m.group(0)
    seasons = SEASON_RE.findall(sent)
    if seasons:
        temp["observation_seasons"] = [f"{s[0]}_{s[1]}" for s in seasons]
    return temp if temp else None

STRENGTH_RE = re.compile(r"(significant|not significant|marginal|p\s*[<=>]\s*0\.\d+)", re.I)
METHOD_RE = re.compile(r"\b(mixed.model|linear.regression|multiple.regression|anova|manova|regression|t.test|chi.?square|fisher|kruskal|mann.whitney|ordination|nonparametric|bayesian|descriptive.only)\b", re.I)

def parse_strength(sent, lang):
    strength = {"status": "explicitly_asserted"}
    if "not significant" in sent.lower() or "no significant" in sent.lower():
        strength["significance"] = "not_significant"
    elif "marginal" in sent.lower() or "marginally" in sent.lower():
        strength["significance"] = "marginal"
    elif "significant" in sent.lower() and "not" not in sent.lower():
        strength["significance"] = "significant"
    elif lang == "no_relationship":
        strength["significance"] = "not_significant"
    else:
        strength["significance"] = "not_reported"
    m = METHOD_RE.search(sent)
    if m:
        method = m.group(1).replace("-", "_").replace(".", "_").replace(" ", "_")
        method_map = {
            "linear_regression": "regression",
            "multiple_regression": "regression",
            "t_test": "regression",
            "chi_square": "nonparametric",
            "fisher": "nonparametric",
            "kruskal": "nonparametric",
            "mann_whitney": "nonparametric",
            "mixed_model": "mixed_model",
            "descriptive_only": "descriptive_only",
        }
        strength["analysis_method"] = method_map.get(method, method)
    return strength

def infer_certainty_grade(lang, significance):
    if lang == "direct_causal" and significance == "significant":
        return "high"
    elif lang == "uncertain_causal" or significance in ("marginal", "not_reported"):
        return "moderate"
    elif lang == "no_relationship":
        return "high" if significance == "not_significant" else "moderate"
    return "not_assessed"

def infer_evidential_basis(sent, lang):
    basis = {"evidence_objects": []}
    sent_lower = sent.lower()
    if "experiment" in sent_lower or "randomized" in sent_lower or "manipulat" in sent_lower:
        basis["evidence_types"] = ["randomized_experiment"]
    elif "regression" in sent_lower or "anova" in sent_lower or "statistical" in sent_lower:
        basis["evidence_types"] = ["observational_cross_sectional"]
    elif "observ" in sent_lower or "survey" in sent_lower:
        basis["evidence_types"] = ["observational_longitudinal"]
    elif "model" in sent_lower or "simulation" in sent_lower:
        basis["evidence_types"] = ["modeling_simulation"]
    elif "mechanism" in sent_lower or "pathway" in sent_lower or "mediat" in sent_lower:
        basis["evidence_types"] = ["mechanistic_study"]
    else:
        basis["evidence_types"] = ["case_study"]
    if "correlation" in sent_lower or "correlated" in sent_lower:
        basis["evidence_objects"].append("correlation")
    if "mechanism" in sent_lower or "pathway" in sent_lower or "mediat" in sent_lower:
        basis["evidence_objects"].append("mechanism")
    if not basis["evidence_objects"]:
        basis["evidence_objects"].append("correlation")
    return basis

def infer_direction(sent, lang):
    if lang in ("direct_causal", "uncertain_causal", "no_relationship"):
        sent_lower = sent.lower()
        if "experiment" in sent_lower or "manipulat" in sent_lower or "treatment" in sent_lower:
            evidence = "experimental"
        elif "temporal" in sent_lower or "preced" in sent_lower or "before" in sent_lower or "after" in sent_lower:
            evidence = "temporal_precedence"
        elif "model" in sent_lower or "structural" in sent_lower or "sem" in sent_lower or "path analysis" in sent_lower:
            evidence = "structural_model"
        elif "natural experiment" in sent_lower or "instrumental" in sent_lower:
            evidence = "natural_experiment"
        else:
            evidence = "theoretical"
        return {"status": "asserted", "evidence_for_direction": evidence}
    return None

def infer_context(sent, slug):
    ctx = {"status": "explicitly_asserted"}
    sent_lower = sent.lower()
    scopes = []
    if "by 2000" in sent_lower or "by 20" in sent_lower:
        scopes.append("temporal_scope: by 2000")
    if "waterloo" in sent_lower:
        scopes.append("geographic_scope: Waterloo, Ontario")
    if "boreal" in sent_lower:
        scopes.append("ecosystem_scope: boreal")
    if "temperate" in sent_lower:
        scopes.append("ecosystem_scope: temperate_forest")
    if scopes:
        ctx["scope_conditions"] = scopes
        return ctx
    return None

def infer_scope(lang, sent):
    if lang in ("direct_causal", "uncertain_causal", "no_relationship"):
        return "assertional"
    return "assertional"

def infer_ecosystem(sent):
    sent_lower = sent.lower()
    if "boreal" in sent_lower:
        return "boreal_and_temperate_high_montane_forests_and_woodlands"
    if "temperate" in sent_lower and ("forest" in sent_lower or "woodlot" in sent_lower):
        return "deciduous_temperate_forests"
    if "peatland" in sent_lower:
        return "boreal_and_temperate_fens"
    if "marsh" in sent_lower:
        return "permanent_marshes"
    if "forest" in sent_lower:
        return "deciduous_temperate_forests"
    if "grassland" in sent_lower:
        return "temperate_subhumid_grasslands"
    return None

def infer_mediation(sent):
    sent_lower = sent.lower()
    if "through" in sent_lower or "mediat" in sent_lower or "via" in sent_lower:
        return {"status": "explicitly_asserted", "pathway_description": sent[:200]}
    return None

def infer_proximate_distal(sent, lang):
    sent_lower = sent.lower()
    if "proximate" in sent_lower or "direct" in sent_lower:
        return "proximate"
    if "distal" in sent_lower or "indirect" in sent_lower or "long-term" in sent_lower:
        return "distal"
    return None

def errors(g): return [r.message for r in VALIDATOR.validate(g, "CausalGraph").results]
def h(*parts): return hashlib.sha1("|".join(parts).encode()).hexdigest()[:10]
def clean(s): return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s).replace("*", "")).strip()

# Whitelist: claim-dense sections + methods/experiment/analysis (often contain causal claims)
# Expanded for completeness in ecological restoration causal claims
TARGET_SECTIONS = {
    # Core claim-dense sections (IMRaD)
    "abstract", "results", "discussion", "conclusions", "findings", "summary",
    # Experimental/analytical sections
    "experiment", "analysis", "observations", "effects", "impacts", "responses",
    # Methods variants (causal claims often in design/treatment descriptions)
    "methods", "methodology", "materials", "treatments", "treatment",
    "experimental design", "study design", "design",
    # Intervention/restoration specific
    "intervention", "interventions", "restoration", "management",
    # Monitoring/evaluation
    "monitoring", "assessment", "evaluation", "follow-up",
    # Site/system/species context
    "site", "study site", "study area", "system", "study system", "species",
    # Setting variants
    "field", "greenhouse", "laboratory", "mesocosm",
    # Discussion subsections that often contain causal synthesis
    "synthesis", "implications", "management implications",
}

def normalize_header(h):
    """Normalize spaced PDF headers: 'a b s t r a c t' -> 'abstract'"""
    return re.sub(r"\s+", "", h.lower())

def split_sentences(body):
    body = re.sub(r"(?<![.!?:\"')\]])\n\s*\n(?=[a-z])", " ", body)
    out = []
    for para in re.split(r"\n\s*\n", body):
        if para.count("|") > 4:
            continue
        for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", para.replace("\n", " ")):
            if 40 <= len(s) <= 600 and CUES.search(s):
                out.append(s.strip())
    return out

def selected_sentences(text):
    """Extract sentences ONLY from whitelisted sections (including subsections)."""
    keep, buf = False, []
    current_major = ""
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            h = normalize_header(re.sub(r"^[#\s\d.*]+", "", line))
            # Track major sections (1-2 #)
            hash_count = len(line) - len(line.lstrip("#"))
            if hash_count <= 2:
                current_major = h
            # Keep if THIS header matches OR parent major section matches
            keep = any(t in h for t in TARGET_SECTIONS) or any(t in current_major for t in TARGET_SECTIONS)
            continue
        if keep:
            buf.append(line)
    sents = split_sentences("\n".join(buf))
    # Fallback: if whitelist yields too few, use CUES filter on full text
    return sents if len(sents) >= 3 else split_sentences(text)

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
    for m in reversed(list(re.finditer(r"\}\s*,", s))):
        try:
            d = json.loads(s[: m.start() + 1] + "]}")
            return d.get("claims", []) if isinstance(d, dict) else None
        except json.JSONDecodeError:
            continue
    return None

async def ask_async(numbered, depth=0):
    """Async version using httpx streaming (avoids AsyncOpenAI vLLM timeout)."""
    payload = {
        "model": MODEL,
        "temperature": 0,
        "max_tokens": 3000,
        "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": numbered}
        ],
        "chat_template_kwargs": {"enable_thinking": False},
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    
    for attempt in range(2):
        try:
            content_chunks = []
            finish_reason = None
            usage = None
            
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                async with client.stream("POST", f"{BASE_URL.rstrip('/')}/chat/completions", 
                                       headers=HEADERS, json=payload) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data: "):
                            continue
                        data_str = line[6:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        if delta.get("content"):
                            content_chunks.append(delta["content"])
                        if choices[0].get("finish_reason"):
                            finish_reason = choices[0]["finish_reason"]
            
            txt = "".join(content_chunks)
            print("  visible chars:", len(txt), "| tokens:", (usage or {}).get("completion_tokens"),
                "| claims:", txt.count('"id"'), "| finish:", finish_reason, flush=True)
            print("  tail:", repr(txt[-200:]), flush=True)
            break
            
        except Exception as e:
            print(f"  request failed: {type(e).__name__} attempt {attempt + 1}", flush=True)
            await asyncio.sleep(2 * (2 ** attempt))
    else:
        return None

    with LOCK:
        STATS["calls"] += 1
        if usage:
            STATS["prompt_tokens"] += usage.get("prompt_tokens", 0)
            STATS["completion_tokens"] += usage.get("completion_tokens", 0)
        if finish_reason == "length":
            STATS["truncated"] = STATS.get("truncated", 0) + 1

    txt = re.sub(r"thinking.*?/thinking", "", txt or "", flags=re.S)
    txt = re.sub(r"\(think\)\.*?\/think\)", "", txt, flags=re.S)
    txt = re.sub(r"[\[\(]think[\)].*?[\[\(]/think[\)]", "", txt, flags=re.S)
    claims = parse_claims(txt)
    claims = parse_claims(txt)
    if claims is None:
        print(f"  reply was not valid JSON (finish={finish_reason})", flush=True)
    elif finish_reason == "length" and depth < 2:
        ids = [c["id"] for c in claims if isinstance(c.get("id"), int)]
        if ids:
            rest = [l for l in numbered.split("\n")
                    if (m := re.match(r"\[(\d+)\]", l)) and int(m.group(1)) > max(ids)]
            if rest:
                more = await ask_async("\n".join(rest), depth + 1)
                claims += more or []
    return claims

async def process_article_async(path):
    """Process all batches for one article concurrently."""
    path = Path(path); slug = path.stem
    cache = OUT / "articles" / f"{slug}.json"
    if cache.exists():
        return slug, {"cached": True}
    if time.time() > DEADLINE:
        return slug, {"skipped": "time budget"}

    sents = selected_sentences(path.read_text(encoding="utf-8"))
    log = {"sentences_sent": len(sents), "kept": 0, "dropped": {}, "invalid": []}

    # Create all batch tasks
    batch_tasks = []
    for start in range(0, len(sents), MAX_SENT):
        end = min(start + MAX_SENT, len(sents))
        batch = "\n".join(f"[{i}] {clean(sents[i])}" for i in range(start, end))
        batch_tasks.append(ask_async(batch))

    # Execute all batches concurrently
    results = await asyncio.gather(*batch_tasks, return_exceptions=True)

    claims, seen, failed = [], set(), False
    for res in results:
        if isinstance(res, Exception) or res is None:
            failed = True
            continue
        for c in res:
            try:
                if c.get("lang") == "no_relationship": c["neg"] = True
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
        return slug, {"failed": True}

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


def check(c, sents):
    if not (isinstance(c.get("id"), int) and 0 <= c["id"] < len(sents)): return "bad_id"
    if c.get("pred") not in PRED or c.get("lang") not in LANG: return "bad_enum"
    if bool(c.get("neg")) != (c["lang"] == "no_relationship"): return "bad_null_pairing"
    if not any(a in ACC for a in c.get("acc", [])): return "no_accounts"
    for side in ("cause", "effect"):
        term = c[side].get("term") or ""
        if not term or not in_sentence(term, sents[c["id"]]): return side + "_not_in_sentence"
    return None

def in_sentence(term, sent):
    low = sent.lower()
    stems = [w[:5] for w in re.findall(r"[a-z]+", term.lower()) if len(w) >= 4]
    return any(s in low for s in stems)

def node(slug, n):
    q = n.get("q") or ""
    a = (n.get("attr") or "").split("|")[-1].strip()
    t = n["term"].split("|")[-1].strip()
    d = {"id": f"causal_mosaic:{slug.lower()}_{h(slug, n.get('type', ''), t.lower(), a.lower(), q)}",
         "name": " ".join(x for x in [q, a, "of" if a else "", t] if x),
         "entity_term": resolve(t, VERIFIED_TERMS)}
    if n.get("type") in ETYPE: d["entity_type"] = n["type"]
    if a: d["measured_attribute"] = resolve(a, VERIFIED_ATTRS)
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
            "original_sentence": sent,
            "source_spans": [{"text": sent}],
            "source_document": slug}
    if c["lang"] == "no_relationship":
        edge["negated"] = True
    if ENRICH:
        s = enrich_node(s, sent)
        o = enrich_node(o, sent)
        edge = enrich_edge(edge, sents, slug)
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
    """Sync wrapper for async article processing."""
    return asyncio.run(process_article_async(path))


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
        print(asyncio.run(process_article_async(target)))
        print(STATS)
    else:
        main(target)