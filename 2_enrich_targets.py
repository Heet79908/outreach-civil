"""Enrich QS/OpenAlex civil targets with recent Crossref work metadata."""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

import argparse
import html
import json
import os
import re
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
from tqdm import tqdm

import domains


API_ROOT = "https://api.crossref.org"
OPENALEX_ROOT = "https://api.openalex.org"
REQUEST_DELAY_SECONDS = 0.15
CROSSREF_TIMEOUT_SECONDS = 8

_skip_crossref = False
_crossref_fail_streak = 0
_skip_openalex = False
_openalex_fail_streak = 0


def crossref_mailto() -> str:
    return (os.environ.get("EMAIL_USER") or "anonymous@example.com").strip()


def user_agent() -> str:
    return f"civil-faculty-outreach/1.0 (mailto:{crossref_mailto()})"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Store recent Crossref/OpenAlex works for civil faculty targets."
    )
    domains.add_domain_argument(parser)
    parser.add_argument(
        "--targets",
        type=Path,
        help="Input target list (default: targets_<domain>.json).",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(__file__).with_name("outreach.db"),
        help="SQLite output (default: outreach.db beside this script).",
    )
    return parser.parse_args()


def api_get(path: str, params: dict[str, str]) -> dict[str, Any]:
    """Call Crossref's Polite Pool and return JSON."""
    url = f"{API_ROOT}{path}?{urlencode(params)}&mailto={crossref_mailto()}"
    headers = {"User-Agent": user_agent(), "Accept": "application/json"}
    response = requests.get(url, headers=headers, timeout=CROSSREF_TIMEOUT_SECONDS)
    response.raise_for_status()
    payload = response.json()
    time.sleep(REQUEST_DELAY_SECONDS)
    return payload


def openalex_get(path: str, params: dict[str, str]) -> dict[str, Any]:
    response = requests.get(
        f"{OPENALEX_ROOT}{path}",
        params=params,
        headers={"User-Agent": user_agent()},
        timeout=20,
    )
    response.raise_for_status()
    time.sleep(REQUEST_DELAY_SECONDS)
    return response.json()


def strip_jats(value: str | None) -> str | None:
    """Convert Crossref JATS/XML abstract markup into clean text."""
    if not value:
        return None
    text = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(text)).strip() or None


def reconstruct_abstract(inverted: object) -> str | None:
    if not isinstance(inverted, dict) or not inverted:
        return None
    slots: list[tuple[int, str]] = []
    for word, positions in inverted.items():
        if not isinstance(positions, list):
            continue
        for position in positions:
            if isinstance(position, int):
                slots.append((position, str(word)))
    slots.sort()
    text = " ".join(word for _position, word in slots).strip()
    return text or None


def papers_from_crossref(name: str, institution: str) -> list[dict[str, str | None]]:
    response = api_get(
        "/works",
        {
            "query.author": name,
            "query.affiliation": institution,
            "filter": "from-pub-date:2024-01-01",
            "rows": "1",
        },
    )
    items = response.get("message", {}).get("items", [])
    if not items:
        return []
    work = items[0]
    titles = work.get("title") or []
    date_parts = work.get("published", {}).get("date-parts") or []
    year = date_parts[0][0] if date_parts and date_parts[0] else None
    return [{
        "id": work.get("DOI"),
        "title": titles[0] if titles else None,
        "publication_date": str(year) if year else None,
        "abstract": strip_jats(work.get("abstract")),
    }]


def papers_from_openalex(
    name: str, institution: str, openalex_id: str | None = None
) -> list[dict[str, str | None]]:
    author_id = (openalex_id or "").strip()
    if not author_id:
        authors = openalex_get("/authors", {"search": name, "per-page": "5"})
        results = authors.get("results") or []
        if not isinstance(results, list) or not results:
            return []
        bits = {part.lower() for part in re.split(r"[^a-zA-Z]+", institution) if len(part) > 3}
        chosen = None
        for author in results:
            if not isinstance(author, dict):
                continue
            blob = json.dumps(
                author.get("last_known_institutions") or author.get("affiliations") or []
            ).lower()
            if bits and any(bit in blob for bit in bits):
                chosen = author
                break
        if chosen is None:
            chosen = results[0] if isinstance(results[0], dict) else None
        if not chosen or not chosen.get("id"):
            return []
        author_id = str(chosen["id"]).rsplit("/", 1)[-1]
    author_id = str(author_id).rsplit("/", 1)[-1]
    works = openalex_get(
        "/works",
        {
            "filter": f"authorships.author.id:{author_id},from_publication_date:2024-01-01",
            "per-page": "1",
            "sort": "publication_date:desc",
        },
    )
    work = (works.get("results") or [None])[0]
    if not isinstance(work, dict):
        return []
    year = work.get("publication_year")
    doi = work.get("doi")
    return [{
        "id": (str(doi).replace("https://doi.org/", "") if doi else work.get("id")),
        "title": work.get("display_name"),
        "publication_date": str(year) if year else None,
        "abstract": reconstruct_abstract(work.get("abstract_inverted_index")),
    }]


