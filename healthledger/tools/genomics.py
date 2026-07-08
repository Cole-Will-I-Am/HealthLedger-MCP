"""Genomic and pharmacogenomic records."""
from healthledger.runtime import *  # noqa: F401,F403


@mcp.tool(annotations={"title": "Add genomic/PGx record", "readOnlyHint": False, "idempotentHint": False})
def add_genomic_record(
    record_type: str,
    gene: str | None = None,
    rsid: str | None = None,
    hgvs_c: str | None = None,
    hgvs_p: str | None = None,
    zygosity: str | None = None,
    clinical_significance: str | None = None,
    inheritance_pattern: str | None = None,
    associated_condition: str | None = None,
    pgx_phenotype: str | None = None,
    pgx_drug: str | None = None,
    pgx_guideline_source: str | None = None,
    polygenic_trait: str | None = None,
    polygenic_score: float | None = None,
    polygenic_percentile: float | None = None,
    test_date: str | None = None,
    report_date: str | None = None,
    lab_name: str | None = None,
    ordering_provider: str | None = None,
    methodology: str | None = None,
    document_id: int | None = None,
    source: str | None = None,
    notes: str | None = None,
    extra_json: str | None = None,
    transcript: str | None = None,
    user: str | None = None,
) -> dict:
    """Store one structured genomic/PGx record as lab-reported data."""
    u = _tool_user(user, "add_genomic_record")
    clean_type = _required_text(record_type, "record_type", max_chars=40).lower()
    if clean_type not in GENOMIC_RECORD_TYPES:
        allowed = "|".join(sorted(GENOMIC_RECORD_TYPES))
        raise ValueError(f"record_type must be one of {allowed}")
    clean_significance = _optional_text(clinical_significance, "clinical_significance", max_chars=40)
    if clean_significance is not None:
        clean_significance = clean_significance.lower()
        if clean_significance not in CLINICAL_SIGNIFICANCE:
            allowed = "|".join(sorted(CLINICAL_SIGNIFICANCE))
            raise ValueError(f"clinical_significance must be one of {allowed}")
    clean_polygenic_percentile = _optional_finite_float(polygenic_percentile, "polygenic_percentile")
    if clean_polygenic_percentile is not None and not 0.0 <= clean_polygenic_percentile <= 100.0:
        raise ValueError("polygenic_percentile must be between 0 and 100")
    row = {
        "user": u,
        "created_ts": _now_iso(),
        "record_type": clean_type,
        "test_date": _parse_date(test_date, "test_date"),
        "report_date": _parse_date(report_date, "report_date"),
        "lab_name": _optional_text(lab_name, "lab_name", max_chars=160),
        "ordering_provider": _optional_text(ordering_provider, "ordering_provider", max_chars=160),
        "methodology": _optional_text(methodology, "methodology", max_chars=80),
        "gene": _optional_text(gene, "gene", max_chars=80),
        "transcript": _optional_text(transcript, "transcript", max_chars=120),
        "hgvs_c": _optional_text(hgvs_c, "hgvs_c", max_chars=200),
        "hgvs_p": _optional_text(hgvs_p, "hgvs_p", max_chars=200),
        "rsid": _optional_text(rsid, "rsid", max_chars=80),
        "zygosity": _optional_text(zygosity, "zygosity", max_chars=80),
        "clinical_significance": clean_significance,
        "inheritance_pattern": _optional_text(inheritance_pattern, "inheritance_pattern", max_chars=160),
        "associated_condition": _optional_text(associated_condition, "associated_condition", max_chars=240),
        "pgx_phenotype": _optional_text(pgx_phenotype, "pgx_phenotype", max_chars=160),
        "pgx_drug": _optional_text(pgx_drug, "pgx_drug", max_chars=160),
        "pgx_guideline_source": _optional_text(pgx_guideline_source, "pgx_guideline_source", max_chars=120),
        "polygenic_trait": _optional_text(polygenic_trait, "polygenic_trait", max_chars=160),
        "polygenic_score": _optional_finite_float(polygenic_score, "polygenic_score"),
        "polygenic_percentile": clean_polygenic_percentile,
        "document_id": int(document_id) if document_id else None,
        "source": _optional_text(source, "source", max_chars=200),
        "notes": _optional_text(notes, "notes"),
        "extra_json": _json_text(extra_json, "extra_json"),
    }
    # TODO: genomic/PGx records may need per-domain consent gating in shared deployments.
    _audit("genomic_record.write", f"{_audit_user(u)} type={clean_type} gene={row['gene']}")
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO genomic_records(user, created_ts, record_type, test_date, report_date, lab_name, "
            "ordering_provider, methodology, gene, transcript, hgvs_c, hgvs_p, rsid, zygosity, "
            "clinical_significance, inheritance_pattern, associated_condition, pgx_phenotype, pgx_drug, "
            "pgx_guideline_source, polygenic_trait, polygenic_score, polygenic_percentile, document_id, source, "
            "notes, extra_json) "
            "VALUES (:user, :created_ts, :record_type, :test_date, :report_date, :lab_name, "
            ":ordering_provider, :methodology, :gene, :transcript, :hgvs_c, :hgvs_p, :rsid, :zygosity, "
            ":clinical_significance, :inheritance_pattern, :associated_condition, :pgx_phenotype, :pgx_drug, "
            ":pgx_guideline_source, :polygenic_trait, :polygenic_score, :polygenic_percentile, :document_id, "
            ":source, :notes, :extra_json)",
            row,
        )
        row["id"] = cur.lastrowid
    return row


@mcp.tool(annotations={"title": "List genomic/PGx records", "readOnlyHint": True, "idempotentHint": True})
def list_genomic_records(
    record_type: str | None = None,
    gene: str | None = None,
    user: str | None = None,
    limit: int = 200,
) -> dict:
    """List genomic/PGx records, optionally filtered by type or gene."""
    u = _tool_user(user, "list_genomic_records")
    lim = _limit(limit, default=200)
    sql = "SELECT * FROM genomic_records WHERE user=?"
    args: list = [u]
    if record_type:
        clean_type = _required_text(record_type, "record_type", max_chars=40).lower()
        if clean_type not in GENOMIC_RECORD_TYPES:
            allowed = "|".join(sorted(GENOMIC_RECORD_TYPES))
            raise ValueError(f"record_type must be one of {allowed}")
        sql += " AND record_type=?"
        args.append(clean_type)
    clean_gene = _optional_text(gene, "gene", max_chars=80)
    if clean_gene:
        sql += " AND gene = ? COLLATE NOCASE"
        args.append(clean_gene)
    sql += " ORDER BY COALESCE(test_date, created_ts) DESC, id DESC LIMIT ?"
    args.append(lim)
    _audit("list_genomic_records", f"{_audit_user(u)} limit={lim}")
    with _db() as conn:
        rows = _rows(conn.execute(sql, args))
    return {"user": u, "count": len(rows), "genomic_records": rows}
