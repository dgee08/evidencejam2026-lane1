#!/usr/bin/env python3
"""
Merge per-article CAMO graphs into a single validated CausalGraph.

Reads JSON files from output/articles/, merges nodes/edges, adds source_documents
with DOIs from corpus/index.csv, outputs merged YAML and JSON.
"""

import csv, hashlib, json, sys, yaml
from pathlib import Path
from datetime import datetime

ARTICLES_DIR = Path("output/articles")
INDEX_CSV = "corpus/index.csv"
OUT_DIR = Path("output")
MERGED_YAML = OUT_DIR / "merged_causal_graph.yaml"
MERGED_JSON = OUT_DIR / "merged_causal_graph.json"


def load_index():
    """Load DOI metadata from corpus/index.csv"""
    index = {}
    with open(INDEX_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            index[row["slug"]] = row
    return index


def load_articles(index):
    """Load all article JSONs from output/articles/"""
    articles = {}
    for path in sorted(ARTICLES_DIR.glob("*.json")):
        if path.name.endswith(".log.json"):
            continue
        slug = path.stem
        data = json.loads(path.read_text(encoding="utf-8"))
        # Skip failed/empty
        if data.get("failed") or data.get("cached") or data.get("skipped"):
            continue
        articles[slug] = data
    return articles


def merge_graphs(articles, index):
    """Merge multiple article graphs into one CausalGraph"""
    all_nodes = {}
    all_edges = []
    source_documents = []
    node_id_map = {}  # original_id -> merged_id

    for slug, graph in articles.items():
        # Build source_document entry
        meta = index.get(slug, {})
        authors_str = meta.get("authors", "")
        authors_list = [a.strip() for a in authors_str.split(";")] if authors_str else []
        source_documents.append({
            "document_id": slug,
            "doi": meta.get("doi", ""),
            "title": meta.get("title", ""),
            "year": int(meta.get("year", 0)) if meta.get("year") else None,
            "authors": authors_list,
            "journal": meta.get("journal", ""),
        })

        # Merge nodes with deduplication by content hash
        for node in graph.get("nodes", []):
            orig_id = node["id"]
            # Create content hash for deduplication
            content = f"{node.get('entity_type','')}|{node.get('entity_term','')}|{node.get('measured_attribute','')}|{node.get('state_or_change_qualifier','')}"
            content_hash = hashlib.sha1(content.encode()).hexdigest()[:12]
            merged_id = f"causal_mosaic:merged_{content_hash}"

            if merged_id not in all_nodes:
                # Create merged node (copy without original id, no source_documents field)
                merged_node = {k: v for k, v in node.items() if k != "id" and k != "source_documents"}
                merged_node["id"] = merged_id
                all_nodes[merged_id] = merged_node
            # Track which source docs this node appears in (for reference, not in output)

            node_id_map[f"{slug}:{orig_id}"] = merged_id

        # Merge edges with remapped node IDs
        for edge in graph.get("edges", []):
            subj_key = f"{slug}:{edge['subject']}"
            obj_key = f"{slug}:{edge['object']}"

            if subj_key not in node_id_map or obj_key not in node_id_map:
                continue  # skip if nodes weren't merged (shouldn't happen)

            merged_edge = {k: v for k, v in edge.items() if k != "id"}
            merged_edge["subject"] = node_id_map[subj_key]
            merged_edge["object"] = node_id_map[obj_key]
            merged_edge["source_document"] = slug
            # Generate new edge ID
            edge_content = f"{merged_edge['subject']}|{merged_edge['predicate']}|{merged_edge['object']}|{slug}"
            merged_edge["id"] = f"causal_mosaic:edge_{hashlib.sha1(edge_content.encode()).hexdigest()[:10]}"
            all_edges.append(merged_edge)

    # Build final CausalGraph - match schema exactly
    merged = {
        "graph_id": f"merged_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        "schema_version": "0.10.0",
        "provenance": {
            "ontology_framework": "ELMO + CAMO",
            "causal_mosaic_version": "0.10.0",
            "created": datetime.now().replace(microsecond=0).isoformat() + "Z",
            "source_corpus": "Evidence Jam 2026 Lane 1 - 308 ecological restoration articles",
            "annotation_protocol": "https://tim-a.ca/causalmosaic/camo_annotation_essentials.html",
        },
        "source_documents": [sd for sd in source_documents if sd["doi"]],
        "nodes": list(all_nodes.values()),
        "edges": all_edges,
    }
    return merged


def main():
    OUT_DIR.mkdir(exist_ok=True)

    print("Loading index...")
    index = load_index()

    print("Loading article graphs...")
    articles = load_articles(index)
    print(f"  Found {len(articles)} successful articles")

    if not articles:
        print("No successful articles to merge!")
        sys.exit(1)

    print("Merging graphs...")
    merged = merge_graphs(articles, index)
    print(f"  Merged nodes: {len(merged['nodes'])}")
    print(f"  Merged edges: {len(merged['edges'])}")
    print(f"  Source documents: {len(merged['source_documents'])}")

    # Write YAML
    print(f"Writing {MERGED_YAML}...")
    with open(MERGED_YAML, "w", encoding="utf-8") as f:
        yaml.dump(merged, f, sort_keys=False, allow_unicode=True)

    # Write JSON
    print(f"Writing {MERGED_JSON}...")
    with open(MERGED_JSON, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)

    print("Done!")


if __name__ == "__main__":
    main()