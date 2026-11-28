"""Export installed public science card names, provider pins, I/O and declared limits for the selected release."""
import json
from gx_core.p0.catalog_index import installed_methods


def inventory():
    rows = []
    for card in installed_methods():
        method = card.get("method")
        purpose = method.get("objective") if isinstance(method, dict) else method
        rows.append({key:card.get(key) for key in (
            "provider", "provider_sha256", "capability_version", "portable_sha256", "names", "aliases",
            "inputs", "outputs", "limitations", "applicable_domain")} | {"purpose":purpose})
    return {"registered_cards":len(rows), "localized_cards":sum(bool(r["names"]) for r in rows),
            "nonlocalized": [r["capability_version"] for r in rows if not r["names"]], "cards":rows}


if __name__ == "__main__":
    print(json.dumps(inventory(), ensure_ascii=False, indent=2))
