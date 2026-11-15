#!/bin/bash
# Evidence Jam 2026 Lane 1 - Pipeline runner
# Usage: ./run.sh corpus/ output/

set -euo pipefail

CORPUS_DIR="${1:-corpus}"
OUT_DIR="${2:-output}"

# Check required env vars
: "${BRINE_URL:?BRINE_URL not set}"
: "${BRINE_MODEL:?BRINE_MODEL not set}"
: "${BRINE_API_KEY:?BRINE_API_KEY not set}"

echo "=== Evidence Jam 2026 Lane 1 Pipeline ==="
echo "Corpus: $CORPUS_DIR"
echo "Output: $OUT_DIR"
echo "Model: $BRINE_MODEL"
echo "Endpoint: $BRINE_URL"
echo ""

# Run pipeline
echo "--- Running pipeline.py ---"
python src/pipeline.py "$CORPUS_DIR" "$OUT_DIR"

# Merge graphs
echo ""
echo "--- Merging graphs ---"
python src/merge_graphs.py

# Validate
echo ""
echo "--- Validating merged graph ---"
python -c "
import yaml
from linkml.validator import Validator
from linkml.validator.plugins import JsonschemaValidationPlugin

schema = yaml.safe_load(open('schema/causalmosaic.yaml', encoding='utf-8'))
validator = Validator('schema/causalmosaic.yaml', validation_plugins=[JsonschemaValidationPlugin(closed=True)])
result = validator.validate(yaml.safe_load(open('output/merged_causal_graph.yaml', encoding='utf-8')), 'CausalGraph')
errors = [r.message for r in result.results if r.severity == 'ERROR']
if errors:
    print('VALIDATION FAILED:')
    for e in errors:
        print(f'  {e}')
    exit(1)
else:
    print('VALIDATION PASSED')
"

echo ""
echo "=== Complete ==="
echo "Output files:"
echo "  - output/merged_causal_graph.yaml"
echo "  - output/merged_causal_graph.json"
echo "  - output/articles/*.json (per-article)"