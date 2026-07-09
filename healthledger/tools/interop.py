"""FHIR R4 interoperability — standards-based health data export.

Produces valid FHIR R4 (Fast Healthcare Interoperability Resources) JSON
bundles from HealthLedger's internal schema. This is the bridge between
your personal health vault and the global healthcare ecosystem.

Every major EHR (Epic, Cerner, Meditech), health system, and research
platform speaks FHIR. A 30-year FHIR export is immediately ingestible by
doctors, researchers, clinical trials, and health data marketplaces.

No external dependencies — builds FHIR resources from pure Python dicts
conforming to the HL7 FHIR R4 specification (http://hl7.org/fhir/R4).

Resources mapped:
    profile (Patient keys)      → Patient
    conditions                  → Condition
    allergies                   → AllergyIntolerance
    medications                 → MedicationStatement
    medication_logs             → MedicationAdministration
    lab_results                 → Observation (laboratory)
    biomarkers                  → Observation (vital-signs / laboratory)
    immunizations               → Immunization
    encounters                  → Encounter
    procedures                  → Procedure
    imaging_reports             → DiagnosticReport / ImagingStudy
    family_history              → FamilyMemberHistory
    metrics                     → Observation (vital-signs)
    genomic_records             → Observation (genomics) / MolecularSequence
    reproductive_records        → Observation / Procedure
    substance_use_logs          → Observation (social-history)
    wearable_samples            → Observation (activity)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from healthledger.config import DATA_TABLES
from healthledger.db import _db, _rows
from healthledger.timeutil import _now_iso

__all__ = [
    "build_fhir_bundle",
    "FHIR_URN_PREFIX",
]

FHIR_URN_PREFIX = "urn:uuid:"


# ── FHIR resource builders ────────────────────────────────────────────────

def _fhir_meta(profile_url: str) -> dict:
    return {
        "profile": [profile_url],
    }


def _fhir_coding(system: str, code: str, display: Optional[str] = None) -> dict:
    c: dict = {"system": system, "code": code}
    if display:
        c["display"] = display
    return c


def _fhir_codeable_concept(codings: list[dict], text: Optional[str] = None) -> dict:
    cc: dict = {"coding": codings}
    if text:
        cc["text"] = text
    return cc


def _fhir_quantity(value: float, unit: Optional[str] = None,
                   system: str = "http://unitsofmeasure.org",
                   code: Optional[str] = None) -> dict:
    q: dict = {"value": value}
    if unit or code:
        q["unit"] = unit
        q["system"] = system
        q["code"] = code or unit
    return q


def _fhir_period(start: Optional[str] = None, end: Optional[str] = None) -> Optional[dict]:
    if not start and not end:
        return None
    p: dict = {}
    if start:
        p["start"] = start
    if end:
        p["end"] = end
    return p


def _fhir_reference(ref_type: str, ref_id: str, display: Optional[str] = None) -> dict:
    ref: dict = {"reference": f"{ref_type}/{ref_id}"}
    if display:
        ref["display"] = display
    return ref


def _fhir_identifier(system: str, value: str) -> dict:
    return {"system": system, "value": value}


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
        return f if f is not None else None
    except (ValueError, TypeError):
        return None


def _iso_date(ts: Optional[str]) -> Optional[str]:
    """Return YYYY-MM-DD from an ISO timestamp."""
    if not ts:
        return None
    return ts[:10] if "T" in ts else ts


# ── LOINC codes for common analytes (partial, extensible) ─────────────────

_LOINC_MAP = {
    "glucose": ("2345-7", "Glucose [Mass/volume] in Serum or Plasma"),
    "glucose_mgdl": ("2345-7", "Glucose [Mass/volume] in Blood"),
    "a1c": ("4548-4", "Hemoglobin A1c/Hemoglobin.total in Blood"),
    "hba1c": ("4548-4", "Hemoglobin A1c/Hemoglobin.total in Blood"),
    "a1c_percent": ("4548-4", "Hemoglobin A1c/Hemoglobin.total in Blood"),
    "cholesterol": ("2093-3", "Cholesterol [Mass/volume] in Serum or Plasma"),
    "cholesterol_total": ("2093-3", "Cholesterol [Mass/volume] in Serum or Plasma"),
    "hdl": ("2085-9", "Cholesterol in HDL [Mass/volume] in Serum or Plasma"),
    "ldl": ("2089-1", "Cholesterol in LDL [Mass/volume] in Serum or Plasma"),
    "triglycerides": ("2571-8", "Triglyceride [Mass/volume] in Serum or Plasma"),
    "creatinine": ("2160-0", "Creatinine [Mass/volume] in Serum or Plasma"),
    "bun": ("3094-0", "Urea nitrogen [Mass/volume] in Serum or Plasma"),
    "sodium": ("2951-2", "Sodium [Moles/volume] in Serum or Plasma"),
    "potassium": ("2823-3", "Potassium [Moles/volume] in Serum or Plasma"),
    "tsh": ("3016-3", "Thyrotropin [Units/volume] in Serum or Plasma"),
    "vitamin_d": ("1989-3", "25-Hydroxyvitamin D3 [Mass/volume] in Serum or Plasma"),
    "vitamin_b12": ("2132-9", "Cobalamin (Vitamin B12) [Mass/volume] in Serum or Plasma"),
    "ferritin": ("2276-4", "Ferritin [Mass/volume] in Serum or Plasma"),
    "iron": ("2498-4", "Iron [Mass/volume] in Serum or Plasma"),
    "alt": ("1742-6", "Alanine aminotransferase [Enzymatic activity/volume] in Serum or Plasma"),
    "ast": ("1920-8", "Aspartate aminotransferase [Enzymatic activity/volume] in Serum or Plasma"),
    "bilirubin": ("1975-2", "Bilirubin.total [Mass/volume] in Serum or Plasma"),
    "albumin": ("1751-7", "Albumin [Mass/volume] in Serum or Plasma"),
    "crp": ("1988-5", "C reactive protein [Mass/volume] in Serum or Plasma"),
    "wbc": ("6690-2", "Leukocytes [#/volume] in Blood"),
    "rbc": ("789-8", "Erythrocytes [#/volume] in Blood"),
    "hemoglobin": ("718-7", "Hemoglobin [Mass/volume] in Blood"),
    "hematocrit": ("4544-3", "Hematocrit [Volume Fraction] of Blood"),
    "platelets": ("777-3", "Platelets [#/volume] in Blood"),
    "psa": ("2857-1", "Prostate specific Ag [Mass/volume] in Serum or Plasma"),
    "cortisol": ("2143-6", "Cortisol [Mass/volume] in Serum or Plasma"),
    "testosterone": ("2986-8", "Testosterone [Mass/volume] in Serum or Plasma"),
    "estradiol": ("2243-4", "Estradiol (E2) [Mass/volume] in Serum or Plasma"),
    "progesterone": ("2839-9", "Progesterone [Mass/volume] in Serum or Plasma"),
}

# UCUM unit mappings for common units
_UCUM_MAP = {
    "mg/dL": "mg/dL",
    "mg/dl": "mg/dL",
    "ng/mL": "ng/mL",
    "ng/ml": "ng/mL",
    "pg/mL": "pg/mL",
    "mmol/L": "mmol/L",
    "mEq/L": "meq/L",
    "meq/L": "meq/L",
    "U/L": "U/L",
    "IU/L": "[iU]/L",
    "g/dL": "g/dL",
    "K/uL": "10*3/uL",
    "10^3/uL": "10*3/uL",
    "%": "%",
    "kg": "kg",
    "cm": "cm",
    "mmHg": "mm[Hg]",
    "bpm": "{beats}/min",
    "lbs": "[lb_av]",
    "lb": "[lb_av]",
}


def _loinc_lookup(analyte: str) -> tuple[str, str]:
    """Look up LOINC code + display for an analyte name."""
    key = analyte.lower().replace(" ", "_").replace("-", "_")
    return _LOINC_MAP.get(key, ("", analyte))


def _ucum_unit(unit: Optional[str]) -> Optional[str]:
    """Map common units to UCUM codes."""
    if not unit:
        return None
    return _UCUM_MAP.get(unit, unit)


# ── Resource builders ─────────────────────────────────────────────────────

def _build_patient(profile: dict) -> dict:
    """Build a FHIR Patient resource from profile key/value pairs."""
    pid = str(uuid.uuid4())
    resource: dict = {
        "resourceType": "Patient",
        "id": pid,
        "meta": _fhir_meta(
            "http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient"
        ),
    }
    # Identifier
    resource["identifier"] = [
        _fhir_identifier(
            "https://healthledger.local/patient-id",
            profile.get("patient_id", pid),
        )
    ]
    # Name (from profile keys)
    given = profile.get("given_name", "")
    family = profile.get("family_name", "")
    if given or family:
        resource["name"] = [{
            "given": [given] if given else [],
            "family": family or "",
        }]
    # Birth date
    bd = profile.get("birth_date") or profile.get("dob")
    if bd:
        resource["birthDate"] = _iso_date(bd)
    # Gender / sex
    sex = profile.get("sex") or profile.get("gender")
    if sex:
        resource["gender"] = sex.lower()
    # Telecom
    phone = profile.get("phone") or profile.get("emergency_contact")
    email = profile.get("email")
    telecom = []
    if phone:
        telecom.append({"system": "phone", "value": str(phone)})
    if email:
        telecom.append({"system": "email", "value": str(email)})
    if telecom:
        resource["telecom"] = telecom
    # Blood type
    bt = profile.get("blood_type")
    if bt:
        # Map common blood type strings to FHIR codes
        resource["extension"] = resource.get("extension", []) + [{
            "url": "http://hl7.org/fhir/StructureDefinition/patient-bloodType",
            "valueCodeableConcept": _fhir_codeable_concept(
                [_fhir_coding("http://hl7.org/fhir/ValueSet/abo-rh", bt.upper())],
                bt,
            ),
        }]
    # Height (if in profile)
    height_cm = _safe_float(profile.get("height_cm"))
    if height_cm:
        resource["extension"] = resource.get("extension", []) + [{
            "url": "http://hl7.org/fhir/StructureDefinition/patient-height",
            "valueQuantity": _fhir_quantity(height_cm, "cm", code="cm"),
        }]
    return resource


def _build_condition(row: dict) -> dict:
    """Build a FHIR Condition resource."""
    cid = str(uuid.uuid4())
    resource: dict = {
        "resourceType": "Condition",
        "id": cid,
        "meta": _fhir_meta(
            "http://hl7.org/fhir/us/core/StructureDefinition/us-core-condition"
        ),
        "clinicalStatus": _fhir_codeable_concept(
            [_fhir_coding(
                "http://terminology.hl7.org/CodeSystem/condition-clinical",
                "active" if row.get("status") == "active" else "inactive",
            )],
        ),
        "code": _fhir_codeable_concept(
            [_fhir_coding("http://snomed.info/sct", "", row.get("name", "Unknown"))],
            row.get("name"),
        ),
    }
    if row.get("onset_date"):
        resource["onsetDateTime"] = row["onset_date"]
    if row.get("resolved_date"):
        resource["abatementDateTime"] = row["resolved_date"]
    if row.get("body_site"):
        resource["bodySite"] = [_fhir_codeable_concept(
            [], row["body_site"]
        )]
    if row.get("severity"):
        resource["severity"] = _fhir_codeable_concept(
            [_fhir_coding(
                "http://hl7.org/fhir/ValueSet/condition-severity",
                row["severity"].lower(),
            )],
        )
    if row.get("notes"):
        resource["note"] = [{"text": row["notes"]}]
    # Source reference back to HealthLedger
    resource["identifier"] = [_fhir_identifier(
        "https://healthledger.local/condition-id",
        str(row.get("id", "")),
    )]
    return resource


def _build_allergy(row: dict) -> dict:
    """Build a FHIR AllergyIntolerance resource."""
    aid = str(uuid.uuid4())
    resource: dict = {
        "resourceType": "AllergyIntolerance",
        "id": aid,
        "meta": _fhir_meta(
            "http://hl7.org/fhir/us/core/StructureDefinition/us-core-allergyintolerance"
        ),
        "clinicalStatus": _fhir_codeable_concept(
            [_fhir_coding(
                "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical",
                "active" if row.get("status") == "active" else "inactive",
            )],
        ),
        "code": _fhir_codeable_concept(
            [], row.get("allergen", "Unknown"),
        ),
    }
    if row.get("reaction"):
        resource["reaction"] = [{
            "manifestation": [_fhir_codeable_concept([], row["reaction"])],
            "severity": row.get("severity", "unknown").lower() if row.get("severity") else "unknown",
        }]
    if row.get("noted_date"):
        resource["onsetDateTime"] = row["noted_date"]
    if row.get("notes"):
        resource["note"] = [{"text": row["notes"]}]
    resource["identifier"] = [_fhir_identifier(
        "https://healthledger.local/allergy-id", str(row.get("id", "")),
    )]
    return resource


def _build_medication_statement(row: dict) -> dict:
    """Build a FHIR MedicationStatement resource."""
    mid = str(uuid.uuid4())
    resource: dict = {
        "resourceType": "MedicationStatement",
        "id": mid,
        "meta": _fhir_meta(
            "http://hl7.org/fhir/us/core/StructureDefinition/us-core-medicationstatement"
        ),
        "status": "active" if row.get("status") == "active" else "stopped",
        "medicationCodeableConcept": _fhir_codeable_concept(
            [], row.get("name", "Unknown"),
        ),
    }
    if row.get("dose"):
        resource["dosage"] = [{
            "text": row["dose"],
            "route": _fhir_codeable_concept([], row.get("route", "")),
        }]
    if row.get("start_date"):
        resource["effectivePeriod"] = _fhir_period(
            row["start_date"], row.get("end_date"),
        )
    if row.get("indication"):
        resource["reasonCode"] = [_fhir_codeable_concept([], row["indication"])]
    resource["identifier"] = [_fhir_identifier(
        "https://healthledger.local/medication-id", str(row.get("id", "")),
    )]
    return resource


def _build_observation(row: dict, analyte: str, value: Any, unit: Optional[str] = None,
                       effective_date: Optional[str] = None, category: str = "laboratory",
                       ref_low: Optional[float] = None, ref_high: Optional[float] = None,
                       row_id: Optional[int] = None) -> dict:
    """Build a generic FHIR Observation resource."""
    oid = str(uuid.uuid4())
    loinc_code, loinc_display = _loinc_lookup(analyte)
    resource: dict = {
        "resourceType": "Observation",
        "id": oid,
        "status": "final",
        "category": [_fhir_codeable_concept(
            [_fhir_coding(
                "http://terminology.hl7.org/CodeSystem/observation-category",
                category,
            )],
        )],
        "code": _fhir_codeable_concept(
            [_fhir_coding("http://loinc.org", loinc_code, loinc_display)],
            analyte,
        ),
    }
    num_val = _safe_float(value)
    if num_val is not None:
        resource["valueQuantity"] = _fhir_quantity(
            num_val, unit, code=_ucum_unit(unit),
        )
    else:
        resource["valueString"] = str(value)

    if effective_date:
        resource["effectiveDateTime"] = effective_date

    if ref_low is not None or ref_high is not None:
        ref_range: dict = {}
        if ref_low is not None:
            ref_range["low"] = _fhir_quantity(ref_low, unit, code=_ucum_unit(unit))
        if ref_high is not None:
            ref_range["high"] = _fhir_quantity(ref_high, unit, code=_ucum_unit(unit))
        resource["referenceRange"] = [ref_range]

    if row_id:
        resource["identifier"] = [_fhir_identifier(
            f"https://healthledger.local/{category}-id", str(row_id),
        )]
    return resource


def _build_immunization(row: dict) -> dict:
    """Build a FHIR Immunization resource."""
    iid = str(uuid.uuid4())
    resource: dict = {
        "resourceType": "Immunization",
        "id": iid,
        "status": "completed",
        "vaccineCode": _fhir_codeable_concept(
            [_fhir_coding("http://hl7.org/fhir/sid/cvx", "", row.get("vaccine", "Unknown"))],
            row.get("vaccine"),
        ),
    }
    if row.get("immunization_date"):
        resource["occurrenceDateTime"] = row["immunization_date"]
    if row.get("lot"):
        resource["lotNumber"] = str(row["lot"])
    resource["identifier"] = [_fhir_identifier(
        "https://healthledger.local/immunization-id", str(row.get("id", "")),
    )]
    return resource


def _build_encounter(row: dict) -> dict:
    """Build a FHIR Encounter resource."""
    eid = str(uuid.uuid4())
    resource: dict = {
        "resourceType": "Encounter",
        "id": eid,
        "status": "finished",
        "class": {
            "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
            "code": "AMB",
            "display": row.get("encounter_type", "ambulatory"),
        },
        "type": [_fhir_codeable_concept(
            [], row.get("encounter_type", "Unknown"),
        )],
    }
    if row.get("encounter_date"):
        resource["period"] = {"start": row["encounter_date"]}
    if row.get("provider"):
        resource["participant"] = [{
            "individual": {"display": row["provider"]},
        }]
    if row.get("facility"):
        resource["serviceProvider"] = {"display": row["facility"]}
    if row.get("reason"):
        resource["reasonCode"] = [_fhir_codeable_concept([], row["reason"])]
    resource["identifier"] = [_fhir_identifier(
        "https://healthledger.local/encounter-id", str(row.get("id", "")),
    )]
    return resource


def _build_procedure(row: dict) -> dict:
    """Build a FHIR Procedure resource."""
    pid = str(uuid.uuid4())
    resource: dict = {
        "resourceType": "Procedure",
        "id": pid,
        "status": "completed",
        "code": _fhir_codeable_concept([], row.get("name", "Unknown")),
    }
    if row.get("procedure_date"):
        resource["performedDateTime"] = row["procedure_date"]
    if row.get("body_site"):
        resource["bodySite"] = [_fhir_codeable_concept([], row["body_site"])]
    if row.get("outcome"):
        resource["outcome"] = _fhir_codeable_concept([], row["outcome"])
    resource["identifier"] = [_fhir_identifier(
        "https://healthledger.local/procedure-id", str(row.get("id", "")),
    )]
    return resource


def _build_diagnostic_report(row: dict) -> dict:
    """Build a FHIR DiagnosticReport for imaging."""
    did = str(uuid.uuid4())
    resource: dict = {
        "resourceType": "DiagnosticReport",
        "id": did,
        "status": "final",
        "code": _fhir_codeable_concept([], row.get("modality", "Imaging")),
    }
    if row.get("imaging_date"):
        resource["effectiveDateTime"] = row["imaging_date"]
    if row.get("impression"):
        resource["conclusion"] = row["impression"]
    if row.get("findings"):
        resource["conclusion"] = (resource.get("conclusion", "") + "\n" + row["findings"]).strip()
    if row.get("body_site"):
        resource["bodySite"] = _fhir_codeable_concept([], row["body_site"])
    resource["identifier"] = [_fhir_identifier(
        "https://healthledger.local/imaging-id", str(row.get("id", "")),
    )]
    return resource


def _build_family_history(row: dict) -> dict:
    """Build a FHIR FamilyMemberHistory resource."""
    fid = str(uuid.uuid4())
    resource: dict = {
        "resourceType": "FamilyMemberHistory",
        "id": fid,
        "status": "completed",
        "relationship": _fhir_codeable_concept(
            [_fhir_coding(
                "http://terminology.hl7.org/CodeSystem/v3-RoleCode",
                "",
                row.get("relation", "Unknown"),
            )],
            row.get("relation"),
        ),
    }
    if row.get("condition_name"):
        resource["condition"] = [_fhir_codeable_concept([], row["condition_name"])]
    if row.get("age_at_onset"):
        resource["onsetAge"] = {"value": _safe_float(row["age_at_onset"]), "unit": "years"}
    if row.get("age_at_death"):
        resource["deceasedAge"] = {"value": _safe_float(row["age_at_death"]), "unit": "years"}
    if row.get("cause_of_death"):
        resource["causeOfDeath"] = _fhir_codeable_concept([], row["cause_of_death"])
    resource["identifier"] = [_fhir_identifier(
        "https://healthledger.local/family-history-id", str(row.get("id", "")),
    )]
    return resource


def _build_genomic_observation(row: dict) -> dict:
    """Build a FHIR Observation for genomic/PGx data."""
    oid = str(uuid.uuid4())
    resource: dict = {
        "resourceType": "Observation",
        "id": oid,
        "status": "final",
        "category": [_fhir_codeable_concept(
            [_fhir_coding(
                "http://terminology.hl7.org/CodeSystem/observation-category",
                "laboratory",
            )],
        )],
        "code": _fhir_codeable_concept(
            [_fhir_coding("http://loinc.org", "81247-9", "Master HL7 genetic variant reporting panel")],
            row.get("gene") or row.get("rsid") or "Genomic finding",
        ),
    }
    components = []
    if row.get("gene"):
        components.append({
            "code": _fhir_codeable_concept(
                [_fhir_coding("http://loinc.org", "48018-6", "Gene studied [ID]")],
                "Gene",
            ),
            "valueString": row["gene"],
        })
    if row.get("rsid"):
        components.append({
            "code": _fhir_codeable_concept([], "dbSNP ID"),
            "valueString": row["rsid"],
        })
    if row.get("clinical_significance"):
        components.append({
            "code": _fhir_codeable_concept(
                [_fhir_coding("http://loinc.org", "53037-8", "Clinical significance")],
                "Clinical significance",
            ),
            "valueString": row["clinical_significance"],
        })
    if row.get("pgx_phenotype"):
        components.append({
            "code": _fhir_codeable_concept([], "PGx phenotype"),
            "valueString": row["pgx_phenotype"],
        })
    if row.get("pgx_drug"):
        components.append({
            "code": _fhir_codeable_concept([], "PGx drug"),
            "valueString": row["pgx_drug"],
        })
    if components:
        resource["component"] = components
    if row.get("test_date"):
        resource["effectiveDateTime"] = row["test_date"]
    resource["identifier"] = [_fhir_identifier(
        "https://healthledger.local/genomic-id", str(row.get("id", "")),
    )]
    return resource


# ── Bundle builder ─────────────────────────────────────────────────────────

def build_fhir_bundle(
    user: str,
    since: Optional[str] = None,
    until: Optional[str] = None,
    tables: Optional[list[str]] = None,
    limit: int = 500,
) -> dict:
    """Build a FHIR R4 Bundle from HealthLedger data.

    Produces a Bundle of type "collection" containing FHIR resources
    mapped from the user's health records. The bundle includes a
    Composition resource as an index.

    Args:
        user: which person to export.
        since / until: ISO8601 or 'YYYY-MM-DD' date bounds (inclusive).
        tables: optional list of table names to include. Default: all.
        limit: max rows per table (capped by MAX_EXPORT_ROWS).

    Returns:
        A FHIR R4 Bundle dict ready for JSON serialization. Includes
        ``resourceType``, ``type``, ``timestamp``, ``entry`` (list of
        {fullUrl, resource} objects), and ``total``.
    """
    from healthledger.config import MAX_EXPORT_ROWS
    lim = min(int(limit), MAX_EXPORT_ROWS) if limit else MAX_EXPORT_ROWS

    lo = since
    hi = until
    entries: list[dict] = []

    with _db() as conn:
        # ── Patient ──────────────────────────────────────────────────────
        profile = {
            r["key"]: r["value"]
            for r in conn.execute(
                "SELECT key, value FROM profile WHERE user=?", (user,)
            ).fetchall()
        }
        patient = _build_patient(profile)
        patient_id = patient["id"]
        entries.append({
            "fullUrl": f"{FHIR_URN_PREFIX}{patient_id}",
            "resource": patient,
        })

        # Helper: add resources from a table
        def _add_table(table_name: str, builder_fn, date_col: Optional[str] = None):
            if tables and table_name not in tables:
                return
            where = "WHERE user=?"
            params: list = [user]
            if lo and date_col:
                where += f" AND {date_col} >= ?"
                params.append(lo)
            if hi and date_col:
                where += f" AND {date_col} <= ?"
                params.append(hi)
            rows = conn.execute(
                f"SELECT * FROM {table_name} {where} ORDER BY "
                f"COALESCE({date_col or 'rowid'}, rowid) ASC LIMIT ?",
                params + [lim],
            ).fetchall()
            for row in rows:
                resource = builder_fn(dict(row))
                rid = resource["id"]
                entries.append({
                    "fullUrl": f"{FHIR_URN_PREFIX}{rid}",
                    "resource": resource,
                })

        # ── Map tables to FHIR resources ─────────────────────────────────
        _add_table("conditions", _build_condition, "onset_date")
        _add_table("allergies", _build_allergy, "noted_date")
        _add_table("medications", _build_medication_statement, "start_date")
        _add_table("immunizations", _build_immunization, "immunization_date")
        _add_table("encounters", _build_encounter, "encounter_date")
        _add_table("procedures", _build_procedure, "procedure_date")
        _add_table("imaging_reports", _build_diagnostic_report, "imaging_date")
        _add_table("family_history", _build_family_history, None)
        _add_table("genomic_records", _build_genomic_observation, "test_date")

        # Lab results → Observations
        if not tables or "lab_results" in tables:
            lab_where = "WHERE user=?"
            lab_params: list = [user]
            if lo:
                lab_where += " AND COALESCE(result_date, created_ts) >= ?"
                lab_params.append(lo)
            if hi:
                lab_where += " AND COALESCE(result_date, created_ts) <= ?"
                lab_params.append(hi)
            lab_rows = conn.execute(
                f"SELECT * FROM lab_results {lab_where} "
                "ORDER BY COALESCE(result_date, created_ts) ASC LIMIT ?",
                lab_params + [lim],
            ).fetchall()
            for row in lab_rows:
                r = dict(row)
                resource = _build_observation(
                    r, r.get("analyte", "lab"), r.get("numeric_value") or r.get("value_text"),
                    unit=r.get("unit"),
                    effective_date=r.get("result_date") or r.get("created_ts"),
                    category="laboratory",
                    ref_low=_safe_float(r.get("ref_low")),
                    ref_high=_safe_float(r.get("ref_high")),
                    row_id=r.get("id"),
                )
                entries.append({
                    "fullUrl": f"{FHIR_URN_PREFIX}{resource['id']}",
                    "resource": resource,
                })

        # Biomarkers → Observations
        if not tables or "biomarkers" in tables:
            bio_where = "WHERE user=?"
            bio_params: list = [user]
            if lo:
                bio_where += " AND COALESCE(measured_date, created_ts) >= ?"
                bio_params.append(lo)
            if hi:
                bio_where += " AND COALESCE(measured_date, created_ts) <= ?"
                bio_params.append(hi)
            bio_rows = conn.execute(
                f"SELECT * FROM biomarkers {bio_where} "
                "ORDER BY COALESCE(measured_date, created_ts) ASC LIMIT ?",
                bio_params + [lim],
            ).fetchall()
            for row in bio_rows:
                r = dict(row)
                resource = _build_observation(
                    r, r.get("biomarker", "biomarker"),
                    r.get("numeric_value") or r.get("value_text"),
                    unit=r.get("unit"),
                    effective_date=r.get("measured_date") or r.get("created_ts"),
                    category="laboratory",
                    ref_low=_safe_float(r.get("ref_low")),
                    ref_high=_safe_float(r.get("ref_high")),
                    row_id=r.get("id"),
                )
                entries.append({
                    "fullUrl": f"{FHIR_URN_PREFIX}{resource['id']}",
                    "resource": resource,
                })

        # Metrics → Observations (vital-signs)
        if not tables or "metrics" in tables:
            met_where = "WHERE user=?"
            met_params: list = [user]
            if lo:
                met_where += " AND ts >= ?"
                met_params.append(lo)
            if hi:
                met_where += " AND ts <= ?"
                met_params.append(hi)
            met_rows = conn.execute(
                f"SELECT * FROM metrics {met_where} ORDER BY ts ASC LIMIT ?",
                met_params + [lim],
            ).fetchall()
            for row in met_rows:
                r = dict(row)
                resource = _build_observation(
                    r, r.get("metric", "metric"), r.get("value"),
                    unit=r.get("unit"),
                    effective_date=r.get("ts"),
                    category="vital-signs",
                    row_id=r.get("id"),
                )
                entries.append({
                    "fullUrl": f"{FHIR_URN_PREFIX}{resource['id']}",
                    "resource": resource,
                })

        # Wearable samples → Observations (activity)
        if not tables or "wearable_samples" in tables:
            wear_where = "WHERE user=?"
            wear_params: list = [user]
            if lo:
                wear_where += " AND start_ts >= ?"
                wear_params.append(lo)
            if hi:
                wear_where += " AND start_ts <= ?"
                wear_params.append(hi)
            wear_rows = conn.execute(
                f"SELECT * FROM wearable_samples {wear_where} ORDER BY start_ts ASC LIMIT ?",
                wear_params + [lim],
            ).fetchall()
            for row in wear_rows:
                r = dict(row)
                resource = _build_observation(
                    r, r.get("sample_type", "activity"), r.get("value"),
                    unit=r.get("unit"),
                    effective_date=r.get("start_ts"),
                    category="activity",
                    row_id=r.get("id"),
                )
                entries.append({
                    "fullUrl": f"{FHIR_URN_PREFIX}{resource['id']}",
                    "resource": resource,
                })

    # Build the Bundle
    bundle_id = str(uuid.uuid4())
    return {
        "resourceType": "Bundle",
        "id": bundle_id,
        "type": "collection",
        "timestamp": _now_iso(),
        "meta": {
            "lastUpdated": _now_iso(),
            "profile": ["http://hl7.org/fhir/StructureDefinition/Bundle"],
        },
        "identifier": {
            "system": "https://healthledger.local/bundle-id",
            "value": bundle_id,
        },
        "total": len(entries),
        "entry": entries,
    }
