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
