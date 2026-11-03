"""Draft personalized faculty outreach emails for every row that has an email."""

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
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from google import genai
from google.genai import types

import domains


GEMINI_MODEL = "gemini-3.5-flash-lite"
GROQ_MODEL = "openai/gpt-oss-20b"
REQUEST_TIMEOUT_SECONDS = 30
# Free Gemini Flash-Lite is 15 RPM; 5s spacing stays under that cap.
MIN_REQUEST_INTERVAL_SECONDS = 5.0
POST_DRAFT_SLEEP_SECONDS = 1
MAX_API_RETRIES = 6
MAX_OUTPUT_TOKENS = 700
MAX_ABSTRACT_CHARS = 1500

_rate_lock = threading.Lock()
_last_request_at = 0.0
_fallback_lock = threading.Lock()
_gemini_exhausted = False
_groq_key_index = 0


SYSTEM_PROMPT_TEMPLATE = """You are writing a cold research-outreach email from the student in the profile to a Civil Engineering professor.

Student profile (JSON):
{user_profile_json}

Professor:
{professor_context}

In <analysis> tags, write 2 sentences: which of the student's areas and kinds of project work are closest to the professor's listed paper, and what realistic next step to propose. If no paper is listed, match against the professor's institution/field instead. Do not invent papers.

In <subject> tags, write exactly this form, filled in (no brackets left). The topic is 2-5 words naming the paper's core topic or method, not its full title, and nothing else:
Research interest in [Short Paper Topic]

In <email> tags, write the email by filling this template. Replace every [bracket]. Never leave placeholders. Keep all unbracketed wording exactly as written.

Dear Professor [Last Name],

Your paper on [Specific Paper Title / Core Method] caught my attention, particularly regarding [Specific Problem, Tradeoff, or Finding from the Paper].

{intro} My technical background is focused on [2-3 Broad Areas]. In my recent project work, I have focused on [What You Have Designed or Built, Summarized Across Projects]{github}.

Given your findings on [Core Challenge or Direction in the Paper], I am particularly keen to explore [Specific Technical Task or Experiment You Want to Add]. I would welcome the opportunity to support your ongoing work by implementing and validating these experiments.

Could we schedule a brief 10-minute introductory call to explore potential research synergy?

{signature}

Rules:
1. Keep it crisp: about 110-130 words before the signature, never more than 140. Word limits per bracket: Paper Title / Core Method up to 8 (a short name, not the full title with subtitle); Specific Problem up to 12; Core Challenge up to 10; Specific Technical Task up to 15.
2. The professor and their work are the focus. The second paragraph only shows the student is a real, serious student: modest, no numbers or metrics, no project names, nothing boastful.
3. Fill Last Name from the professor block. Keep the "I am ..." sentence exactly as written above.
4. [2-3 Broad Areas]: 2-3 short fields the profile clearly supports, choosing those closest to this paper (for example "structural analysis, geotechnical engineering, and water resources").
5. [What You Have Designed or Built]: up to 18 words, an overview across the student's projects and experience (not a single project), phrased as what they designed or built, optionally ending with "using" and 2-3 tools (for example "modelling reinforced-concrete frames and running hydraulic simulations, using Python, OpenSees, and MATLAB"). Describe the work as it appears in the profile; never reword it to sound like the professor's topic. Every tool must appear in the profile's technical_skills.
6. Leave out achievements, ranks, and honors.
7. Paper title, method, problem, and findings must come only from the professor block. If there is no paper, do not invent a title: open with their research area or institution instead of "Your paper on ...".
8. The experiment you offer must be a realistic next step on their listed work, matched to the student's skills. Do not claim you already did their paper.
9. Add no links beyond what the template already shows. Plain text only, no Markdown.
10. End with the signature exactly as shown above, with nothing after it.
11. Do not use "I hope this email finds you well" or a generic subject like "Quick question"."""

