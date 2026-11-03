"""Civil & Structural Engineering outreach: one field, one faculty table."""

from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path


DEFAULT_DOMAIN = "civil"

# QS Civil & Structural Engineering is one table. Do not split geotech / structures /
# transportation / water / construction into separate outreach domains.
DOMAINS: dict[str, dict[str, object]] = {
    "civil": {
        "label": "Civil and Structural Engineering (all civil domains)",
        "aliases": (
            "civil",
            "civil engineering",
            "structural",
            "structural engineering",
            "civil and structural",
            "geotechnical",
            "transportation",
            "water resources",
            "construction",
            "environmental civil",
        ),
        # OpenAlex subfields covering the civil bucket as one field.
        # 2305 Environmental Engineering is omitted: it matches chemists and
        # mechanical engineers. Water / environmental-civil work still appears
        # under 2205 (hydraulics, water systems, infrastructure).
        "openalex_subfields": frozenset(
            {
                "2205",  # Civil and Structural Engineering
                "2215",  # Building and Construction
            }
        ),
        "keywords": frozenset(
            {
                "civil engineering",
                "structural engineering",
                "structural analysis",
                "geotechnical",
                "geotech",
                "soil mechanics",
                "foundation engineering",
                "transportation engineering",
                "traffic engineering",
                "pavement",
                "highway engineering",
                "water resources",
                "hydraulics",
                "hydrology",
                "coastal engineering",
                "construction engineering",
                "construction management",
                "building and construction",
                "environmental engineering",
                "wastewater",
                "earthquake engineering",
                "seismic",
                "bridge engineering",
                "concrete",
                "steel structures",
                "tunneling",
                "rock mechanics",
                "infrastructure",
            }
        ),
    },
}

# Europe (EU + UK + CH + NO + IS + LI + Balkan/EU-adjacent) plus US, CA, JP, SG, AU, NZ.
# China, India, Korea, Hong Kong, Taiwan, Middle East, Latin America, and Africa are excluded
# unless they appear in this set (they do not).
ALLOWED_COUNTRY_CODES = frozenset(
    {
        "US",
        "CA",
        "JP",
        "SG",
        "AU",
        "NZ",
        "UK",
        "GB",
        "IE",
        "FR",
        "DE",
        "NL",
        "BE",
        "LU",
        "CH",
        "AT",
        "IT",
        "ES",
        "PT",
        "SE",
        "NO",
        "DK",
        "FI",
        "IS",
        "PL",
        "CZ",
        "SK",
        "HU",
        "RO",
        "BG",
        "GR",
        "HR",
        "SI",
        "EE",
        "LV",
        "LT",
        "CY",
        "MT",
        "RS",
        "BA",
        "ME",
        "MK",
        "AL",
        "UA",
        "BY",
        "LI",
    }
)

_TABLE_OK = re.compile(r"^faculty_[a-z0-9]+$")
CONTACTED_STATUSES = ("sent", "bounced", "feedback")


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _alias_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for domain_id, spec in DOMAINS.items():
        index[_norm(domain_id)] = domain_id
        index[_norm(str(spec["label"]))] = domain_id
        for alias in spec["aliases"]:  # type: ignore[union-attr]
            index[_norm(str(alias))] = domain_id
    return index


ALIASES = _alias_index()


def domain_choices() -> str:
    lines = ["Known domains (use one at a time):"]
    for domain_id, spec in DOMAINS.items():
        lines.append(f"  {domain_id:<14} {spec['label']}")
    return "\n".join(lines)


def unranked_domain_message(value: str) -> str:
    return (
        f"This pipeline only targets Civil and Structural Engineering, not {value!r}.\n"
        "Faculty lists come from the QS Civil & Structural Engineering ranking "
        "(one bucket: structures, geotech, transportation, water, construction, "
        "environmental civil). There is no domain split like CS.\n"
        + domain_choices()
    )


def resolve_domain(value: str) -> str:
    domain_id = ALIASES.get(_norm(value))
    if domain_id is None:
        raise RuntimeError(unranked_domain_message(value))
    return domain_id


def add_domain_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--domain",
        default=DEFAULT_DOMAIN,
        help="Research field (only 'civil' is supported; default: civil).",
    )
    parser.add_argument(
        "--list-domains",
        action="store_true",
        help="Print the civil field this pipeline targets, then exit.",
    )


def resolve_domain_choice(cli_domain: str | None) -> str:
    if cli_domain and str(cli_domain).strip():
        return resolve_domain(cli_domain)
    return DEFAULT_DOMAIN


def domain_label(domain_id: str) -> str:
    return str(DOMAINS[domain_id]["label"])


def domain_keywords(domain_id: str) -> frozenset[str]:
    return frozenset(DOMAINS[domain_id]["keywords"])  # type: ignore[arg-type]


def openalex_subfields(domain_id: str) -> frozenset[str]:
    return frozenset(DOMAINS[domain_id]["openalex_subfields"])  # type: ignore[arg-type]


def country_allowed(country_code: str | None) -> bool:
    if not country_code:
        return False
    return country_code.strip().upper() in ALLOWED_COUNTRY_CODES


def faculty_table(domain_id: str) -> str:
    table = f"faculty_{domain_id}"
    if not _TABLE_OK.fullmatch(table):
        raise RuntimeError(f"Invalid faculty table name: {table}")
    return table


