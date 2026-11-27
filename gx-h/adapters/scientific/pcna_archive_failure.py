"""Read the archive worker's closed eligibility diagnostics, including its pre-marker format."""
import re


METHOD = "pcna-archive-fetch@1"
MAX_LOG_BYTES = 8192
_ENTRY = re.compile(r"(?:[0-9][A-Z0-9]{3}|PDB_[A-Z0-9]{8})", re.ASCII | re.I)
_OBSERVED = rb"ValueError: Expected three deposited model-1 human P12004 polypeptide chains, observed ([0-9]{1,4})"
_ASSEMBLY = b"ValueError: The three observed PCNA chains do not share a deposited assembly operation"
_DECLARED = b"ValueError: The P12004 entity must have exactly three distinct asymmetric-unit label chains"


def describe(parameters, observed, exit_code, stderr=b""):
    entry = parameters.get("entry_id")
    if not isinstance(entry, str) or not _ENTRY.fullmatch(entry):
        return None
    entry = entry.lower() if entry.upper().startswith("PDB_") else entry.upper()
    detail = {"kind": "job_failure", "entry_id": entry,
              "reason_code": "nonzero_exit" if observed == "nonzero_exit" else "process_failure",
              "summary": "The archive Job failed without a verified scientific exclusion."}
    if observed != "nonzero_exit" or exit_code != 1 or len(stderr) > MAX_LOG_BYTES:
        return detail
    body = stderr.rstrip(b"\r\n")
    last = body.rsplit(b"\n", 1)[-1]
    if last.startswith(b"GX_PCNA_EXCLUSION "):
        if body != last:
            return detail
    else:
        lines = body.rsplit(b"\n", 3)
        if (len(lines) != 4 or not body.startswith(b"Traceback (most recent call last):\n")
                or not lines[-3].endswith(b", in pcna_chains") or not lines[-2].startswith(b"    raise ValueError(")):
            return detail
    if last in (_ASSEMBLY, b"GX_PCNA_EXCLUSION assembly"):
        return dict(detail, kind="archive_exclusion", reason_code="pcna_assembly_mismatch",
                    summary="The three observed PCNA chains do not share a deposited assembly operation.")
    if last == _DECLARED:
        return dict(detail, kind="archive_exclusion", reason_code="pcna_chain_count",
                    summary="The PCNA entity lacks exactly three distinct declared asymmetric-unit label chains.")
    count = re.fullmatch(rb"GX_PCNA_EXCLUSION (declared|observed) ([0-9]{1,4})", last)
    legacy = re.fullmatch(_OBSERVED, last)
    if count or legacy:
        declared = bool(count and count[1] == b"declared")
        n = int(count[2] if count else legacy[1])
        if n == 3:
            return detail
        population = "distinct declared asymmetric-unit label chains" if declared else "observed model-1 polypeptide chains"
        return dict(detail, kind="archive_exclusion", reason_code="pcna_chain_count",
                    chain_count=n, summary=f"The method requires three {population} for human P12004. The entry has {n}.")
    return detail
