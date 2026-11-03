"""Send pending outreach drafts with Gmail and persist crash-safe send states."""

from __future__ import annotations

import argparse
import base64
import sqlite3
import sys
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

import domains


SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
DAILY_SEND_LIMIT = 500
PENDING_SEND_ORDER = """
        COALESCE(institution_rank, 9999) ASC,
        COALESCE(author_score, 0) DESC,
        name ASC,
        institution ASC
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dispatch pending outreach drafts.")
    domains.add_domain_argument(parser)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(__file__).with_name("outreach.db"),
        help="SQLite database path (default: outreach.db beside this script).",
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        default=Path(__file__).with_name("credentials.json"),
        help="OAuth client credentials downloaded from Google Cloud.",
    )
    parser.add_argument(
        "--token",
        type=Path,
        default=Path(__file__).with_name("token.json"),
        help="Cached OAuth token file.",
    )
    parser.add_argument(
        "--limit", type=int, help="Send at most this many emails in this run."
    )
    parser.add_argument(
        "--no-daily-cap",
        action="store_true",
        help="Ignore the UTC daily cap and only honor --limit (run-based sending).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show eligible records without sending or changing their status.",
    )
    return parser.parse_args()


def get_gmail_service(credentials_path: Path, token_path: Path):
    """Authenticate with Gmail using the OAuth installed-app flow."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as error:
        raise RuntimeError(
            "Install Gmail dependencies first: "
            "pip install google-api-python-client google-auth-httplib2 "
            "google-auth-oauthlib"
        ) from error

    try:
        from google.auth.exceptions import RefreshError
    except ImportError:
        RefreshError = Exception  # type: ignore[misc, assignment]

    credentials = None
    if token_path.exists():
        credentials = Credentials.from_authorized_user_file(token_path, SCOPES)
        if credentials and credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
                token_path.write_text(credentials.to_json(), encoding="utf-8")
            except RefreshError:
                # Testing OAuth apps expire refresh tokens after ~7 days.
                credentials = None
    if not credentials or not credentials.valid:
        if not credentials_path.exists():
            raise RuntimeError(
                f"OAuth client file not found: {credentials_path}. "
                "Create a Desktop OAuth client in Google Cloud and download it there."
            )
        print(
            "Gmail login needed. A browser window will open — "
            "sign in with the same account as EMAIL_USER.",
            flush=True,
        )
        flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
        credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
        token_path.write_text(credentials.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=credentials)


def ensure_schema(connection: sqlite3.Connection, domain_id: str) -> str:
    domains.ensure_pipeline_state(connection)
    return domains.ensure_faculty_table(connection, domain_id)


