"""Read the specified pair/window from a previously published interface table."""


def sufficient_metric(parameters, metric_id, table_capability, output_name, table_document):
    if (table_capability != "pcna-interface@1" or output_name != "interface-table.json"
            or table_document.get("schema") != "gx.pcna-interface-table/1"
            or metric_id not in ("n_contact_residue_pairs", "buried_area_A2")):
        return False
    pair = table_document.get("pairs", {}).get(parameters.get("pair", "12"), {})
    block = parameters.get("block_index", -1)
    if block == -1:
        return pair.get("whole", {}).get(metric_id) is not None
    return any(row["block_index"] == block and row.get(metric_id) is not None
               for row in pair.get("windows", ()))
