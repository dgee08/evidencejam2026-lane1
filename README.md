# Evidence Jam 2026 — Team d (Lane 1: Causal Annotation) 

## Our challenge

> **Create a script that annotates the scientific journal articles, contained in**
> **`corpus/` in markdown format, using the Causal Mosaic Schema (CAMO). The annotations**
> **should be in YAML and JSON. Use ontologies where-ever possible.**

We were tasked with creating a script that reads the articles in `corpus/` and produces one merged CAMO graph, in both YAML and JSON. The graph should be valid under `schema/causalmosaic.yaml`, displaying causal claims, their topics, strengths, and locations in the original text.

## What's in this repo

```
corpus/              308 ecological restoration articles, one markdown file each
corpus/index.csv     Data on slug, DOI, title, authors, year, journal, word count
example/             Hand-checked CAMO annotation of one article
out/                 WHAT IS THIS FOLDER FOR?
prompts/             Written prompt for Qwen to understand the project
schema/              The Causal Mosaic Schema (CAMO) v0.10.0, as LinkML
src/                 Source code for extraction pipeline and creation of merged graph
BACKGROUND.md        Original project README
run.sh               File to organize entire process into one command
```

### `corpus/`

Each article is named by a short slug (e.g. `YKQQZV93.md`) and was converted
from PDF, so conversion artefacts were expected: image placeholders, broken tables,
headers and footers mid-paragraph, and inline tags. Use
`corpus/index.csv` to look up an article's DOI and bibliographic details.

### `example/`

Worked annotation showing what good output looks like: `Murphy2005.yaml`
and its source: `Murphy2005.pdf`

### `schema/causalmosaic.yaml`

The Causal Mosaic Schema is a way of describing causal relationships between 
populations of taxa, environmental variables, composite variables, management
interventions and environmental processes. The header comments and
the `description` on every class, slot, and enum are the annotation guide.

### `src/`

The source code in this repo includes: 
- `pipeline.py`: Our extraction pipeline, which builds Causal Mosaic Schema files for each article. Our final version uses a whitelist to select important parts of articles and runs workers asynchronously to decrease endpoint timeouts.
- `pipeline_original.py`: The original pipeline iteration. This version uses a blacklist to skip less useful parts of articles and runs workers in sync.
- `merge_graphs.py`: Program to merge single article outputs into final YAML/JSON schema.

An annotation essentials guide can be found at [this link](https://tim-a.ca/causalmosaic/camo_annotation_essentials.html). 

## Our Solution

Our solution to Lane 1 involves a simple data extraction pipeline, shown below.

![Extraction pipeline diagram](assets/extraction_pipeline.png)

Using the prompt from `extract_system.txt`, `pipeline.py` runs for each article: pulling relevant sentences from the text, drawing insights into the CAMO schema, and creating [ARTICLE CODE].json. After creating all the single article outputs, merge_graphs.py runs, merging all the JSON files into a completed causal graph in both JSON and YAML formats.

### How pipeline.py works

Description of how pipeline.py works

## How merge_graphs.py works

This script merges per-article Casual Mosaic graphs into a single validated CasualGraph. It is run from the project root and it reads each article JSON in output/articles/ with bibliographic metadata from corpus/index.csv. Nodes are deduplicated across articles with the first node seen winning. Edges are remapped to the merged node IDs, tagged with their source article slug, and given new hashed IDs. Each edge is traceable to its source as they are not deduplicated across articles. As a result, with the provenance metadata and a source_documents list limited to articles with a DOI, is written to output/merged_causal_graph.yaml and .json. The schema version and corpus description are hard-coded so merged nodes do not record which articles they came from, and the script exits with an error if no articles load. 

## Running the Program

The shell script run.sh file acts as the entry point for the Lane 1 pipeline. Run it as ./run.sh [corpus_dir] [output_dir], which defaults to corpus and output. Prior to doing anything, the program first checks that the endpoint, model name, and key is set and it exits with and error naming any that are missing. Once it starts, it works in three steps. Firstly, it read each article in your corpus folder and builds a cause-and-effect graph for it. Secondly, it combines all of those graphs into one merged graph, removing duplicate nodes along the way. Thirdly, it checks the merged graph against the CausalMosaic schema and prints either VALIDATION PASSED or a list of what needs fixing. If any steps fail, the script stops so you do not end up with partial results. When it finishes, there will be a merged graph in output/merged_causal_graph.yaml and .json, with individual article graphs in output/articles/. As the merge and validation steps always use the output/ folder, make sure to choose the same one or else the following steps will not find the files. 
