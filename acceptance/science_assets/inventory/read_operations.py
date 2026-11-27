"""Approved historical read operations, grouped by question and evidence type."""
import re

# Every pair is an individual operation, even when two providers expose a shared concept.
# No pattern matching or entire-server import admits other operations.
GROUPS = {
    "structure-entry-search": ("Search deposited structures", "database", ["protein","peptide","antibody"],
                               "公開結構 搜索 沉積 PDB", {"PDB-MCP-Server":"search_structures search_by_uniprot"}),
    "structure-entry-description": ("Inspect public deposition metadata", "database", ["protein","peptide","antibody"],
                                    "存檔蛋白 資料 结构记录 PDB", {"PDB-MCP-Server":"get_structure_info"}),
    "model-record-coverage": ("Inspect archived prediction coverage", "database", ["protein"], "模型檔案 缺失 覆蓋",
                              {"AlphaFold-MCP-Server":"check_availability get_coverage_info"}),
    "model-record-metadata": ("Inspect an existing model record", "database", ["protein"], "已發表模型 版本 日期",
                              {"AlphaFold-MCP-Server":"get_prediction_metadata batch_structure_info"}),
    "model-record-confidence": ("Inspect recorded prediction confidence", "database", ["protein"], "模型置信度 原始檔案",
                                {"AlphaFold-MCP-Server":"get_confidence_scores"}),
    "curated-protein-description": ("Inspect a public protein annotation", "database", ["protein","enzyme"],
                                    "天然蛋白 酵母 注释 註釋", {
                                        "Augmented-Nature-UniProt-MCP-Server":"get_protein_info",
                                        "STRING-db-MCP-Server":"get_protein_annotations",
                                        "ProteinAtlas-MCP-Server":"get_protein_info get_protein_by_ensembl"}),
    "protein-archive-xref": ("Find an external archive or publication for an entry", "database", ["protein","enzyme"],
                             "來源 溯源 文獻 蛋白", {"Augmented-Nature-UniProt-MCP-Server":"get_external_references get_literature_references"}),
    "protein-annotation-confidence": ("Inspect annotation evidence", "database", ["protein","enzyme"],
                                      "酶证据 蛋白註釋 證據等級", {"Augmented-Nature-UniProt-MCP-Server":"get_annotation_confidence"}),
    "protein-archive-accession": ("Check an archive accession or taxon identity", "database", ["protein","enzyme"],
                                  "識別碼 分類 登錄號 物種", {"Augmented-Nature-UniProt-MCP-Server":"validate_accession get_taxonomy_info",
                                                           "NCBI-Datasets-MCP-Server":"get_taxonomy_info get_taxonomic_lineage"}),
    "antibody-annotation": ("Read a published antibody reagent annotation", "database", ["antibody"],
                            "抗體資料 抗体注释 抗體原件", {"ProteinAtlas-MCP-Server":"get_antibody_info"}),
    "pathway-archive-record": ("Read published pathway descriptions", "database", ["enzyme"],
                               "通路 徑路 歸檔", {"KEGG-MCP-Server":"get_pathway_info get_pathway_reactions"}),
    "reaction-archive-record": ("Read a recorded reaction", "database", ["enzyme"], "天然酶 反應資料库 数据库",
                               {"KEGG-MCP-Server":"get_reaction_info search_reactions"}),
    "enzyme-archive-record": ("Read published enzyme classifications", "database", ["enzyme"], "酶分類 EC 注释 注釋",
                             {"KEGG-MCP-Server":"get_enzyme_info search_enzymes"}),
    "compound-archive-record": ("Read a recorded compound identifier or description", "database", ["enzyme"],
                                "底物 化合物 編號", {"KEGG-MCP-Server":"get_compound_info search_compounds"}),
    "organism-archive-list": ("Read the archive organism list", "database", ["enzyme","protein"],
                              "物种 物種 索引", {"KEGG-MCP-Server":"list_organisms"}),
    "literature-record-search": ("Search publication metadata", "research", ["research"], "文献 文獻 搜尋 检索",
                                {"PubMed-MCP-Server":"search_pubmed_key_words search_pubmed_advanced",
                                 "gx_academic_nature_v1":"search_papers", "gx_academic_openalex_v1":
                                 "search_works search_by_topic search_in_journal_list search_works_in_venue find_review_articles",
                                 "gx_academic_pubmed_v1":"pubmed_search_articles pubmed_europepmc_search",
                                 "arxiv-mcp-server":"search_papers", "gx_academic_search_v1":"search_literature"}),
    "literature-record": ("Read publication metadata for a record", "research", ["research"], "書目 原件 摘要",
                          {"PubMed-MCP-Server":"get_pubmed_article_metadata", "gx_academic_nature_v1":"get_paper_by_id",
                           "gx_academic_openalex_v1":"get_work", "gx_academic_pubmed_v1":"pubmed_fetch_articles pubmed_europepmc_fetch"}),
    "literature-fulltext": ("Read available publication text", "research", ["research"], "學術全文 文献原件 论文全文",
                            {"gx_academic_pubmed_v1":"pubmed_fetch_fulltext","arxiv-mcp-server":"read_paper"}),
    "literature-access-status": ("Locate available public copies", "research", ["research"], "公開原件 開放取用",
                                 {"gx_academic_openalex_v1":"find_open_access_version"}),
    "literature-related": ("Read linked publication records", "research", ["research"], "文獻引用 溯源 關聯研究",
                           {"gx_academic_openalex_v1":"get_related_works get_work_references get_work_citations",
                            "gx_academic_pubmed_v1":"pubmed_find_related"}),
    "publication-identifiers": ("Look up indexed publication references", "research", ["research"], "書目識別碼 文献索引",
                                {"gx_academic_openalex_v1":"batch_resolve_references",
                                 "gx_academic_pubmed_v1":"pubmed_convert_ids pubmed_lookup_citation"}),
    "literature-citation": ("Read citation metadata", "research", ["research"], "引用 引用資料",
                            {"gx_academic_nature_v1":"get_citation", "gx_academic_pubmed_v1":"pubmed_format_citations pubmed_fetch_citation_metrics"}),
    "literature-controlled-vocabulary": ("Look up controlled subject headings", "research", ["research"],
                                         "主題詞 文献主题", {"gx_academic_nature_v1":"lookup_mesh","gx_academic_pubmed_v1":"pubmed_lookup_mesh"}),
}
# Portable provenance links; each scope is deliberately narrower than the archived public API.
NATIVE_RELATIONS = {
    "structure-entry-search": ("org.gx.pcna-archive/1.0.0","pcna-archive-search@1","PCNA archive search limited to P12004."),
    "structure-entry-description": ("org.gx.pcna-archive/1.0.0","pcna-archive-fetch@1","Fetches a coordinate record in the P12004 study scope."),
    "literature-record-search": ("org.gx.publication-discovery/1.0.0","literature-query@1","Bibliographic search through Europe PMC and Crossref."),
    "literature-fulltext": ("org.gx.publication-discovery/1.0.0","literature-original@1","Full text when the indexed public source is supported."),
    "reaction-protein-annotation": ("org.gx.yeast-enzyme-evidence/1.0.1","yeast-reaction-index@2","S288C annotation for the plugin's supported substrates."),
    "curated-protein-description": ("org.gx.yeast-enzyme-evidence/1.0.1","yeast-uniprot-record@2","Assigned S288C protein records only."),
    "archived-enzyme-assays": ("org.gx.yeast-enzyme-archive/1.0.0","yeast-archive-lookup@1","Curated yeast archive lookup in the declared reaction/taxon panel."),
}
ARXIV_RELATIONS = {
    "literature-record-search":("org.gx.arxiv-sources/1.0.0","arxiv-search@1","Search published arXiv records."),
    "literature-fulltext":("org.gx.arxiv-sources/1.0.0","arxiv-fetch@1","Fetches the versioned arXiv article where available."),
}


