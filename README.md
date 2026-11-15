## Our Solution

Our solution is a two-stage pipeline: extract causal claims from each article, then merge them into one graph.

![Extraction pipeline diagram](assets/extraction_pipeline.png)

1. **Extract:** For each article, `pipeline.py` pulls out the relevant sentences, sends them to the LLM with the prompt in `prompts/extract_system.txt`, maps the results into the CAMO schema, and writes `[ARTICLE CODE].json`.
2. **Merge:** Once every article has been processed, `merge_graphs.py` combines all the JSON files into a single causal graph in both JSON and YAML.

---

### How `pipeline.py` works

1. **Select sentences.** The pipeline keeps sentences that match the causal keyword filter (`CUES`) and sit under whitelisted headings. If fewer than 3 sentences are found, it drops the heading whitelist and searches the full text for `CUES` matches.
2. **Batch and send to the LLM.** Sentences are numbered and sent in batches of `CAMO_MAX_SENT`, following `prompts/extract_system.txt`. Batches from one article run concurrently, and articles run in a thread pool.
3. **Validate each claim.** A claim returned by the LLM is kept only if it has:
   - a valid sentence ID
   - a valid predicate
   - a valid causal language value
   - a valid philosophical account
   - a negation consistent with `no_relationship`
   - cause and effect terms that appear in the sentence

   Claims that fail any check are dropped and tallied in the article log.
4. **Build nodes and edges.** Each passing claim becomes two nodes (subject and object) and one edge, each with a deterministic hashed ID.
5. **Add rule-based fields.** Pattern-matching parsers on the claim's source sentence fill in extra fields:

   | Applied to | Fields |
   | ---------- | ------ |
   | **Edges** | `dose`, `comparator`, `temporal_extent`, `strength`, `certainty_grade`, `evidential_basis`, `direction`, `context_dependence`, `scope`, `ecosystem_context`, `mediation`, `proximate_distal` |
   | **Nodes** | `life_stage`, `life_stage_term`, `applied_to` |

6. **Parse ontology terms.** The LLM can return ontology terms as `CURIE | label`. The pipeline stores the CURIE in `entity_term` or `measured_attribute`. If a value has no valid CURIE, it is kept as free text.
7. **Validate each article graph.** Each article graph is checked against the LinkML schema. If validation fails, each claim is re-validated on its own, and any that fail are dropped and logged under `invalid`.
8. **Merge.** The article graphs are merged into one graph, which is validated again.

---

## How `merge_graphs.py` works

`merge_graphs.py` merges the per-article Causal Mosaic graphs into a single validated `CausalGraph`. Run it from the project root.

**Inputs**
- Article JSON files in `output/articles/`
- Bibliographic metadata from `corpus/index.csv`

**Outputs**
- `output/merged_causal_graph.yaml`
- `output/merged_causal_graph.json`

**Merge behavior**
- **Nodes** are deduplicated across articles; the first node seen wins.
- **Edges** are remapped to the merged node IDs, tagged with their source article slug, and given new hashed IDs. They are *not* deduplicated across articles, so every edge stays traceable to its source.
- **Provenance** metadata is included, and `source_documents` is limited to articles with a DOI.

**Notes**
- The schema version and corpus description are hard-coded.
- Because of this, merged nodes do not record which articles they came from.
- The script exits with an error if no articles load.

---

## Running the Program

`run.sh` is the single entry point for the Lane 1 pipeline.

```bash
./run.sh [corpus_dir] [output_dir]
```

Both arguments are optional and default to `corpus` and `output`.

### Prerequisites

Before doing anything, `run.sh` checks that the LLM endpoint, model name, and API key are set. It exits with an error naming any that are missing.

```bash
export BRINE_URL="..."       # LLM endpoint URL
export BRINE_MODEL="..."     # model name
export BRINE_API_KEY="..."   # your API key
```


### What it does

1. **Extract.** Reads each article in the corpus folder and builds a cause-and-effect graph for it.
2. **Merge.** Combines all the article graphs into one merged graph, removing duplicate nodes along the way.
3. **Validate.** Checks the merged graph against the Causal Mosaic schema and prints either `VALIDATION PASSED` or a list of what needs fixing.

If any step fails, the script stops so you don't end up with partial results.

### Output

| Path | Contents |
| ---- | -------- |
| `output/merged_causal_graph.yaml` | Final merged graph (YAML) |
| `output/merged_causal_graph.json` | Final merged graph (JSON) |
| `output/articles/` | Individual article graphs |

> **Note:** The merge and validation steps always read from `output/`. If you pass a different output directory, the later steps won't find the files, so keep the same folder throughout.

---

## Judging Criteria
 
The sections below cover faithfulness, value to a real user, honest limits, and how to install and extend the pipeline.
 
---
 
### Faithfulness
 
Faithfulness means the graph says only what the articles say. The pipeline treats the LLM as untrusted and checks everything it returns.
 