def faculty_table_names(connection: sqlite3.Connection) -> list[str]:
    migrate_legacy_faculty_table(connection)
    names = []
    for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY 1"
    ):
        if _TABLE_OK.fullmatch(str(row[0])):
            names.append(str(row[0]))
    return names


def already_contacted(
    connection: sqlite3.Connection,
) -> tuple[set[tuple[str, str]], set[str]]:
    """People we already emailed. Used so a later scrape does not mail them again."""
    people: set[tuple[str, str]] = set()
    emails: set[str] = set()
    placeholders = ",".join("?" * len(CONTACTED_STATUSES))
    for table in faculty_table_names(connection):
        rows = connection.execute(
            f"""
            SELECT name, institution, email
            FROM {table}
            WHERE status IN ({placeholders})
            """,
            CONTACTED_STATUSES,
        )
        for name, institution, email in rows:
            people.add((name, institution))
            if email and str(email).strip():
                emails.add(str(email).strip().lower())
    return people, emails


def is_already_contacted(
    connection: sqlite3.Connection,
    name: str,
    institution: str,
    email: str | None = None,
) -> bool:
    people, emails = already_contacted(connection)
    if (name, institution) in people:
        return True
    if email and str(email).strip().lower() in emails:
        return True
    return False


def delete_unsent_faculty(connection: sqlite3.Connection) -> int:
    """Drop unsent rows. Keep sent/bounced/feedback so the same person is not emailed twice."""
    deleted = 0
    placeholders = ",".join("?" * len(CONTACTED_STATUSES))
    for table in faculty_table_names(connection):
        cursor = connection.execute(
            f"DELETE FROM {table} WHERE status NOT IN ({placeholders})",
            CONTACTED_STATUSES,
        )
        deleted += int(cursor.rowcount or 0)
    connection.commit()
    return deleted


def targets_path(root: Path, domain_id: str) -> Path:
    return root / f"targets_{domain_id}.json"


def ranking_path(root: Path) -> Path:
    return root / "data" / "qs_civil_ranking.json"


def maybe_list_domains(args: argparse.Namespace) -> bool:
    if getattr(args, "list_domains", False):
        print(domain_choices())
        return True
    return False


def migrate_legacy_faculty_table(connection: sqlite3.Connection) -> None:
    """No CS legacy table in this project; kept so the pipeline shape matches Acad."""
    return


def ensure_pipeline_state(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS pipeline_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    connection.commit()


def ensure_faculty_table(connection: sqlite3.Connection, domain_id: str) -> str:
    """Create/migrate the professors table. Returns the table name (faculty_civil)."""
    migrate_legacy_faculty_table(connection)
    table = faculty_table(domain_id)
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {table} (
            name TEXT NOT NULL,
            institution TEXT NOT NULL,
            semantic_scholar_id TEXT,
            openalex_id TEXT,
            email TEXT,
            recent_papers_json TEXT NOT NULL DEFAULT '[]',
            lookup_status TEXT NOT NULL DEFAULT 'pending',
            updated_at TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            draft_text TEXT,
            draft_subject TEXT,
            sent_at TEXT,
            gmail_message_id TEXT,
            send_attempted_at TEXT,
            notes TEXT,
            institution_rank INTEGER,
            author_score REAL,
            PRIMARY KEY (name, institution)
        )
        """
    )
    columns = {
        row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    extra = {
        "semantic_scholar_id": f"ALTER TABLE {table} ADD COLUMN semantic_scholar_id TEXT",
        "openalex_id": f"ALTER TABLE {table} ADD COLUMN openalex_id TEXT",
        "email": f"ALTER TABLE {table} ADD COLUMN email TEXT",
        "recent_papers_json": (
            f"ALTER TABLE {table} ADD COLUMN recent_papers_json TEXT NOT NULL DEFAULT '[]'"
        ),
        "lookup_status": (
            f"ALTER TABLE {table} ADD COLUMN lookup_status TEXT NOT NULL DEFAULT 'pending'"
        ),
        "updated_at": f"ALTER TABLE {table} ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''",
        "status": f"ALTER TABLE {table} ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'",
        "draft_text": f"ALTER TABLE {table} ADD COLUMN draft_text TEXT",
        "draft_subject": f"ALTER TABLE {table} ADD COLUMN draft_subject TEXT",
        "sent_at": f"ALTER TABLE {table} ADD COLUMN sent_at TEXT",
        "gmail_message_id": f"ALTER TABLE {table} ADD COLUMN gmail_message_id TEXT",
        "send_attempted_at": f"ALTER TABLE {table} ADD COLUMN send_attempted_at TEXT",
        "notes": f"ALTER TABLE {table} ADD COLUMN notes TEXT",
        "institution_rank": f"ALTER TABLE {table} ADD COLUMN institution_rank INTEGER",
        "author_score": f"ALTER TABLE {table} ADD COLUMN author_score REAL",
    }
    for column, statement in extra.items():
        if column not in columns:
            connection.execute(statement)
    connection.commit()
    return table


def saved_domain(connection: sqlite3.Connection) -> str | None:
    try:
        row = connection.execute(
            "SELECT value FROM pipeline_state WHERE key = 'active_domain'"
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    try:
        return resolve_domain(str(row[0]))
    except RuntimeError:
        return None