SIGN_OFF_LINE = re.compile(
    r"^[ \t]*(?:(?:best|warm|kind)[ \t]+)?regards,?[ \t]*$"
    r"|^[ \t]*(?:sincerely|thanks|thank you),?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate outreach drafts for every faculty row that has an email."
    )
    domains.add_domain_argument(parser)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(__file__).with_name("outreach.db"),
        help="SQLite database path (default: outreach.db beside this script).",
    )
    parser.add_argument(
        "--profile",
        type=Path,
        default=Path(__file__).with_name("profile.json"),
        help="Applicant profile JSON (default: profile.json beside this script).",
    )
    parser.add_argument("--limit", type=int, help="Draft at most this many emails.")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Personalize one sample draft, print it, and send it to your own inbox.",
    )
    parser.add_argument(
        "--test-to",
        help="Recipient for --test (default: the email in profile.json).",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("GEMINI_MODEL", GEMINI_MODEL),
        help=f"Gemini model ID (default: {GEMINI_MODEL}).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Concurrent Gemini requests (default: 1; free tier is 15 RPM).",
    )
    return parser.parse_args()


def load_gemini_key() -> str:
    key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("Set GEMINI_API_KEY in the local .env file.")
    return key


def load_groq_keys() -> list[str]:
    """Load every Groq key from .env. 1 key or 50 — all of them are used in rotation."""
    found: list[str] = []

    def take(value: str | None) -> None:
        if not value:
            return
        for part in re.split(r"[,;\n]+", value):
            key = part.strip().strip('"').strip("'")
            if key:
                found.append(key)

    take(os.environ.get("GROQ_API_KEYS"))
    take(os.environ.get("GROQ_API_KEY"))
    numbered: list[tuple[int, str]] = []
    for name, value in os.environ.items():
        match = re.fullmatch(r"GROQ_API_KEY_(\d+)", name)
        if match:
            numbered.append((int(match.group(1)), value or ""))
    for _, value in sorted(numbered):
        take(value)

    unique: list[str] = []
    seen: set[str] = set()
    for key in found:
        if key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


def groq_model() -> str:
    return (os.environ.get("GROQ_MODEL") or GROQ_MODEL).strip()


def is_capacity_error(error: BaseException) -> bool:
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "429",
            "resource_exhausted",
            "rate limit",
            "ratelimit",
            "quota",
            "too many requests",
        )
    )