**Guaranteed by code**
- **Quotes are never generated.** `original_sentence` and `source_spans` are copied from the article's own sentence list using the sentence ID the model returned (`sents[c["id"]]`). The model never writes this text, so it cannot paraphrase or fabricate a quote.
- **Every edge points at a real sentence.** An ID outside the sentence range is dropped (`bad_id`).
- **Only allowed vocabulary is used.** Predicate, causal language, philosophical account and entity type must be values from the schema enums. Anything else is dropped (`bad_enum`, `no_accounts`) or left unset.
- **Null results are paired correctly.** `negated: true` must agree with `causal_language: no_relationship`, otherwise the claim is dropped (`bad_null_pairing`).
- **Terms must appear in the sentence.** The cause and effect terms must share a word stem with the sentence they cite. Otherwise the claim is dropped (`cause_not_in_sentence` / `effect_not_in_sentence`).
- **Schema validity is enforced.** Output that fails LinkML validation is removed claim by claim.
**What happens to a rejected claim**
- It is dropped and counted in the article's log (`output/articles/<slug>.log.json`).
- It does not reach the graph.
---
 
### Value to a real user
 
**What they can trust**
- **Quotes.** Every edge quotes the authors' own sentence, copied from the article rather than generated.
- **Authors' language.** `causal_language` reflects how the authors phrase the claim, not how good the evidence is.
- **Null results.** They are kept as `no_relationship` + `negated: true`, not silently dropped.
- **Validity.** The graph validates against the CAMO schema, and each edge links to a DOI.
**What they must check**
- **Labels.** Whether the cause and effect labels truly match the sentence. Open `original_sentence` for any edge they will rely on.
- **Ontology CURIEs.** Some terms are free text, and some mapping tables are coarse (see [Honest limits](#honest-limits)).
- **Stage-5 fields.** Dose, comparator, certainty grade and evidential basis are heuristic.
- **Recall.** Only sentences that pass the cue filter are read, and only `causes` / `contributes_to` / `enables` predicates are produced. The absence of an edge does not mean the paper makes no such claim.
---
 
### Honest limits
 
These are real failures we found in the pipeline.
 
1. **Truncated model replies.**
   - **What happens:** Replies are capped (`max_tokens=2048`, reasoning limited to 512). With a large batch (`CAMO_MAX_SENT`, default 300), the JSON can be cut off mid-claim (`finish_reason == "length"`, counted as `truncated` in `run_stats.json`).
   - **Effect:** `parse_claims` salvages the complete claims before the cut, so the tail of a long batch is lost silently.
   - **Workaround:** Lower `CAMO_MAX_SENT`.
2. **A "lower" comparator that invalidated a good edge.**
   - **What happens:** The comparator parser sets `comparator_dose_level` from the first word it finds (`higher`, `lower`, `intermediate`, `high`, `low`). That word is not always a value the schema allows.
   - **Effect:** A correct edge failed LinkML validation because of one bad optional field. Per-claim revalidation then dropped the whole edge.
3. **Invented defaults.** Several rule-based fields fall back to a default when the text gives no evidence:
   - `guess_counted_entity` defaults to *Sanguinaria canadensis* (`NCBITaxon:3472`) for any dose it cannot attribute. This comes from our worked example article and is wrong for other papers.
   - `parse_temporal` assumes a transplant year (1997 for one slug, otherwise `year - 3`) to compute months since intervention.
   - `infer_evidential_basis` defaults to `case_study`, then adds `correlation` as the evidence object.
   - `infer_direction` defaults to `evidence_for_direction: theoretical`.
   - `infer_scope` returns `assertional` for every claim.
   - `certainty_grade` is computed from language and a regex-detected significance word, not from the paper's evidence.
4. **Shared ontology codes.**
   - **What happens:** The lookup tables reuse one code for many unrelated terms. `ENV_CURIES` maps forest, lake, river, marsh and peatland all to `ENVO:01000253`. `ATTR_CURIES` maps many different attributes to `PATO:0001441` or `PATO:0000070`, and several chemicals and environmental factors to one code.
   - **Effect:** The CURIEs are syntactically valid, so the graph passes validation, but the terms are semantically wrong.
   - **Advice:** Free-text labels are safer than these codes.
---
 
### Craft and reusability
 
#### Install
 
```bash
git clone <repo-url> && cd <repo>
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # pinned versions
```
 
#### Configuration
 
**Required** (set these before running):
 
```bash
export BRINE_URL="..."       # LLM endpoint URL
export BRINE_MODEL="..."     # model name
export BRINE_API_KEY="..."   # your API key
```
 
**Optional** `CAMO_*` settings:
 
| Variable | Default | Meaning |
| -------- | ------- | ------- |
| `CAMO_WORKERS` | `3` | Articles processed in parallel |
| `CAMO_MAX_SENT` | `300` | Sentences per LLM request. Lower it if replies are being truncated. |
| `CAMO_BUDGET_MIN` | `180` | Time budget in minutes. After it, remaining articles are skipped. |
| `CAMO_LIMIT` | `0` (all) | Only process the first N articles, useful for a quick test |
| `CAMO_FORCE` | unset | Set to `1` to ignore cached per-article results in `output/articles/` |
 
---
