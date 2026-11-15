import yaml
from linkml.validator import validate

for lang in ["no_relationship", "associational", "uncertain_causal", "direct_causal"]:
    for neg in [False, True]:
        h = yaml.safe_load(open("example/minimal.yaml", encoding="utf-8"))
        h["edges"][0]["causal_language"] = lang
        if neg:
            h["edges"][0]["negated"] = True
        errs = [r.message for r in validate(h, "schema/causalmosaic.yaml", "CausalGraph").results]
        print(lang, "negated" if neg else "", "->", "OK" if not errs else errs[0][:130])