# Evidence Jam 2026: Lane 1 - Team d

## Our Solution

Our solution is a two-stage pipeline: extract causal claims from each article, then merge them into one graph.

![Extraction pipeline diagram](assets/extraction_pipeline.png)

1. **Extract:** For each article, `pipeline.py` pulls out the relevant sentences, sends them to the LLM with the prompt in `prompts/extract_system.txt`, maps the results into the CAMO schema, and writes `[ARTICLE CODE].json`.
2. **Merge:** Once every article has been processed, `merge_graphs.py` combines all the JSON files into a single causal graph in both JSON and YAML.

For background information on this project, see [BACKGROUND.md](BACKGROUND.md).

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
- **Recall.** Only sentences that pass the cue filter are read, and only `causes` / `contributes_to` / `enables` predicates are produced. The absence of an edge does not mean the paper makes no such claim.

---
 
### Honest limits

These are real problems we found. Where we measured something, we say what; everything was tested on a few articles, not all 308.

| # | Limit | What we saw | Mitigation |
| - | ----- | ----------- | ---------- |
| 1 | **Replies cut off** | A dense batch (16 results sentences) hit the 3,000-token reply cap, and the claims at the end were lost silently. | Compact JSON output halved reply length. Lower `CAMO_MAX_SENT` if it recurs. |
| 2 | **Batch size trades tokens for claims** | One 92-sentence call kept 6 claims. Three calls of up to 40 kept 9. | Default is 40. Tuned on one article only. |
| 3 | **Wording check rejects good claims** | On one article, 4 to 7 of about 15 model claims were dropped for not matching the sentence (e.g. "carbon" vs "C"). We never read the rejects. | Prompt now says to reuse the sentence's words. Untested at scale. |
| 4 | **Causal wording too strong** | "Associated with" isn't an allowed relationship, so comparisons and "contributes to" often come out as direct causes. | None yet. Allow weaker relationships. |
| 5 | **Misread sentences and wrong measurements** | A payback-time sentence became "decreased emissions" in one run and "increased biomass" in another. Rates and durations were labelled "abundance". | Prompt rules reduced this but did not remove it. |
| 6 | **Few ontology codes, unchecked** | Measurements often had PATO codes, but main subjects (species, processes, variables) usually did not. Codes are not verified against real ontology files. | Count codes in the output before relying on them. |
| 7 | **Results vary and failures waste work** | One article gave 11, 10, 8 and 6 edges across small changes. One failed batch discards the good batches too. | None yet. Retry only the failed batch. |
| 8 | **Not run on the full corpus** | Total run time and energy per article are unmeasured. Cached results in `output/articles/` are reused unless `CAMO_FORCE=1`. | Set `CAMO_FORCE=1` for a clean run. |

#### Optional enrichment step (off by default)

The rule-based step that adds dose, comparator, strength and evidence fields runs only with `CAMO_ENRICH=1`. Our outputs were made without it. We do not trust it yet:

- **Invented defaults:** it assumes *Sanguinaria canadensis* when a dose's species is unclear (from our worked example), assumes a planting year, and falls back to "case study", "theoretical" and "assertional".
- **Bad optional fields:** a comparison label the schema did not accept caused one correct edge to be dropped.
- **Coarse code tables:** its lookup lists give one code to many unrelated terms (forest, lake, river and marsh share a habitat code), so codes look valid but the meaning is wrong.
- **Weak confidence grade:** it comes from the authors' wording and the word "significant", not from the evidence.

Plain-text labels are safer than these codes.

---
 
## Building on this work (reusability)

If you pick this project up, these are the extensions we think would help most, roughly in order of value. Each one points at the place in the code to start.

| Idea | Where to start | Why it matters |
| ---- | -------------- | -------------- |
| **Check ontology codes against real ontology files.** Bundle small PATO, GO, ChEBI, ENVO and NCBITaxon term lists, and keep a model-supplied CURIE only if its code and label match. | `node()` in `pipeline.py` | Today we cannot tell a right code from a wrong one, and the lookup tables are coarse (see Honest limits 6 and 9). |
| **Build a small gold set and score against it.** Hand-annotate a few articles, starting from `example/Murphy2005.yaml`, and measure edge precision and recall. | new script in `src/` | Every prompt and batch-size change so far was judged by eye on one or two articles. |
| **Look at the rejected claims.** Log the claim and sentence for every drop, then loosen or replace the stem check. | `check()`, `in_sentence()` | Up to about half of the model's claims were dropped on one article, and we never read them. |
| **Allow weaker relationships.** Add `associated_with` and `associational` to the allowed values and tell the model when to use them. | `PRED`, `LANG`, `prompts/extract_system.txt` | Fixes overstated causal labels (Honest limit 4). |
| **Keep good batches when one fails.** Retry only the failed batch. | `process_article_async()` | Saves tokens and avoids losing whole articles. |
| **Merge nodes across articles.** Match nodes by CURIE or normalized label, and record which articles each node comes from. | `merge()` | Lets the graph show the same concept supported by several papers. |
| **Make the enrichment step trustworthy.** Replace invented defaults with "leave the field out", test each parser on a few articles, then enable it. | `enrich_node()`, `enrich_edge()`, the `parse_*` and `infer_*` helpers | These fields are the richest part of the schema, but they are currently off. |
| **Improve sentence selection.** Compare the whitelist version (`pipeline.py`) with the blacklist version (`pipeline_original.py`), and test the `CUES` list. | `selected_sentences()`, `CUES` | The pipeline only reads sentences that pass this filter, so it limits recall. |
| **Cut tokens further.** Two-pass design (a cheap filter, then extraction), or shorter prompts. | `prompts/extract_system.txt`, `ask_async()` | Prompt tokens were about 85% of our usage. |
| **Clean the source text.** Strip `<sup>` tags and link fragments from spans. | `clean()`, `build()` | Spans are currently noisy to read. |

Tips: set `CAMO_LIMIT` and `CAMO_FORCE=1` while experimenting, and read `output/articles/<slug>.log.json` after each run.