def slug(value):
    return re.sub(r"[^a-z0-9]+","-",value.lower()).strip("-")

# Public annotations and archives. A directory row attests only its location.
DATASETS = {
    "reaction-names": ("Rhea reaction identifiers", ["enzyme"], ["反應命名", "反应编号"], [
        ("Rhea","local-rhea"),("Systems_Biology/enzymes/rhea","systems-biology-rhea")]),
    "archived-enzyme-assays": ("Archival enzyme measurements", ["enzyme"], ["原始酶实验", "酶實驗條件"], [
        ("BRENDA","local-brenda"),("db-brenda","brenda-route"),("Systems_Biology/enzymes/BRENDA","systems-biology-brenda"),
        ("Systems_Biology/enzymes/SABIO_RK","systems-biology-sabio")]),
    "curated-protein-description": ("Curated natural protein records", ["enzyme","protein"], ["蛋白註釋","蛋白注释"], [
        ("UniProtKB_Swiss-Prot","local-swissprot"),("db-uniprot-api","uniprot-route"),("sequence_data/UniProt","uniprot-archive")]),
    "protein-family-annotation": ("Protein family classifications", ["protein","enzyme"], ["蛋白家族", "歸檔分類"], [
        ("InterPro","local-interpro"),("db-interpro","interpro-route"),("sequence_data/InterPro","interpro-archive"),
        ("db-pfam","pfam-route"),("Proteins/Pfam","pfam-archive"),("HAMAP","hamap-archive"),("CAZy","cazy-archive")]),
    "archive-residue-cross-reference": ("Cross-reference deposited residue identifiers",["protein","antibody","peptide"],
                                        ["坐標對照","结构编号"], [("SIFTS","sifts-archive"),("db-pdbe-kb","pdbe-kb-route")]),
    "pdb-archive": ("Deposited PDB coordinate records", ["protein","antibody","peptide","cyclic-peptide"],
                    ["蛋白沉积","歸檔坐標"], [("Proteins/Protein_Structures/PDB_mmCIF","local-pdb-mmcif")]),
    "archive-structure-classification": ("Structural classification of deposited macromolecules", ["protein"], ["蛋白分類"],
                                         [("db-cath","cath-route")]),
    "model-record-metadata": ("Precomputed public structure-model metadata",["protein"],["預測歸檔","预测版本"],
                              [("db-alphafold-db","alphafold-db-route")]),
    "enzyme-nomenclature": ("Enzyme classification and archival identity", ["enzyme"], ["EC 命名","酶名索引"], [
        ("Systems_Biology/enzymes/ENZYME","enzyme-archive"),("Systems_Biology/enzymes/ExplorEnz","explorenz-archive"),
        ("indexes/enzyme-food-20260128-sprot2026_03/lookup.sqlite","enzyme-food-swissprot")]),
    "annotation-interchange": ("Enzyme experiment metadata standard",["enzyme"],["數據標準","记录格式"],
                               [("EnzymeML_Specification","enzymeml-spec")]),
    "compound-archive-record": ("Small-molecule identifier annotations", ["enzyme"], ["底物編號","反应物"],
                                [("Systems_Biology/metabolic_networks/ChEBI","chebi-archive")]),
}
