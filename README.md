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

### Quick Start (One Command)

```bash
# 1. Install dependencies (one-time)
pip install -r requirements.txt

# 2. Set required environment variables
export BRINE_URL="https://your-endpoint/v1"
export BRINE_MODEL="Qwen3.8-27B"
export BRINE_API_KEY="sk-..."

# 3. Run the pipeline
./run.sh corpus/ output/
```

That's it. The script handles extraction, merging, and validation automatically.

---

### Detailed Instructions

#### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

**Key packages:** `openai`, `httpx`, `linkml`, `pyyaml`, `pandas`

#### 2. Configure LLM Endpoint (Required)

Set these **three** environment variables before running:

| Variable | Description | Example |
|----------|-------------|---------|
| `BRINE_URL` | LiteLLM/vLLM endpoint URL | `https://brine.sharcnet.ca/v1` |
| `BRINE_MODEL` | Model name on the endpoint | `Qwen3.8-27B` |
| `BRINE_API_KEY` | Your API key | `sk-abc123...` |

```bash
export BRINE_URL="https://brine.sharcnet.ca/v1"
export BRINE_MODEL="Qwen3.8-27B"
export BRINE_API_KEY="sk-..."
```

#### 3. Run the Pipeline

```bash
./run.sh [corpus_dir] [output_dir]
```

| Argument | Default | Description |
|----------|---------|-------------|
| `corpus_dir` | `corpus/` | Folder containing article `.md` files |
| `output_dir` | `output/` | Where to write results |

**Examples:**
```bash
./run.sh                    # Uses corpus/ and output/
./run.sh corpus/ output/    # Explicit
./run.sh /data/articles /results  # Custom paths
```

---

### What the Script Does

| Step | Script | Description |
|------|--------|-------------|
| 1 | `pipeline.py` | Processes each article → extracts causal claims → builds CAMO graph → saves `output/articles/[SLUG].json` |
| 2 | `merge_graphs.py` | Merges all article graphs → deduplicates nodes → adds DOIs from `corpus/index.csv` |
| 3 | Validation | Runs LinkML schema validation on merged graph |

The script stops on any failure (extraction error, merge error, validation error).

---

### Expected Outputs

```
output/
├── merged_causal_graph.yaml    # Final merged graph (YAML)
├── merged_causal_graph.json    # Final merged graph (JSON)
└── articles/
    ├── [SLUG].json             # Per-article graph
    └── [SLUG].log.json         # Extraction log (sentences, claims, drops)
```

---

### Configuration (Optional)

Tune behavior via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `CAMO_WORKERS` | `3` | Concurrent articles (thread pool) |
| `CAMO_MAX_SENT` | `100` | Max sentences per LLM batch |
| `CAMO_BUDGET_MIN` | `120` | Time budget in minutes (hard deadline) |
| `CAMO_LIMIT` | `0` | Process only first N articles (0 = all) |
| `CAMO_FORCE` | `0` | `1` = reprocess cached articles |

**Example:**
```bash
CAMO_WORKERS=5 CAMO_BUDGET_MIN=180 ./run.sh
```

---

### Troubleshooting

| Issue | Fix |
|-------|-----|
| `BRINE_URL not set` | Export all three required env vars |
| `ModuleNotFoundError: httpx` | Run `pip install -r requirements.txt` |
| Timeout / slow | Increase `CAMO_BUDGET_MIN` or decrease `CAMO_MAX_SENT` |
| Validation fails | Check `output/articles/[SLUG].log.json` for invalid claims |
| No articles processed | Verify `corpus/` has `.md` files and `corpus/index.csv` exists |

---

### Performance Notes

- **~308 articles** in corpus
- **~1–3 minutes per article** (depends on endpoint speed)
- **~2–4 hours** for full run on shared endpoint
- Token usage logged in `STATS` at end of run
