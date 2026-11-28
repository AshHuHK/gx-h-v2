"""Read optional display names and search terms from verified plugin package members."""
import json


def _terms(record):
    names = record["names"]
    aliases = record.get("aliases", [])
    if (not isinstance(names, dict) or not names
            or not all(isinstance(k, str) and isinstance(v, str) and v.strip() for k, v in names.items())
            or not isinstance(aliases, list) or not all(isinstance(v, str) and v.strip() for v in aliases)):
        raise ValueError("Expected localized names and a list of search aliases")
    return {"names": names, "aliases": list(dict.fromkeys([*names.values(), *aliases]))}


def declarations(packages, unavailable):
    """Provider-declared text takes priority over translations for an exact method card."""
    local, translations = {}, {}
    for package in packages:
        provider = f"{package.pin.id}@{package.pin.version}"
        for label in ("gx_catalog_terms", "gx_catalog_translations"):
            member = package.manifest.labels.get(label)
            if not member:
                continue
            try:
                source = json.loads(package.read_file(member))
                if label == "gx_catalog_terms":
                    entries = {(provider, method): _terms(spec) for method, spec in source.items()}
                    local.update(entries)
                else:
                    entries = {}
                    for spec in source:
                        translation = _terms(spec)
                        for target in spec["sources"]:
                            key = (target["provider"], target["method"], target["portable_sha256"])
                            entries[key] = translation
                    translations.update(entries)
            except (AttributeError, KeyError, ValueError, TypeError):
                # A malformed optional translation does not remove the verified executable card.
                unavailable.append(f"{provider} / {member}")
    return local, translations
