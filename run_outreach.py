"""Run one outreach cycle: optional catalog refresh, then draft-and-send every found address."""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import importlib.util

import domains

ROOT = Path(__file__).resolve().parent
SCRAPE_FACULTY = 500


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="One outreach run: scrape 500 faculty, then send every address found."
    )
    domains.add_domain_argument(parser)
    parser.add_argument("--database", type=Path, default=ROOT / "outreach.db")
    parser.add_argument("--profile", type=Path, default=ROOT / "profile.json")
    parser.add_argument("--targets", type=Path, help="Override targets JSON path.")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Send at most this many. Default: every pending faculty with an email.",
    )
    parser.add_argument(
        "--skip-research",
        action="store_true",
        help="Do not scrape/enrich; only draft and send addresses already in the database.",
    )
    return parser.parse_args()


def log(message: str) -> None:
    print(message, flush=True)


def run_script(script: str, extra: list[str] | None = None) -> None:
    command = [sys.executable, str(ROOT / script), *(extra or [])]
    log(f"$ {' '.join(command)}")
    result = subprocess.run(command, cwd=ROOT, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"{script} exited with {result.returncode}.")


def connect(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    domains.ensure_pipeline_state(connection)
    domains.migrate_legacy_faculty_table(connection)
    connection.commit()
    return connection


def state_get(connection: sqlite3.Connection, key: str, default: str = "") -> str:
    row = connection.execute(
        "SELECT value FROM pipeline_state WHERE key = ?",
        (key,),
    ).fetchone()
    return row["value"] if row else default


def state_set(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        """
        INSERT INTO pipeline_state (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
    connection.commit()


def profile_hash(profile_path: Path) -> str:
    return hashlib.sha256(profile_path.read_bytes()).hexdigest()


def load_modules():
    spec = importlib.util.spec_from_file_location("draft_mod", ROOT / "3_draft_emails.py")
    draft_mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(draft_mod)
    spec2 = importlib.util.spec_from_file_location("dispatch_mod", ROOT / "4_dispatch.py")
    dispatch_mod = importlib.util.module_from_spec(spec2)
    assert spec2.loader is not None
    spec2.loader.exec_module(dispatch_mod)
    return draft_mod, dispatch_mod


def research(args: argparse.Namespace, domain_id: str, targets_file: Path) -> None:
    log(
        f"Research run: cataloguing {SCRAPE_FACULTY} {domains.domain_label(domain_id)} "
        f"faculty ({domain_id}) and resolving emails."
    )
    have_targets = False
    if targets_file.exists():
        try:
            existing = json.loads(targets_file.read_text(encoding="utf-8"))
            have_targets = (
                isinstance(existing, list)
                and SCRAPE_FACULTY - 50 <= len(existing) <= SCRAPE_FACULTY
            )
        except (OSError, json.JSONDecodeError):
            have_targets = False
    if have_targets:
        log(f"Using existing {targets_file.name} ({len(existing)} faculty).")
    else:
        try:
            run_script(
                "1_parse_targets.py",
                [
                    "--domain",
                    domain_id,
                    "--output",
                    str(targets_file),
                    "--max-faculty",
                    str(SCRAPE_FACULTY),
                    "--top",
                    "80",
                ],
            )
        except RuntimeError as error:
            if not targets_file.exists():
                raise
            log(
                f"Live scrape failed ({error}); "
                f"continuing with existing {targets_file.name}."
            )
    run_script(
        "2_enrich_targets.py",
        [
            "--domain",
            domain_id,
            "--targets",
            str(targets_file),
            "--database",
            str(args.database),
        ],
    )
    table = domains.faculty_table(domain_id)
    connection = connect(args.database)
    try:
        domains.ensure_faculty_table(connection, domain_id)
        connection.execute(
            f"""
            UPDATE {table}
            SET status = 'pending'
            WHERE status = 'skipped'
              AND (email IS NULL OR TRIM(email) = '')
            """
        )
        connection.commit()
    finally:
        connection.close()
    run_script(
        "2.5_resolve_emails.py",
        [
            "--domain",
            domain_id,
            "--targets",
            str(targets_file),
            "--database",
            str(args.database),
        ],
    )


def apply_send_priority(
    connection: sqlite3.Connection, table: str, targets_path: Path
) -> None:
    """Apply QS school / OpenAlex author order from targets.json onto the database."""
    if not targets_path.exists():
        return
    try:
        targets = json.loads(targets_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(targets, list) or not targets:
        return
    if "institution_rank" not in targets[0]:
        return
    updated = 0
    for target in targets:
        if not {"name", "institution", "institution_rank"} <= target.keys():
            continue
        try:
            rank = int(target["institution_rank"])
            author_score = float(target.get("author_score") or 0)
        except (TypeError, ValueError):
            continue
        cursor = connection.execute(
            f"""
            UPDATE {table}
            SET institution_rank = ?, author_score = ?
            WHERE name = ? AND institution = ?
            """,
            (rank, author_score, target["name"], target["institution"]),
        )
        updated += cursor.rowcount
    connection.commit()
    if updated:
        log(f"Send order: QS Civil priority applied to {updated} faculty.")


def clear_unsent_drafts(connection: sqlite3.Connection, table: str) -> int:
    connection.execute(
        f"""
        UPDATE {table}
        SET draft_text = NULL, draft_subject = NULL
        WHERE status = 'pending'
        """
    )
    changed = connection.execute("SELECT changes()").fetchone()[0]
    connection.commit()
    return int(changed)


def eligible_rows(connection: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    people, emails = domains.already_contacted(connection)
    rows = connection.execute(
        f"""
        SELECT name, institution, email, draft_text, draft_subject, recent_papers_json
        FROM {table}
        WHERE email IS NOT NULL
          AND TRIM(email) != ''
          AND status = 'pending'
        ORDER BY
        COALESCE(institution_rank, 9999) ASC,
        COALESCE(author_score, 0) DESC,
        name ASC,
        institution ASC
        """
    ).fetchall()
    keep: list[sqlite3.Row] = []
    for row in rows:
        email = (row["email"] or "").strip().lower()
        if (row["name"], row["institution"]) in people or email in emails:
            continue
        keep.append(row)
    return keep


def main() -> int:
    args = parse_args()
    if domains.maybe_list_domains(args):
        return 0
    if not args.profile.exists():
        raise RuntimeError(f"Missing profile: {args.profile}")
    connection = connect(args.database)
    domain_id = domains.resolve_domain_choice(args.domain)
    state_set(connection, "active_domain", domain_id)
    table = domains.ensure_faculty_table(connection, domain_id)
    targets_file = args.targets or domains.targets_path(ROOT, domain_id)
    draft_mod, dispatch_mod = load_modules()
    apply_send_priority(connection, table, targets_file)

    log(
        f"Outreach for {domain_id} "
        f"({domains.domain_label(domain_id)}); table {table}."
    )

    do_research = not args.skip_research
    if do_research:
        connection.close()
        research(args, domain_id, targets_file)
        connection = connect(args.database)
        table = domains.ensure_faculty_table(connection, domain_id)
        apply_send_priority(connection, table, targets_file)
    else:
        log("Skipping faculty research this run.")

    current_hash = profile_hash(args.profile)
    previous_hash = state_get(connection, "profile_hash")
    if previous_hash and previous_hash != current_hash:
        cleared = clear_unsent_drafts(connection, table)
        log(f"profile.json changed; cleared {cleared} unsent draft(s) for rewrite.")
    stale = connection.execute(
        f"""
        UPDATE {table}
        SET draft_text = NULL, draft_subject = NULL
        WHERE status = 'pending'
          AND (draft_subject IS NULL OR TRIM(draft_subject) = '')
          AND draft_text IS NOT NULL AND TRIM(draft_text) != ''
        """
    ).rowcount
    connection.commit()
    if stale:
        log(f"Cleared {stale} unsent draft(s) that had no subject line.")
    state_set(connection, "profile_hash", current_hash)

    profile = draft_mod.load_profile(args.profile)
    user_profile_json = draft_mod.compact_profile_for_prompt(profile)
    model = draft_mod.GEMINI_MODEL
    credentials = ROOT / "credentials.json"
    token = ROOT / "token.json"
    service = dispatch_mod.get_gmail_service(credentials, token)

    rows = eligible_rows(connection, table)
    send_cap = args.limit if args.limit is not None else len(rows)
    if args.limit is None:
        log(f"{len(rows)} pending faculty with email. Sending all {send_cap} found addresses.")
    else:
        log(f"{len(rows)} pending faculty with email. Sending up to {args.limit}.")
    sent = drafted = failed = 0
    seen_people: set[tuple[str, str]] = set()
    seen_emails: set[str] = set()
    try:
        for row in rows:
            if sent >= send_cap:
                break
            name = row["name"]
            institution = row["institution"]
            email = (row["email"] or "").strip()
            email_key = email.lower()
            person_key = (name, institution)
            if (
                person_key in seen_people
                or email_key in seen_emails
                or domains.is_already_contacted(connection, name, institution, email)
            ):
                log(f"Skipping {name} ({institution}): already contacted.")
                continue
            draft_text = row["draft_text"]
            draft_subject = row["draft_subject"] if "draft_subject" in row.keys() else None
            if not draft_text or not str(draft_text).strip():
                status, _, _, error = draft_mod.draft_one_faculty(
                    (name, institution, row["recent_papers_json"] or "[]"),
                    profile=profile,
                    user_profile_json=user_profile_json,
                    model=model,
                    database=args.database,
                    table=table,
                )
                if status != "ok":
                    failed += 1
                    log(f"Draft failed for {name} ({institution}): {error}")
                    continue
                drafted += 1
                refreshed = connection.execute(
                    f"""
                    SELECT draft_text, draft_subject FROM {table}
                    WHERE name = ? AND institution = ?
                    """,
                    (name, institution),
                ).fetchone()
                draft_text = refreshed["draft_text"]
                draft_subject = refreshed["draft_subject"]
                log(f"Drafted {name} ({institution}).")

            result = dispatch_mod.send_one(
                connection,
                service,
                table=table,
                name=name,
                institution=institution,
                email=email,
                draft_text=draft_text,
                subject=draft_subject,
            )
            if result != "sent":
                log(f"Did not send {name}: {result}")
                continue
            sent += 1
            seen_people.add(person_key)
            seen_emails.add(email_key)
            log(f"[{sent}/{send_cap}] Sent to {name} ({email}).")
            if sent >= send_cap:
                break
    finally:
        connection.close()

    log(f"Finished: {drafted} drafted, {sent} sent, {failed} draft failures.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("Stopped.", flush=True)
        raise SystemExit(0)