def recent_papers(
    name: str, institution: str, openalex_id: str | None = None
) -> list[dict[str, str | None]]:
    """Fetch one recent paper via Crossref, then OpenAlex if Crossref is down."""
    global _skip_crossref, _crossref_fail_streak, _skip_openalex, _openalex_fail_streak
    if _skip_openalex:
        return []
    if not _skip_crossref:
        try:
            papers = papers_from_crossref(name, institution)
            _crossref_fail_streak = 0
            if papers:
                return papers
        except (requests.RequestException, OSError, ValueError, RuntimeError) as error:
            _crossref_fail_streak += 1
            if _crossref_fail_streak >= 2:
                _skip_crossref = True
                print(
                    "Crossref unreachable; using OpenAlex for remaining paper lookups.",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                print(f"Crossref skip {name}: {error}", file=sys.stderr, flush=True)
    try:
        papers = papers_from_openalex(name, institution, openalex_id)
        _openalex_fail_streak = 0
        return papers
    except (requests.RequestException, OSError, ValueError, RuntimeError) as error:
        _openalex_fail_streak += 1
        print(f"OpenAlex skip {name} ({institution}): {error}", file=sys.stderr, flush=True)
        if _openalex_fail_streak >= 3:
            _skip_openalex = True
            print(
                "OpenAlex rate-limited; finishing remaining faculty without paper abstracts.",
                file=sys.stderr,
                flush=True,
            )
        return []


def open_database(path: Path, domain_id: str) -> tuple[sqlite3.Connection, str]:
    connection = sqlite3.connect(path)
    domains.ensure_pipeline_state(connection)
    table = domains.ensure_faculty_table(connection, domain_id)
    return connection, table


def apply_ranks_from_targets(
    connection: sqlite3.Connection,
    table: str,
    targets: list[dict],
) -> int:
    """Stamp QS institution/author priority onto existing faculty rows."""
    updated = 0
    for target in targets:
        if not {"name", "institution"} <= target.keys():
            continue
        rank = target.get("institution_rank")
        if rank is None:
            continue
        try:
            rank_value = int(rank)
        except (TypeError, ValueError):
            continue
        try:
            author_score = float(target.get("author_score") or 0)
        except (TypeError, ValueError):
            author_score = 0.0
        cursor = connection.execute(
            f"""
            UPDATE {table}
            SET institution_rank = ?, author_score = ?
            WHERE name = ? AND institution = ?
            """,
            (rank_value, author_score, target["name"], target["institution"]),
        )
        updated += cursor.rowcount
    connection.commit()
    return updated


def is_processed(connection: sqlite3.Connection, table: str, name: str, institution: str) -> bool:
    if domains.is_already_contacted(connection, name, institution):
        return True
    return connection.execute(
        f"SELECT 1 FROM {table} WHERE name = ? AND institution = ?",
        (name, institution),
    ).fetchone() is not None


def save_faculty(
    connection: sqlite3.Connection,
    table: str,
    target: dict[str, Any],
    papers: list[dict[str, str | None]],
) -> None:
    # Crossref does not expose personal email addresses in work search responses.
    email = None
    connection.execute(
        f"""
        INSERT INTO {table} (
            name, institution, semantic_scholar_id, openalex_id, email, recent_papers_json,
            lookup_status, updated_at, institution_rank, author_score
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name, institution) DO UPDATE SET
            semantic_scholar_id = excluded.semantic_scholar_id,
            openalex_id = COALESCE(excluded.openalex_id, {table}.openalex_id),
            email = COALESCE(NULLIF(TRIM(excluded.email), ''), {table}.email),
            recent_papers_json = excluded.recent_papers_json,
            lookup_status = excluded.lookup_status,
            updated_at = excluded.updated_at,
            institution_rank = excluded.institution_rank,
            author_score = excluded.author_score
        """,
        (
            target["name"],
            target["institution"],
            None,
            target.get("openalex_id"),
            email,
            json.dumps(papers, ensure_ascii=False),
            "matched" if papers else "not_found",
            datetime.now(UTC).isoformat(),
            int(target["institution_rank"]) if target.get("institution_rank") is not None else None,
            float(target.get("author_score") or 0),
        ),
    )


def main() -> int:
    args = parse_args()
    if domains.maybe_list_domains(args):
        return 0
    connection = sqlite3.connect(args.database)
    domains.ensure_pipeline_state(connection)
    domain_id = domains.resolve_domain_choice(args.domain)
    table = domains.ensure_faculty_table(connection, domain_id)
    targets_file = args.targets or domains.targets_path(Path(__file__).resolve().parent, domain_id)
    connection.close()

    try:
        targets = json.loads(targets_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"Error reading {targets_file}: {error}", file=sys.stderr)
        return 1

    if not isinstance(targets, list):
        print("Error: targets JSON must be a list.", file=sys.stderr)
        return 1

    connection, table = open_database(args.database, domain_id)
    processed = matched = not_found = 0
    try:
        for target in tqdm(targets, desc="Enriching targets", unit="author"):
            if not {"name", "institution"} <= target.keys():
                print(f"Skipping malformed target: {target!r}", file=sys.stderr)
                continue
            if is_processed(connection, table, target["name"], target["institution"]):
                continue

            papers = recent_papers(
                target["name"],
                target["institution"],
                target.get("openalex_id"),
            )
            save_faculty(connection, table, target, papers)
            connection.commit()
            processed += 1
            matched += bool(papers)
            not_found += not papers
        ranked = apply_ranks_from_targets(connection, table, targets)
        if ranked:
            print(f"Updated send priority for {ranked} existing faculty row(s).")
    finally:
        connection.close()

    print(f"Finished: {processed} processed, {matched} matched, {not_found} not found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
