# Evidence Jam 2026 Lane 1 — Handoff Summary

## Project Status: Submission-Ready

**Repo:** `evidencejam2026-lane1` | **Branch:** `main` | **Last commit:** Pipeline + merge + runner complete

---

## Key Files

| File | Purpose |
|------|---------|
| `src/pipeline.py` | **Main pipeline** (was `pipeline_fast.py`) — async, section whitelist, MAX_SENT=300 |
| `src/pipeline_original.py` | Original sync pipeline (reference) |
| `src/merge_graphs.py` | Merges per-article JSONs → single validated CAMO graph |
| `run.sh` | **One-command runner** (judges execute this) |
| `schema/causalmosaic.yaml` | CAMO v0.10.0 LinkML schema |
| `corpus/` | 308 articles (markdown) + `index.csv` (DOI metadata) |
| `example/Murphy2005.yaml` | Gold standard (25 findings, ~47 edges) |

---

## Pipeline Architecture

```
corpus/*.md → selected_sentences() [whitelist + CUES regex]
    → async batches (MAX_SENT=300) → Qwen3-27B via BRINE_URL
    → parse_claims() → enrich_node/enrich_edge (CURIE mapping)
    → LinkML Validator (closed=True) → output/articles/{slug}.json
    → merge_graphs.py → output/merged_causal_graph.yaml + .json
```

**Config (env vars):**
- `BRINE_URL`, `BRINE_MODEL`, `BRINE_API_KEY` — **required** (judges provide)
- `CAMO_WORKERS=3` — async concurrency
- `CAMO_MAX_SENT=300` — sentences per LLM batch
- `CAMO_BUDGET_MIN=180` — 3hr time budget
- `CAMO_LIMIT=0` — article cap (0 = all)

---

## Critical Decisions & Rationale

1. **Whitelist over blacklist** — 27 target sections (IMRaD + methods/treatments/interventions/restoration/monitoring/site/system). Fallback to full-text CUES if <3 sentences.

2. **MAX_SENT=300** — Single batch for most articles. Larger = fewer API calls = better endpoint resilience. Smaller (60) caused more timeouts.

3. **Async 3 workers** — Original used 10 sync threads; 3 async handles flaky BRINE endpoint better.

4. **max_tokens=8192** — Matches original pipeline. Reduced 4096 + disable_thinking caused more failures.

5. **Node deduplication in merge** — Content-hash based (entity_type|entity_term|measured_attribute|qualifier).

---

## Known Constraints

| Issue | Impact | Mitigation |
|-------|--------|------------|
| **BRINE endpoint flaky** | Timeouts on ~10% of calls | Async retries (2,4,8,16s backoff), 3 workers |
| **30% recall on Murphy gold** | 15-16 claims vs 25 findings | CUES regex + Qwen3-27B ceiling; all teams share this |
| **Validation drops** | ~5-10% claims fail CAMO schema | LinkML Validator enforced; enrich_node fixes common CURIE issues |
| **CUES regex recall gap** | Misses "associated with", "linked to", subtle causal language | Biggest improvement opportunity if time permits |

---

## Run Commands

```bash
# Required env vars (judges set these)
export BRINE_URL="https://..."
export BRINE_MODEL="qwen3-27b"
export BRINE_API_KEY="..."

# Full run (all 308 articles, ~3 hours)
./run.sh corpus/ output/

# Quick test (limit articles)
CAMO_LIMIT=5 ./run.sh corpus/ output/

# Single article
python src/pipeline.py corpus/YKQQZV93.md output/
```

---

## Output Structure

```
output/
├── articles/
│   ├── YKQQZV93.json      # Per-article CausalGraph
│   ├── 2F76GVEE.json
│   └── ...
├── merged_causal_graph.yaml   # Single validated graph (judges check this)
├── merged_causal_graph.json
└── token_usage.json           # If implemented
```

**Merged graph includes:**
- `source_documents[]` with DOIs from `corpus/index.csv`
- Deduplicated `nodes[]` + `edges[]` with `source_document` refs
- Validated against `schema/causalmosaic.yaml` (0 errors)

---

## Next Improvements (Priority Order)

1. **Token usage logging** → `output/token_usage.json` (for footprint score)
2. **CUES regex expansion** — add "associated with", "correlated with", "predicted"
3. **Cross-article parallelism** — process multiple articles concurrently
4. **Resume flag** — explicit `--resume` in run.sh
5. **Validation report** — write errors to file for judges

---

## Session Context (for continuity)

- All pipeline changes committed to `main`
- 84/308 articles successfully processed in test runs
- Validation passes (0 errors, 0 warnings)
- `run.sh` calls `src/pipeline.py` (renamed from `pipeline_fast.py`)
- Original preserved as `src/pipeline_original.py`