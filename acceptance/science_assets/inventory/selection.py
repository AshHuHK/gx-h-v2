"""Scientifically reviewed historical catalogue selections, linked by source identities."""

# The selector is checked against original table columns. A changed or ambiguous record
# is reported by the inventory builder before it can acquire a published reference.
def card(table, match, reference, canonical, binding, kind, domains, title, purpose,
         aliases, inputs, outputs, limits, *, version=None, license_basis=None):
    return {
        "table": table, "match": match, "reference": reference + "@legacy",
        "canonical_method_id": canonical, "binding": binding, "kind": kind,
        "domains": domains, "title": title, "purpose": purpose,
        "aliases": aliases, "inputs": inputs, "outputs": outputs, "limitations": limits,
        "version": version, "license_basis": license_basis,
    }


CARDS = [
    card("methods", {"id":"protenix"}, "protenix-model", "protein-structure-prediction",
         "historical-gx-model-register", "method", ["protein"],
         "Protenix v2 model registration", "Inspect the archived identity of a structure prediction model.",
         ["結構模型", "结构模型", "蛋白模型", "Protenix"], ["model identity"],
         ["historical model description"], "The historical model declaration has no current Job binding.",
         version="85767b811c40ed46e73a9b39519cf6bfca8701ba"),
    card("methods", {"id":"structure-geometry","version":"1.0.0"}, "native-coordinate-comparison",
         "deposited-coordinate-geometry", "science-native-v2.structure-geometry", "method", ["protein"],
         "Deposited three-chain geometry", "Compare observed backbone positions across three chains of one entity.",
         ["骨架讀數", "骨架读数", "鏈幾何", "链几何", "structure geometry"],
         ["deposited mmCIF", "three explicit chains"], ["chain comparison report"],
         "Single coordinate state. Coverage describes observed coordinates only.", version="1.0.0"),
    card("mcp-tools", {"server_id":"PDB-MCP-Server","tool_name":"get_structure_quality"},
         "pdb-quality-operation", "pdb-entry-quality", "legacy-pdb-mcp.get_structure_quality", "method",
         ["protein","peptide","antibody"], "PDB deposited structure quality",
         "Look up publicly deposited entry quality metadata.", ["坐標質控", "结构质量", "PDB quality"],
         ["PDB accession"], ["entry quality metadata"], "Schema checked. Historical service status is not operation-level execution evidence."),
    card("data", {"id":"PDB-MCP-Server"}, "pdb-entry-mcp", "pdb-archive",
         "legacy-pdb-mcp", "database", ["protein","peptide","antibody"],
         "Protein Data Bank via MCP", "Locate archived macromolecular structures by accession.",
         ["PDB", "公開結構", "公共结构"], ["PDB accession"], ["deposition metadata"],
         "PDB is also exposed through a historical web route. Two representations of the same entry share one scientific source."),
    card("data", {"id":"db-rcsb-pdb"}, "pdb-entry-web-route", "pdb-archive", "legacy-rcsb-web",
         "database", ["protein","peptide","antibody"], "Protein Data Bank via RCSB",
         "Find structural records on the same archive through the RCSB route.", ["PDB", "結構資料庫", "结构数据库"],
         ["PDB accession"], ["deposition metadata"], "Known archive route. Deployment and per-operation evidence are independent of the MCP provider."),
    card("data", {"id":"PDB_CCD"}, "pdb-chemical-dictionary", "pdb-chemical-components",
         "historical-ccd-directory", "database", ["peptide","cyclic-peptide","protein"],
         "PDB Chemical Component Dictionary", "Resolve component identifiers when checking deposited peptide records.",
         ["CCD", "線性多肽", "线性多肽", "殘基註釋"], ["component ID"], ["component definition"],
         "The source row identifies a directory; content release and modification coverage remain unverified."),
    card("workflows", {"id":"cyclic_head_tail_topology"}, "cyclic-closure-qc-control",
         "archived-peptide-topology-qc", "legacy-topology-scenario", "recipe", ["cyclic-peptide"],
         "Cyclic peptide archived closure control", "Inspect the first documented failure of the deposited connectivity control.",
         ["环肽质控", "環肽質控", "環化連接", "topology QC"], ["deposited connectivity record"],
         ["historical control verdict"], "The historical control failed its closure-distance criterion. It does not establish prediction or experimental validity."),
    card("workflows", {"id":"fab"}, "fab-coverage-history", "fab-deposit-coverage", "legacy-fab-case",
         "recipe", ["antibody"], "Fab historical coverage gap", "Find the recorded gap in paired heavy/light chain validation.",
         ["Fab", "抗體", "抗体", "VH/VL", "重輕鏈配對"], ["paired Fab deposit"],
         ["prior coverage assessment"], "Recorded as deferred. Evidence from VHH does not establish a paired Fab result."),
    card("data", {"id":"indexes/rhea142-sprot2026_03/lookup.sqlite"}, "rhea-sprot-index",
         "reaction-protein-annotation", "native-rhea-swissprot-snapshot", "database", ["enzyme"],
         "Rhea / reviewed Swiss-Prot annotation snapshot", "Read recorded links between a reaction, taxon and curated natural protein.",
         ["Rhea", "Swiss-Prot", "酿酒酵母", "酵母", "天然酶注釋"], ["reaction accession", "taxon"],
         ["reaction/protein annotation"], "A curated association with unspecified direction or no direct measurement remains qualified.",
         version="Rhea 142 / Swiss-Prot 2026_03"),
    card("data", {"id":"indexes/reaction-names/rhea139-142.sqlite"}, "rhea-reaction-names",
         "reaction-name-history", "native-rhea-name-snapshot", "database", ["enzyme"],
         "Rhea reaction identifiers and names", "Resolve a public reaction identifier across the indexed releases.",
         ["Rhea", "反應別名", "反应名称"], ["reaction ID or alias"], ["release and direction record"],
         "The snapshot covers releases 139–142.", version="139–142"),
    card("data", {"id":"kinetics/brenda2026.1-sabio20260920-v1.sqlite"}, "kinetics-assay-snapshot",
         "archived-enzyme-assays", "native-brenda-sabio-snapshot", "database", ["enzyme"],
         "BRENDA and SABIO-RK assay records", "Find archival measurements with experiment, unit and substrate context.",
         ["酶动学", "酶動力學", "BRENDA", "SABIO-RK"], ["enzyme annotation", "substrate"], ["assay records"],
         "Evidence and conditions belong to individual assays. Filter missing identifiers and units explicitly.",
         version="BRENDA 2026.1 / SABIO-RK 2026-09-20 v1",
         license_basis="The native assay manifest records BRENDA CC BY 4.0 and academic use for this SABIO-RK export."),
    card("methods", {"id":"enzyme-kinetics","version":"1"}, "native-assay-reading",
         "archived-enzyme-assays", "science-native-v2.enzyme-kinetics", "method", ["enzyme"],
         "Historical carbohydrate hydrolase assay lookup", "Read previously measured assay records with the reported material and conditions.",
         ["糖水解酶", "既有酶實驗", "反应条件", "archived assay"], ["enzyme class", "substrate"],
         ["assays.jsonl", "report"], "Limited to fourteen carbohydrate hydrolase EC classes. No transfer of activity across proteins.", version="1"),
    card("skills", {"collection":"ash_proj 共享知识库","skill_id":"pdb-database"}, "pdb-source-reading",
         "structure-record-reading", "shared-pdb-reading-guide", "skill", ["protein","antibody","peptide"],
         "Public structural record reading guide", "Find the original archival record and qualify deposition-level evidence.",
         ["PDB 精讀", "PDB 原件", "結構閱讀", "structure reading"], ["structure accession"],
         ["reading guidance"], "Guide only. Record-specific evidence needs the original deposit."),
    card("research", {"resource":"Europe PMC / PMC"}, "europe-pmc-research-route",
         "publication-retrieval", "legacy-europe-pmc-route", "research", ["research"],
         "Europe PMC / PMC literature records", "Find a publication and inspect available full text.",
         ["文献", "文獻", "PMC", "JATS"], ["query or PMCID"], ["bibliographic record", "available article text"],
         "The historical route includes metadata-only hits. Full-text coverage is article-specific."),
    card("methods", {"id":"eternafold","version":"13d2487-gx1"}, "rna-structure-archive",
         "rna-secondary-structure", "native-eternafold-manifest", "method", ["rna"],
         "EternaFold historical RNA model", "Locate the identity of an archived RNA secondary-structure model.",
         ["RNA 模型", "RNA secondary structure"], ["RNA record"], ["historical model record"],
         "Indexed for the deferred RNA workstream.", version="13d2487-gx1"),
]


