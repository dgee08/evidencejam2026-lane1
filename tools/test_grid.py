import yaml
from linkml.validator import Validator
from linkml.validator.plugins import JsonschemaValidationPlugin

V = Validator("schema/causalmosaic.yaml", validation_plugins=[JsonschemaValidationPlugin(closed=True)])
for pred in ["causes", "contributes_to", "enables", "prevents", "disrupts", "regulates", "associated_with", "correlated_with"]:
    row = []
    for lang in ["no_relationship", "uncertain_causal", "direct_causal"]:
        h = yaml.safe_load(open("example/minimal.yaml", encoding="utf-8"))
        h["edges"][0]["predicate"] = pred
        h["edges"][0]["causal_language"] = lang
        if lang == "no_relationship":
            h["edges"][0]["negated"] = True
        row.append(f"{lang}={'OK' if not V.validate(h, 'CausalGraph').results else 'X'}")
    print(pred, row)