def make_message(
    recipient: str,
    professor_name: str,
    draft_text: str,
    subject: str | None = None,
) -> dict[str, str]:
    message = EmailMessage()
    message["To"] = recipient
    last = professor_name.split()[-1]
    message["Subject"] = (subject or "").strip() or f"Interest in your recent research, Professor {last}"
    message.set_content(draft_text)
    return {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")}


def utc_today() -> str:
    return datetime.now(UTC).date().isoformat()


def count_sent_today(connection: sqlite3.Connection, table: str) -> int:
    """Count durable send attempts already recorded for the current UTC day."""
    today = utc_today()
    return connection.execute(
        f"""
        SELECT COUNT(*)
        FROM {table}
        WHERE (
            status = 'sent'
            AND sent_at IS NOT NULL
            AND substr(sent_at, 1, 10) = ?
        ) OR (
            status = 'sending'
            AND send_attempted_at IS NOT NULL
            AND substr(send_attempted_at, 1, 10) = ?
        )
        """,
        (today, today),
    ).fetchone()[0]


def remaining_today(connection: sqlite3.Connection, table: str) -> int:
    return max(0, DAILY_SEND_LIMIT - count_sent_today(connection, table))


def mark_sending(
    connection: sqlite3.Connection, table: str, name: str, institution: str
) -> None:
    """Commit the pre-send state so crashes cannot cause a resend."""
    connection.execute(
        f"""
        UPDATE {table}
        SET status = 'sending', send_attempted_at = ?
        WHERE name = ? AND institution = ? AND status = 'pending'
        """,
        (datetime.now(UTC).isoformat(), name, institution),
    )
    connection.commit()


def send_one(
    connection: sqlite3.Connection,
    service,
    *,
    table: str,
    name: str,
    institution: str,
    email: str,
    draft_text: str,
    subject: str | None = None,
) -> str:
    """Send one pending draft. Returns 'sent', 'missing_email', or 'skipped'."""
    if not email or not str(email).strip():
        connection.execute(
            f"""
            UPDATE {table} SET status = 'missing_email'
            WHERE name = ? AND institution = ? AND status = 'pending'
            """,
            (name, institution),
        )
        connection.commit()
        return "missing_email"
    mark_sending(connection, table, name, institution)
    response = (
        service.users()
        .messages()
        .send(
            userId="me",
            body=make_message(email, name, draft_text, subject),
        )
        .execute()
    )
    connection.execute(
        f"""
        UPDATE {table}
        SET status = 'sent', sent_at = ?, gmail_message_id = ?
        WHERE name = ? AND institution = ? AND status = 'sending'
        """,
        (
            datetime.now(UTC).isoformat(),
            response.get("id"),
            name,
            institution,
        ),
    )
    connection.commit()
    return "sent"


def main() -> int:
    args = parse_args()
    if domains.maybe_list_domains(args):
        return 0
    connection = sqlite3.connect(args.database)
    domains.ensure_pipeline_state(connection)
    domain_id = domains.resolve_domain_choice(args.domain)
    table = ensure_schema(connection, domain_id)
    people, emails = domains.already_contacted(connection)
    rows = [
        row
        for row in connection.execute(
            f"""
            SELECT name, institution, email, draft_text, draft_subject
            FROM {table}
            WHERE draft_text IS NOT NULL
              AND TRIM(draft_text) != ''
              AND email IS NOT NULL
              AND TRIM(email) != ''
              AND status = 'pending'
            ORDER BY
            {PENDING_SEND_ORDER}
            """
        ).fetchall()
        if (row[0], row[1]) not in people
        and (row[2] or "").strip().lower() not in emails
    ]

    if args.dry_run:
        remaining = (
            len(rows)
            if args.no_daily_cap
            else remaining_today(connection, table)
        )
        shown = rows[: remaining if args.limit is None else min(args.limit, remaining)]
        for name, institution, email, _draft, _subject in shown:
            print(f"{name} ({institution}): {email or 'missing email'}")
        print(
            f"{len(rows)} pending records found in {table}; "
            f"{count_sent_today(connection, table)} already sent today; "
            f"{'no UTC cap' if args.no_daily_cap else str(remaining) + ' remaining under the cap'}; "
            "no messages sent."
        )
        connection.close()
        return 0

    try:
        if not args.no_daily_cap:
            remaining = remaining_today(connection, table)
            if remaining == 0:
                print(
                    f"Daily cap of {DAILY_SEND_LIMIT} emails already reached for {utc_today()} UTC. "
                    "Pending drafts are unchanged and will resume tomorrow."
                )
                return 0

        service = get_gmail_service(args.credentials, args.token)
        sent = missing_email = 0
        for name, institution, email, draft_text, draft_subject in rows:
            if not args.no_daily_cap and remaining_today(connection, table) == 0:
                print(
                    f"Daily cap of {DAILY_SEND_LIMIT} emails reached. "
                    "Stopping until tomorrow; remaining drafts stay pending."
                )
                break
            if args.limit is not None and sent >= args.limit:
                break
            result = send_one(
                connection,
                service,
                table=table,
                name=name,
                institution=institution,
                email=email,
                draft_text=draft_text,
                subject=draft_subject,
            )
            if result == "missing_email":
                missing_email += 1
                print(f"Skipped {name}: no recipient email.")
                continue
            sent += 1
            print(
                f"[{sent}] Sent email to {name} ({email}). "
                f"Today: {count_sent_today(connection, table)}/{DAILY_SEND_LIMIT}."
            )

            if not args.no_daily_cap and remaining_today(connection, table) == 0:
                print(
                    f"Daily cap of {DAILY_SEND_LIMIT} emails reached. "
                    "Remaining drafts stay pending until tomorrow."
                )
                break
            if args.limit is not None and sent >= args.limit:
                break
    finally:
        connection.close()

    print(f"Finished: {sent} sent, {missing_email} skipped for missing email.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