def archive_selections():
    try:
        from .read_operations import DATASETS, GROUPS, slug
    except ImportError:
        from read_operations import DATASETS, GROUPS, slug
    import json
    from pathlib import Path
    pins = json.loads(Path(__file__).with_name("approved-operation-digests.json").read_bytes())
    result = []
    for original in CARDS:
        spec = original.copy()
        if spec["table"] == "mcp-tools":
            key = spec["match"]["server_id"] + "/" + spec["match"]["tool_name"]
            spec["operation_sha256"] = pins[key]
        result.append(spec)
    for canonical, (purpose,kind,domains,alias_text,providers) in GROUPS.items():
        for server, ops in providers.items():
            for name in ops.split():
                key = server + "/" + name
                spec = card("mcp-tools",{"server_id":server,"tool_name":name}, slug(server+"-"+name),
                            canonical, key, kind, domains, server+" • "+name, purpose, alias_text.split(),
                            [], [], "A returned record is subject to the archive's own release and coverage.")
                spec["operation_sha256"] = pins[key]
                result.append(spec)
    for canonical,(purpose,domains,aliases,records) in DATASETS.items():
        for lookup,ident in records:
            result.append(card("data",{"id":lookup},ident,canonical,ident,"database",domains,purpose,
                               "Locate the "+lookup+" source listed in the historical asset index.", aliases,
                               ["source identifier"],["archive reference"],
                               "Current content release, licence and sample coverage require the underlying data record."))
    return result
