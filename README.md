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
 
1. **Model replies that get cut off.**
   - **What happens:** We limit how long the model's reply can be (3,000 tokens). When a batch of sentences contains many claims, the reply can stop in the middle of a claim. In one test, a batch of just 16 sentences from a results section hit the limit.
   - **Effect:** The pipeline keeps the complete claims from before the cut, so the claims at the end of a busy batch are lost without any warning.
   - **Workaround:** We ask the model to write its answer in a compact form, which roughly halved reply length in our test. If cutoffs still happen, lower `CAMO_MAX_SENT` to send fewer sentences per batch.
2. **Bigger batches save tokens but find fewer claims.**
   - **What happens:** Every call to the model repeats our instructions, so fewer, larger batches use fewer tokens overall. But the model finds fewer claims when it is given a very long list of sentences. On one article, a single call with 92 sentences kept 6 claims, while three calls of up to 40 sentences kept 9.
   - **Effect:** Saving tokens can cost us claims. We only tested this on one article, so the best batch size for the whole corpus is unknown.
   - **Workaround:** The default is 40 sentences per batch. We did not tune it further.
3. **Good claims thrown out by our wording check.**
   - **What happens:** To catch made-up claims, we check that the words the model names as cause and effect appear in the sentence. This check is strict, so it rejects claims where the model used a full name or a different form of a word (for example "carbon" where the paper says "C", or "copper" where it says "Cu").
   - **Effect:** On one article, between 4 and 7 of the model's roughly 15 claims were rejected this way. We did not read the rejected claims, so we do not know how many were actually good. We have since told the model to reuse the sentence's own words, but we have not tested whether that helps across the corpus.
4. **Causal wording that is too strong.**
   - **What happens:** The model is only allowed a few relationship types, and "is associated with" is not one of them. A plain comparison or a "contributes to" statement is often labelled as a direct cause.
   - **Effect:** In our test output, a comparison of tree density against control sites and a finding that metals built up in the soil were both labelled as direct causes. Some claims sound more certain than the authors' words support.
5. **Misread sentences and the wrong measurement.**
   - **What happens:** The model sometimes picks the wrong outcome for a sentence, and sometimes attaches a measurement that does not fit.
   - **Effect:** A sentence about how long it takes to pay back emissions was recorded as "decreased emissions" in one run and "increased biomass" in another. Rates, durations and amounts were often labelled as "abundance", giving nodes like "increased abundance of carbon sequestration". The output is accepted by the schema, but these entries are wrong.
6. **Few standard ontology terms on the main subjects.**
   - **What happens:** The model only gives a standard ontology code when its instructions push it to, and otherwise writes plain text. In our sample outputs, some measurements had standard codes, but none of the main subjects (the species, processes or variables) did. At one point, rewording the instructions removed the codes entirely.
   - **Effect:** Most nodes are labelled with plain text instead of standard terms. We do not check the model's codes against the real ontology, so a wrong code would pass unnoticed.
   - **Advice:** Count how many standard codes are in the output before relying on the ontology coverage.
7. **Results change between runs, and failures waste work.**
   - **What happens:** The same article produced 11, 10, 8 and 6 links across runs where we only made small changes to the instructions or batch size. Also, if one batch for an article fails, the pipeline discards the batches that worked and marks the whole article as failed.
   - **Effect:** Results are not repeatable, and a single failed batch means paying for the whole article again on retry.
8. **The extra detail fields are off by default and unreliable.** A separate step can add details such as dose, comparison group, strength and type of evidence. It only runs when `CAMO_ENRICH=1`. Our test outputs were made without it, and we did not test it on the full corpus. Problems we found or expect:
   - It picks a comparison label (such as "higher" or "lower") from the first matching word, and that word is not always one the schema accepts. One correct link was rejected because of this single optional field, and the pipeline then dropped the whole link.
   - When it cannot tell which species a dose refers to, it assumes *Sanguinaria canadensis*. That species comes from our worked example and is wrong for most other papers.
   - It assumes a planting year (1997 for one article, otherwise three years before publication) to work out time since the intervention.
   - It falls back to "case study" as the type of evidence, "theoretical" as the basis for direction, and "assertional" as the scope for every claim.
   - Its confidence grade comes from the authors' wording and whether the word "significant" appears, not from the quality of the evidence.
9. **One ontology code used for many different things.**
   - **What happens:** Our built-in lookup lists give the same code to many unrelated terms. Forest, lake, river, marsh and peatland all share one habitat code. Many different measurements share one or two measurement codes, and several chemicals and environmental factors share a single code.
   - **Effect:** The codes look valid, so the graph passes the schema check, but the meaning is wrong.
   - **Advice:** Plain-text labels are safer than these codes.
10. **Not tested on the whole corpus.**
    - **What happens:** We ran the pipeline on a few articles, not all 308.
    - **Effect:** We have not measured the total run time or the energy used per article. Also, the pipeline skips any article that already has a saved result in `output/articles/`, so old results made with earlier instructions can be reused unless `CAMO_FORCE=1` is set.
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
