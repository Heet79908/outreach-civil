"""Resolve missing faculty emails from the public homepage, ORCID, or Wikipedia URL."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from requests import RequestException

import domains


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
EMAIL_PATTERN = re.compile(
    r"\b[A-Z0-9][A-Z0-9._%+-]{0,63}@"
    r"(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+"
    r"[A-Z]{2,63}\b",
    re.IGNORECASE,
)
BRACKETED_OBFUSCATION_PATTERN = re.compile(
    r"(?:\[|\(|\{)\s*(at|dot)\s*(?:\]|\)|\})", re.IGNORECASE
)
SPACED_OBFUSCATION_PATTERN = re.compile(r"\s+(at|dot)\s+", re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract public emails from homepage / ORCID / Wikipedia fallback URLs."
    )
    domains.add_domain_argument(parser)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(__file__).with_name("outreach.db"),
        help="SQLite database path (default: outreach.db beside this script).",
    )
    parser.add_argument(
        "--targets",
        type=Path,
        help="Target list path (default: targets_<domain>.json).",
    )
    parser.add_argument("--limit", type=int, help="Process at most this many rows.")
    parser.add_argument(
        "-r",
        "--restart",
        action="store_true",
        help="Clear resolved emails and rebuild the queue from the start.",
    )
    return parser.parse_args()


def normalize_obfuscations(text: str) -> str:
    """Turn common textual '[at]' / '[dot]' encodings into an email address."""
    def replace(match: re.Match[str]) -> str:
        return "@" if match.group(1).lower() == "at" else "."

    text = BRACKETED_OBFUSCATION_PATTERN.sub(replace, text)
    text = SPACED_OBFUSCATION_PATTERN.sub(replace, text)
    return re.sub(r"\s*([@.])\s*", r"\1", text)


def sanitize_markup(html: str) -> str:
    """Drop incomplete numeric entities that crash html.parser's int() conversion."""
    return re.sub(r"&#x?[0-9a-fA-F]*;?", "", html)


def parse_homepage(html: str) -> BeautifulSoup | None:
    """Parse homepage HTML, ignoring malformed markup and broken entities."""
    try:
        return BeautifulSoup(html, "html.parser")
    except (ValueError, TypeError):
        try:
            return BeautifulSoup(sanitize_markup(html), "html.parser")
        except (ValueError, TypeError):
            return None


def extract_emails(html: str) -> list[str]:
    soup = parse_homepage(html)
    candidates = []

    if soup is not None:
        # mailto links retain useful addresses even when the visible link text differs.
        for link in soup.select("a[href^='mailto:']"):
            address = link.get("href", "")[7:].split("?", 1)[0]
            candidates.append(address)
        text = soup.get_text(" ", strip=True)
    else:
        text = sanitize_markup(html)

    candidates.extend(EMAIL_PATTERN.findall(normalize_obfuscations(text)))
    return sorted({email.lower() for email in candidates if EMAIL_PATTERN.fullmatch(email)})


def homepage_domain(url: str) -> str:
    host = urlparse(url).hostname or ""
    return host.lower().removeprefix("www.")


def choose_email(emails: list[str], homepage_url: str) -> str | None:
    """Prefer emails at the academic or homepage-host university domain."""
    if not emails:
        return None
    home_domain = homepage_domain(homepage_url)

    def score(email: str) -> tuple[int, str]:
        domain = email.rsplit("@", 1)[1]
        matches_homepage = home_domain and (
            domain == home_domain
            or domain.endswith(f".{home_domain}")
            or home_domain.endswith(f".{domain}")
        )
        return (
            2 if matches_homepage else 1 if domain.endswith(".edu") else 0,
            email,
        )

    return max(emails, key=score)


def homepage_index(targets_path: Path) -> dict[tuple[str, str], str]:
    targets = json.loads(targets_path.read_text(encoding="utf-8"))
    return {
        (target["name"], target["institution"]): target["homepage"]
        for target in targets
        if target.get("name") and target.get("institution") and target.get("homepage")
    }


def ensure_schema(connection: sqlite3.Connection, domain_id: str) -> str:
    domains.ensure_pipeline_state(connection)
    return domains.ensure_faculty_table(connection, domain_id)


def reset_email_queue(connection: sqlite3.Connection, table: str) -> int:
    """Clear emails so the resolver starts from the beginning."""
    connection.execute(
        f"""
        UPDATE {table}
        SET email = NULL, status = 'pending'
        WHERE status NOT IN ('sent', 'sending')
        """
    )
    connection.commit()
    return connection.total_changes


def mark_skipped(
    connection: sqlite3.Connection, table: str, name: str, institution: str
) -> None:
    connection.execute(
        f"""
        UPDATE {table}
        SET status = 'skipped'
        WHERE name = ? AND institution = ?
          AND status NOT IN ('sent', 'sending')
        """,
        (name, institution),
    )
    connection.commit()


def main() -> int:
    args = parse_args()
    if domains.maybe_list_domains(args):
        return 0
    connection = sqlite3.connect(args.database)
    domains.ensure_pipeline_state(connection)
    domain_id = domains.resolve_domain_choice(args.domain)
    table = ensure_schema(connection, domain_id)
    targets_file = args.targets or domains.targets_path(
        Path(__file__).resolve().parent, domain_id
    )
    try:
        homepages = homepage_index(targets_file)
    except (OSError, json.JSONDecodeError) as error:
        connection.close()
        print(f"Error reading {targets_file}: {error}", file=sys.stderr)
        return 1

    try:
        if args.restart:
            cleared = reset_email_queue(connection, table)
            print(f"Restarted queue: reset {cleared} records.")
        rows = connection.execute(
            f"""
            SELECT name, institution, email
            FROM {table}
            WHERE (email IS NULL OR TRIM(email) = '')
              AND status != 'skipped'
            ORDER BY
            COALESCE(institution_rank, 9999) ASC,
            COALESCE(author_score, 0) DESC,
            name ASC,
            institution ASC
            """
        ).fetchall()
        people, emails = domains.already_contacted(connection)
        resolved = no_email = failed = 0
        for name, institution, _email in rows:
            if (name, institution) in people:
                continue
            if args.limit is not None and resolved + no_email + failed >= args.limit:
                break
            homepage = homepages.get((name, institution))
            if not homepage:
                no_email += 1
                mark_skipped(connection, table, name, institution)
                print(f"Skipped {name} ({institution}): no homepage.", flush=True)
                continue

            try:
                response = requests.get(
                    homepage,
                    headers={"User-Agent": USER_AGENT},
                    timeout=20,
                )
                response.raise_for_status()
                email = choose_email(extract_emails(response.text), response.url)
            except (RequestException, ValueError, TypeError, OSError) as error:
                failed += 1
                mark_skipped(connection, table, name, institution)
                print(f"Skipped {name}'s homepage: {error}", flush=True)
                time.sleep(1)
                continue

            time.sleep(1)
            if not email:
                no_email += 1
                print(f"No email found for {name}.", flush=True)
                continue

            connection.execute(
                f"""
                UPDATE {table}
                SET email = ?, status = 'pending'
                WHERE name = ? AND institution = ?
                """,
                (email, name, institution),
            )
            connection.commit()
            resolved += 1
            print(f"Resolved email for {name}: {email}", flush=True)
    finally:
        connection.close()

    print(f"Finished: {resolved} resolved, {no_email} without email, {failed} failed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
