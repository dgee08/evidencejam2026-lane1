import yaml
s = yaml.safe_load(open("schema/causalmosaic.yaml", encoding="utf-8"))
for name in ["PhilosophicalAccountEnum", "ClaimStrengthEnum", "CausalPredicateEnum", "DocumentSectionEnum"]:
    print("\n##", name)
    for k, v in (s["enums"][name].get("permissible_values") or {}).items():
        print("-", k, ":", (v or {}).get("description"))