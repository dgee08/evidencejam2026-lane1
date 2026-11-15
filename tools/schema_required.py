import yaml

s = yaml.safe_load(open("schema/causalmosaic.yaml", encoding="utf-8"))
slots = s.get("slots", {}) or {}

def show_class(name):
    c = s["classes"][name]
    print(f"\n=== {name} ===")
    for sname in c.get("slots", []) or []:
        spec = dict(slots.get(sname) or {})
        spec.update((c.get("slot_usage", {}) or {}).get(sname) or {})
        print(f"- {sname}: range={spec.get('range')} multivalued={spec.get('multivalued', False)} "
              f"required={spec.get('required', False)} pattern={spec.get('pattern')}")
    for sname, spec in (c.get("attributes", {}) or {}).items():
        spec = spec or {}
        print(f"- {sname}: range={spec.get('range')} multivalued={spec.get('multivalued', False)} "
              f"required={spec.get('required', False)} pattern={spec.get('pattern')}")

for cname in ["CausalGraph", "GraphProvenance", "CausalNode", "CausalEdge", "SourceDocument", "TextSpan"]:
    show_class(cname)

print("\n\n#### ENUM VALUES ####")
for ename in ["PhilosophicalAccountEnum", "CausalPredicateEnum", "ClaimStrengthEnum",
              "StateOrChangeQualifierEnum", "EntityTypeEnum"]:
    e = s["enums"].get(ename, {})
    print(f"\n{ename}: {list((e.get('permissible_values') or {}).keys())}")