def wait_for_rate_slot() -> None:
    global _last_request_at
    with _rate_lock:
        now = time.monotonic()
        wait = MIN_REQUEST_INTERVAL_SECONDS - (now - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def gemini_retry_seconds(error: Exception) -> float:
    match = re.search(r"retry in ([0-9.]+)\s*s", str(error), re.IGNORECASE)
    if match:
        return min(max(float(match.group(1)), 5.0), 90.0)
    return 35.0


def draft_with_gemini(system_prompt: str, model: str) -> str:
    """Call Gemini Flash-Lite, pausing on 429 using the API retry hint."""
    client = genai.Client(api_key=load_gemini_key())
    config = types.GenerateContentConfig(
        temperature=0.7,
        max_output_tokens=MAX_OUTPUT_TOKENS,
    )
    retries = 2 if load_groq_keys() else MAX_API_RETRIES
    hit_limit = False
    last_error: BaseException | None = None
    for attempt in range(retries):
        try:
            wait_for_rate_slot()
            response = client.models.generate_content(
                model=model,
                contents=system_prompt + "\n\nGenerate the requested tagged response.",
                config=config,
            )
            text = (getattr(response, "text", None) or "").strip()
            if not text:
                raise RuntimeError("Gemini returned an empty draft.")
            return text
        except Exception as error:
            last_error = error
            message = str(error)
            if "429" in message or "RESOURCE_EXHAUSTED" in message:
                hit_limit = True
                delay = gemini_retry_seconds(error)
                print(
                    f"Gemini 429; waiting {delay:.0f}s "
                    f"(attempt {attempt + 1}/{retries}).",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(delay)
                continue
            if "503" in message or "UNAVAILABLE" in message:
                delay = min(20 * (attempt + 1), 90)
                print(
                    f"Gemini 503; waiting {delay}s "
                    f"(attempt {attempt + 1}/{retries}).",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(delay)
                continue
            if attempt == retries - 1:
                raise RuntimeError(f"Gemini failed: {error}") from error
            print(f"Gemini error; retrying: {error}", file=sys.stderr, flush=True)
            time.sleep(5)
    if hit_limit:
        raise RuntimeError("Gemini 429 RESOURCE_EXHAUSTED after retries.")
    raise RuntimeError(f"Gemini did not return a draft after retries: {last_error}")


def draft_with_groq(system_prompt: str) -> str:
    """Draft with Groq, rotating through GROQ_API_KEYS when a key is limited."""
    try:
        from groq import Groq
    except ImportError as error:
        raise RuntimeError("Install groq first: pip install groq") from error

    keys = load_groq_keys()
    if not keys:
        raise RuntimeError("Set GROQ_API_KEYS in the local .env file.")

    global _groq_key_index
    last_error: BaseException | None = None
    for _attempt in range(len(keys) * 2):
        with _fallback_lock:
            idx = _groq_key_index % len(keys)
            key = keys[idx]
        try:
            client = Groq(api_key=key)
            completion = client.chat.completions.create(
                model=groq_model(),
                messages=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": "Generate the requested tagged response.",
                    },
                ],
                temperature=0.7,
                max_tokens=MAX_OUTPUT_TOKENS,
            )
            text = ((completion.choices[0].message.content) or "").strip()
            if not text:
                raise RuntimeError("Groq returned an empty draft.")
            with _fallback_lock:
                _groq_key_index += 1
            return text
        except Exception as error:
            last_error = error
            if is_capacity_error(error) or "401" in str(error) or "403" in str(error):
                with _fallback_lock:
                    _groq_key_index += 1
                    nxt = (_groq_key_index % len(keys)) + 1
                print(
                    f"Groq key {idx + 1}/{len(keys)} unavailable; "
                    f"switching to key {nxt}.",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            raise RuntimeError(f"Groq failed: {error}") from error
    raise RuntimeError(f"All Groq API keys failed: {last_error}")


def draft_llm(system_prompt: str, gemini_model: str) -> str:
    """Use Gemini first; if it hits a rate limit, rotate through Groq keys."""
    global _gemini_exhausted
    groq_keys = load_groq_keys()
    with _fallback_lock:
        gemini_done = _gemini_exhausted
    if not gemini_done:
        try:
            return draft_with_gemini(system_prompt, gemini_model)
        except RuntimeError as error:
            if groq_keys and is_capacity_error(error):
                with _fallback_lock:
                    _gemini_exhausted = True
                print(
                    f"Gemini hit its limit; switching to {len(groq_keys)} Groq key(s).",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                raise
    if not groq_keys:
        raise RuntimeError("Gemini hit its limit and GROQ_API_KEYS is not set.")
    return draft_with_groq(system_prompt)


def add_draft_column(connection: sqlite3.Connection, domain_id: str) -> str:
    domains.ensure_pipeline_state(connection)
    return domains.ensure_faculty_table(connection, domain_id)


def parse_papers(recent_papers_json: str) -> list[dict]:
    try:
        papers = json.loads(recent_papers_json or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(papers, list):
        return []
    return [paper for paper in papers if isinstance(paper, dict)]


def latest_abstract(recent_papers_json: str) -> str | None:
    for paper in parse_papers(recent_papers_json):
        abstract = paper.get("abstract")
        if not isinstance(abstract, str):
            continue
        abstract = abstract.strip()
        if not abstract:
            continue
        if len(abstract) > MAX_ABSTRACT_CHARS:
            abstract = abstract[:MAX_ABSTRACT_CHARS].rsplit(" ", 1)[0] + "..."
        return abstract
    return None


def professor_research_context(name: str, institution: str, recent_papers_json: str) -> str:
    """Build prompt context from abstract, paper title, or institution alone."""
    lines = [f"Name: {name}", f"Institution: {institution}"]
    papers = parse_papers(recent_papers_json)
    if not papers:
        lines.append(
            "No recent paper metadata. Do not invent publications; "
            "ground the email in the student's work and this institution."
        )
        return "\n".join(lines)

    paper = papers[0]
    title = paper.get("title")
    year = paper.get("publication_date")
    if isinstance(title, str) and title.strip():
        titled = title.strip()
        if isinstance(year, str) and year.strip():
            titled = f"{titled} ({year.strip()})"
        lines.append(f"Recent paper title: {titled}")
    abstract = latest_abstract(recent_papers_json)
    if abstract:
        lines.append(f"Abstract: {abstract}")
    else:
        lines.append(
            "No abstract is available. Use the paper title if present. "
            "Do not invent additional paper content."
        )
    return "\n".join(lines)


def load_profile(profile_path: Path) -> dict:
    """Load the complete user profile as a JSON object."""
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not read profile {profile_path}: {error}") from error

    if not isinstance(profile, dict):
        raise RuntimeError("profile.json must contain a JSON object.")
    return profile


def short_institution(institution: str) -> str:
    """'Indian Institute of Technology, Gandhinagar' -> 'IIT Gandhinagar'."""
    institution = (institution or "").strip()
    match = re.fullmatch(r"Indian Institute of Technology[,\s]+(.+)", institution, re.IGNORECASE)
    return f"IIT {match.group(1).strip()}" if match else institution


def split_degree(degree: str) -> tuple[str, str]:
    """'B.Tech. in Civil Engineering' -> ('B.Tech.', 'Civil Engineering')."""
    degree = (degree or "").strip()
    match = re.fullmatch(r"(.+?)\s+in\s+(.+)", degree)
    return (match.group(1), match.group(2)) if match else (degree, "")


def intro_sentence(profile: dict) -> str:
    """'I am Name, a student in Civil Engineering at IIT Gandhinagar with a CPI of 9.14/10.0.'"""
    info = profile.get("personal_info") or {}
    name = (info.get("name") or "").strip()
    institution = short_institution(info.get("institution") or "")
    cpi = re.sub(r"\s*/\s*", "/", (info.get("cpi") or "").strip())

    degree, branch = split_degree(info.get("degree") or "")
    student = f"student in {branch}" if branch else f"{degree} student".strip()
    article = "an" if student[:1].lower() in "aeiou" else "a"
    sentence = f"I am {name}, {article} {student}"
    if institution:
        sentence += f" at {institution}"
    if cpi:
        sentence += f" with a CPI of {cpi}"
    return sentence + "."


def signature_block(profile: dict) -> str:
    """Best regards / name / branch / institution, one per line."""
    info = profile.get("personal_info") or {}
    degree, branch = split_degree(info.get("degree") or "")
    lines = [
        "Best regards,",
        info.get("name"),
        branch or degree,
        short_institution(info.get("institution") or ""),
    ]
    return "\n".join(line.strip() for line in lines if isinstance(line, str) and line.strip())


def apply_signature(draft: str, profile: dict) -> str:
    """Replace whatever sign-off the model wrote with the fixed profile signature."""
    match = SIGN_OFF_LINE.search(draft)
    body = draft[: match.start()] if match else draft
    return f"{body.rstrip()}\n\n{signature_block(profile)}"


def compact_profile_for_prompt(profile: dict) -> str:
    """Student facts for the draft prompt: identity, CPI, projects, experience, skills, links."""
    info = profile.get("personal_info") or {}
    projects = []
    for project in (profile.get("projects") or [])[:3]:
        if not isinstance(project, dict):
            continue
        points = project.get("key_technical_points") or []
        projects.append(
            {
                "name": project.get("name"),
                "summary": project.get("summary"),
                "tech": points[:2],
            }
        )
    links = {
        key: value
        for key, value in (info.get("links") or {}).items()
        if isinstance(value, str) and value.strip()
    }
    compact = {
        "name": info.get("name"),
        "institution": short_institution(info.get("institution") or ""),
        "degree": info.get("degree"),
        "year": info.get("year"),
        "cpi": info.get("cpi"),
        "links": links,
        "projects": projects,
        "experience": profile.get("experience"),
        "technical_skills": profile.get("technical_skills"),
    }
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))


def profile_links(profile: dict) -> dict[str, str]:
    links = (profile.get("personal_info") or {}).get("links") or {}
    return {key: value for key, value in links.items() if isinstance(value, str) and value}


def personalize_draft(draft: str, professor_name: str, profile: dict) -> str:
    """Fill last-name placeholders and replace truncated profile URLs."""
    last_name = professor_name.split()[-1]
    draft = draft.replace("[Last Name]", last_name)
    draft = draft.replace("[Professor Last Name]", last_name)
    draft = re.sub(
        r"\[(?:Professor(?:['’]s)?\s+)?Last Name\]",
        last_name,
        draft,
        flags=re.IGNORECASE,
    )

    # Paper titles arrive HTML-escaped ("&amp;"), and emails go out as plain text,
    # so unescape entities and unwrap Markdown links to the bare URL.
    draft = html.unescape(draft)
    draft = re.sub(r"\[[^\]\n]*\]\((https?://[^)\s]+)\)", r"\1", draft)

    links = profile_links(profile)
    if github := links.get("github"):
        draft = re.sub(r"https://github\.com/(?![\w.-])", github, draft)
    if linkedin := links.get("linkedin"):
        draft = re.sub(
            r"https?://(?:www\.)?linkedin\.com/(?:in/)?(?=\s|$|\|)",
            linkedin,
            draft,
            flags=re.IGNORECASE,
        )
    if codeforces := links.get("codeforces"):
        draft = re.sub(
            r"https://codeforces\.com/(?:profile/)?(?=\s|$|\|)",
            codeforces,
            draft,
        )
    draft = re.sub(r"[ \t]*\|[ \t]*", " | ", draft)
    draft = re.sub(r"(?:\| )+", "| ", draft)
    draft = re.sub(r"^\s*\|\s*|\s*\|\s*$", "", draft, flags=re.MULTILINE)
    draft = re.sub(r"[ \t]{2,}", " ", draft)
    draft = re.sub(r" *\n", "\n", draft)
    return draft.strip()


def rewrite_saved_drafts(connection: sqlite3.Connection, table: str, profile: dict) -> int:
    """Fix unsent drafts that still have placeholders or stub links."""
    columns = {
        row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    status_filter = "AND status = 'pending'" if "status" in columns else ""
    rows = connection.execute(
        f"""
        SELECT name, institution, draft_text
        FROM {table}
        WHERE draft_text IS NOT NULL AND TRIM(draft_text) != ''
        {status_filter}
        """
    ).fetchall()
    updated = 0
    for name, institution, draft_text in rows:
        rewritten = personalize_draft(draft_text, name, profile)
        if rewritten != draft_text:
            connection.execute(
                f"UPDATE {table} SET draft_text = ? WHERE name = ? AND institution = ?",
                (rewritten, name, institution),
            )
            updated += 1
    if updated:
        connection.commit()
    return updated


def load_dispatch_module():
    path = Path(__file__).with_name("4_dispatch.py")
    spec = spec_from_file_location("outreach_dispatch", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def send_test_email(
    draft: str,
    professor_name: str,
    recipient: str,
    subject: str | None = None,
) -> None:
    import base64
    from email.message import EmailMessage

    dispatch = load_dispatch_module()
    credentials = Path(__file__).with_name("credentials.json")
    token = Path(__file__).with_name("token.json")
    service = dispatch.get_gmail_service(credentials, token)

    message = EmailMessage()
    message["To"] = recipient
    message["Subject"] = subject or f"Outreach draft for Professor {professor_name.split()[-1]}"
    message.set_content(
        f"[TEST MESSAGE — this was not sent to the professor]\n\n{draft}"
    )
    body = {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")}
    service.users().messages().send(userId="me", body=body).execute()


def run_test(connection: sqlite3.Connection, profile: dict, args: argparse.Namespace) -> int:
    """Print one personalized draft and send it to the applicant's inbox."""
    recipient = args.test_to or (profile.get("personal_info") or {}).get("email")
    if not recipient:
        connection.close()
        raise RuntimeError("No test recipient: set personal_info.email or pass --test-to.")

    add_draft_column(connection, args.domain)
    row = connection.execute(
        f"""
        SELECT name, institution, email, draft_text, draft_subject, recent_papers_json
        FROM {args.table}
        WHERE email IS NOT NULL AND TRIM(email) != ''
          AND draft_text IS NOT NULL AND TRIM(draft_text) != ''
        ORDER BY name, institution
        LIMIT 1
        """
    ).fetchone()

    if row is None:
        connection.close()
        raise RuntimeError(
            "No saved draft with an email address was found. "
            "Run this script without --test first to generate drafts."
        )

    name, institution, faculty_email, draft_text, draft_subject, _papers = row
    draft = personalize_draft(draft_text, name, profile)
    subject = personalize_draft(draft_subject or "", name, profile)
    connection.execute(
        f"""
        UPDATE {args.table} SET draft_text = ?, draft_subject = ?
        WHERE name = ? AND institution = ?
        """,
        (draft, subject or draft_subject, name, institution),
    )
    connection.commit()
    connection.close()

    print("=" * 72)
    print(f"Sample draft for {name} ({institution})")
    print(f"Subject: {subject or '(none)'}")
    print(f"Professor email (not used for this test): {faculty_email}")
    print(f"Test recipient: {recipient}")
    print("=" * 72)
    print(draft)
    print("=" * 72)

    try:
        send_test_email(draft, name, recipient, subject=subject or None)
    except RuntimeError as error:
        print(f"Draft printed above, but Gmail send failed: {error}", file=sys.stderr)
        return 1

    print(f"Sent test email to {recipient}.")
    return 0


def build_system_prompt(user_profile_json: str, professor_context: str, profile: dict) -> str:
    github = profile_links(profile).get("github")
    return SYSTEM_PROMPT_TEMPLATE.format(
        user_profile_json=user_profile_json,
        professor_context=professor_context,
        intro=intro_sentence(profile),
        github=f" ({github})" if github else "",
        signature=signature_block(profile),
    )


def extract_subject_and_email(response_text: str) -> tuple[str, str]:
    """Return the generated subject and email body from tagged LLM output."""
    subject_match = re.search(
        r"<subject>\s*(.*?)\s*</subject>",
        response_text,
        re.IGNORECASE | re.DOTALL,
    )
    email_match = re.search(
        r"<email>\s*(.*?)\s*</email>",
        response_text,
        re.IGNORECASE | re.DOTALL,
    )
    if not subject_match:
        raise RuntimeError("LLM response did not contain a <subject>...</subject> block.")
    if not email_match:
        raise RuntimeError("LLM response did not contain an <email>...</email> block.")
    subject = re.sub(r"\s+", " ", subject_match.group(1)).strip()
    email = email_match.group(1).strip()
    if not subject:
        raise RuntimeError("LLM response contained an empty <subject> block.")
    if not email:
        raise RuntimeError("LLM response contained an empty <email> block.")
    return subject, email


def save_draft(
    database: Path,
    name: str,
    institution: str,
    subject: str,
    draft: str,
    table: str,
    write_lock: threading.Lock | None = None,
) -> None:
    lock = write_lock or threading.Lock()
    with lock:
        connection = sqlite3.connect(database, timeout=60)
        try:
            connection.execute(
                f"""
                UPDATE {table}
                SET draft_subject = ?, draft_text = ?
                WHERE name = ? AND institution = ?
                """,
                (subject, draft, name, institution),
            )
            connection.commit()
        finally:
            connection.close()


def draft_one_faculty(
    row: tuple[str, str, str],
    *,
    profile: dict,
    user_profile_json: str,
    model: str,
    database: Path,
    table: str,
    write_lock: threading.Lock | None = None,
) -> tuple[str, str, str, BaseException | None]:
    name, institution, papers_json = row
    try:
        system_prompt = build_system_prompt(
            user_profile_json,
            professor_research_context(name, institution, papers_json),
            profile,
        )
        response_text = draft_llm(system_prompt, model)
        subject, email_body = extract_subject_and_email(response_text)
        draft = apply_signature(personalize_draft(email_body, name, profile), profile)
        subject = personalize_draft(subject, name, profile)
        save_draft(
            database,
            name,
            institution,
            subject,
            draft,
            table,
            write_lock,
        )
    except RuntimeError as error:
        time.sleep(POST_DRAFT_SLEEP_SECONDS)
        return "failed", name, institution, error
    time.sleep(POST_DRAFT_SLEEP_SECONDS)
    return "ok", name, institution, None


def main() -> int:
    args = parse_args()
    if domains.maybe_list_domains(args):
        return 0
    profile = load_profile(args.profile)
    connection = sqlite3.connect(args.database)
    domains.ensure_pipeline_state(connection)
    domain_id = domains.resolve_domain_choice(args.domain)
    args.domain = domain_id
    table = add_draft_column(connection, domain_id)
    args.table = table
    user_profile_json = compact_profile_for_prompt(profile)
    rewritten = rewrite_saved_drafts(connection, table, profile)
    if rewritten:
        print(f"Updated {rewritten} existing drafts (last name and profile links).")

    if args.test:
        return run_test(connection, profile, args)

    print(
        f"Domain {domain_id} ({domains.domain_label(domain_id)}); table {table}.",
        flush=True,
    )
    print(f"Using Gemini model {args.model}.", flush=True)
    groq_keys = load_groq_keys()
    if groq_keys:
        print(
            f"Groq fallback ready with {len(groq_keys)} key(s) "
            f"(model {groq_model()}).",
            flush=True,
        )
    connection.execute("PRAGMA journal_mode=WAL")
    people, emails = domains.already_contacted(connection)
    rows = [
        (name, institution, papers)
        for name, institution, papers, email in connection.execute(
            f"""
            SELECT name, institution, recent_papers_json, email
            FROM {table}
            WHERE email IS NOT NULL
              AND TRIM(email) != ''
              AND (draft_text IS NULL OR TRIM(draft_text) = '')
            ORDER BY
            COALESCE(institution_rank, 9999) ASC,
            COALESCE(author_score, 0) DESC,
            name ASC,
            institution ASC
            """
        ).fetchall()
        if (name, institution) not in people
        and (email or "").strip().lower() not in emails
    ]
    if args.limit is not None:
        rows = rows[: args.limit]
    with_abstract = sum(1 for _name, _inst, papers in rows if latest_abstract(papers))
    workers = max(1, args.workers)
    print(
        f"{len(rows)} undrafted rows with email will all be drafted; "
        f"{with_abstract} have an abstract. Workers: {workers}.",
        flush=True,
    )
    drafted = skipped = failed = 0
    consecutive_capacity_failures = 0
    write_lock = threading.Lock()
    print_lock = threading.Lock()
    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(
                    draft_one_faculty,
                    row,
                    profile=profile,
                    user_profile_json=user_profile_json,
                    model=args.model,
                    database=args.database,
                    table=table,
                    write_lock=write_lock,
                )
                for row in rows
            ]
            for future in as_completed(futures):
                status, name, institution, error = future.result()
                if status == "skipped":
                    skipped += 1
                    continue
                if status == "failed":
                    failed += 1
                    with print_lock:
                        print(
                            f"Skipped {name} ({institution}): {error}",
                            file=sys.stderr,
                            flush=True,
                        )
                    if error and "429" in str(error):
                        consecutive_capacity_failures += 1
                        if consecutive_capacity_failures >= 5:
                            with print_lock:
                                print(
                                    "Gemini stayed rate-limited. "
                                    "Saved drafts are kept; rerun to resume.",
                                    file=sys.stderr,
                                    flush=True,
                                )
                            for pending in futures:
                                pending.cancel()
                            break
                    continue

                consecutive_capacity_failures = 0
                drafted += 1
                with print_lock:
                    print(f"[{drafted}] Drafted email for {name} ({institution})", flush=True)
    finally:
        connection.close()

    print(
        f"Finished: {drafted} drafted, {skipped} skipped, {failed} failed."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
