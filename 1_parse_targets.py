"""Build a civil-engineering faculty list from QS university rank + OpenAlex authors."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path
import requests

import domains


OPENALEX_ROOT = "https://api.openalex.org"
DOWNLOAD_TIMEOUT_SECONDS = 30
REQUEST_DELAY_SECONDS = 0.2
MIN_WORKS_COUNT = 8
DEFAULT_PER_INSTITUTION = 25


def mailto() -> str:
    return (os.environ.get("EMAIL_USER") or "anonymous@example.com").strip()


def user_agent() -> str:
    return f"civil-faculty-outreach/1.0 (mailto:{mailto()})"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a JSON list of civil/structural faculty from the QS ranking "
            "(allowed countries only) and OpenAlex authors at those universities."
        )
    )
    domains.add_domain_argument(parser)
    parser.add_argument(
        "--ranking",
        type=Path,
        help="QS ranking JSON (default: data/qs_civil_ranking.json).",
    )
    parser.add_argument(
        "--from-year",
        type=int,
        default=date.today().year - 10,
        help="Kept for pipeline compatibility; OpenAlex uses cited_by_count.",
    )
    parser.add_argument(
        "--to-year",
        type=int,
        default=date.today().year,
        help="Kept for pipeline compatibility; OpenAlex uses cited_by_count.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=80,
        help="Number of allowed-country QS universities to draw faculty from (default: 80).",
    )
    parser.add_argument(
        "--max-faculty",
        type=int,
        default=500,
        help="Maximum unique faculty to write (default: 500).",
    )
    parser.add_argument(
        "--max-per-institution",
        type=int,
        default=DEFAULT_PER_INSTITUTION,
        help="OpenAlex authors to consider per university (default: 25).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Destination JSON path (default: targets_civil.json).",
    )
    parser.add_argument(
        "--list-schools",
        action="store_true",
        help="Print allowed-country QS schools (after --top) and exit. No OpenAlex calls.",
    )
    args = parser.parse_args()
    if domains.maybe_list_domains(args):
        raise SystemExit(0)
    args.domain = domains.resolve_domain_choice(args.domain)
    root = Path(__file__).resolve().parent
    if args.ranking is None:
        args.ranking = domains.ranking_path(root)
    if args.output is None:
        args.output = domains.targets_path(root, args.domain)
    if args.top < 1:
        parser.error("--top must be positive")
    if args.max_faculty < 1:
        parser.error("--max-faculty must be positive")
    if args.max_per_institution < 1:
        parser.error("--max-per-institution must be positive")
    return args


def load_ranking(path: Path) -> list[dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not read ranking file {path}: {error}") from error
    rows = payload.get("institutions") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"{path} has no institutions list.")
    cleaned = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        university = str(row.get("university") or "").strip()
        country_code = str(row.get("country_code") or "").strip().upper()
        if not university:
            continue
        try:
            rank = int(row.get("rank") or 9999)
        except (TypeError, ValueError):
            rank = 9999
        cleaned.append(
            {
                "rank": rank,
                "rank_display": str(row.get("rank_display") or rank),
                "university": university,
                "country": str(row.get("country") or "").strip(),
                "country_code": country_code,
            }
        )
    cleaned.sort(key=lambda item: (item["rank"], item["university"]))
    return cleaned


def allowed_universities(rows: list[dict], top: int) -> list[dict]:
    allowed = [row for row in rows if domains.country_allowed(row["country_code"])]
    return allowed[:top]


def openalex_get(path: str, params: dict[str, str]) -> dict:
    query = dict(params)
    query["mailto"] = mailto()
    response = requests.get(
        f"{OPENALEX_ROOT}{path}",
        params=query,
        headers={"User-Agent": user_agent(), "Accept": "application/json"},
        timeout=DOWNLOAD_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    time.sleep(REQUEST_DELAY_SECONDS)
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"OpenAlex {path} returned a non-object payload.")
    return payload


def short_id(value: str | None) -> str | None:
    if not value:
        return None
    return str(value).rsplit("/", 1)[-1] or None


def find_institution(university: str, country_code: str) -> dict | None:
    """Resolve a QS university name to an OpenAlex institution record."""
    try:
        payload = openalex_get(
            "/institutions",
            {"search": university, "per-page": "8"},
        )
    except (requests.RequestException, OSError, ValueError) as error:
        print(f"  OpenAlex institution search failed for {university}: {error}", flush=True)
        return None
    results = payload.get("results") or []
    if not isinstance(results, list) or not results:
        print(f"  No OpenAlex institution for {university}.", flush=True)
        return None

    def score(item: dict) -> tuple[int, int, int]:
        display = str(item.get("display_name") or "")
        country = str(item.get("country_code") or "").upper()
        name_hit = 2 if _norm_name(display) == _norm_name(university) else (
            1 if _norm_name(university) in _norm_name(display)
            or _norm_name(display) in _norm_name(university)
            else 0
        )
        country_hit = 1 if country and country == country_code else 0
        works = int(item.get("works_count") or 0)
        return (name_hit, country_hit, works)

    ranked = [item for item in results if isinstance(item, dict)]
    if not ranked:
        return None
    ranked.sort(key=score, reverse=True)
    chosen = ranked[0]
    chosen_country = str(chosen.get("country_code") or "").upper()
    if country_code and chosen_country and chosen_country != country_code:
        country_matches = [item for item in ranked if str(item.get("country_code") or "").upper() == country_code]
        if country_matches:
            chosen = country_matches[0]
    return chosen


def _norm_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _subfield_id(topic: object) -> str | None:
    if not isinstance(topic, dict):
        return None
    subfield = topic.get("subfield") or {}
    if not isinstance(subfield, dict):
        return None
    return short_id(str(subfield.get("id") or ""))


def _topic_count(topic: dict) -> int:
    try:
        return int(topic.get("count") or 0)
    except (TypeError, ValueError):
        return 0


def _topic_blob(topic: dict) -> str:
    subfield = topic.get("subfield") if isinstance(topic.get("subfield"), dict) else {}
    return " ".join(
        str(part or "")
        for part in (topic.get("display_name"), subfield.get("display_name"))
    ).lower()


# OpenAlex files FEM / computational-structural people under these neighbors.
_RELATED_PRIMARY_SUBFIELDS = frozenset({"2206", "2305", "2211"})


def author_is_civil(author: dict, domain_id: str) -> bool:
    """Keep authors whose OpenAlex topics are mainly civil, not a weak side tag."""
    civil_subfields = domains.openalex_subfields(domain_id)
    keywords = domains.domain_keywords(domain_id)
    topics = [topic for topic in (author.get("topics") or []) if isinstance(topic, dict)]
    if not topics:
        return False

    def keyword_hit(topic: dict) -> bool:
        blob = _topic_blob(topic)
        return any(keyword in blob for keyword in keywords)

    top_sub = _subfield_id(topics[0])
    if top_sub in civil_subfields:
        return True

    top5 = topics[:5]
    has_civil_topic = any(
        _subfield_id(topic) in civil_subfields or keyword_hit(topic) for topic in top5
    )
    if not has_civil_topic:
        return False
    if top_sub in _RELATED_PRIMARY_SUBFIELDS:
        return True
    if keyword_hit(topics[0]) or (len(topics) > 1 and keyword_hit(topics[1])):
        return True
    civil_count = sum(
        _topic_count(topic) for topic in top5 if _subfield_id(topic) in civil_subfields
    )
    total = sum(_topic_count(topic) for topic in top5) or 1
    return civil_count / total >= 0.40


def homepage_for_author(author: dict, institution_homepage: str | None) -> str:
    """OpenAlex rarely has faculty homepages; ORCID / Wikipedia / school site are fallbacks."""
    ids = author.get("ids") if isinstance(author.get("ids"), dict) else {}
    orcid = ids.get("orcid") if isinstance(ids, dict) else None
    wikipedia = ids.get("wikipedia") if isinstance(ids, dict) else None
    if isinstance(orcid, str) and orcid.strip():
        return orcid.strip()
    if isinstance(wikipedia, str) and wikipedia.strip():
        return wikipedia.strip()
    if institution_homepage:
        return institution_homepage
    openalex_id = author.get("id")
    if isinstance(openalex_id, str) and openalex_id.strip():
        return openalex_id.strip()
    return ""


def authors_at_institution(
    institution_id: str,
    domain_id: str,
    limit: int,
) -> list[dict]:
    subfields = "|".join(sorted(domains.openalex_subfields(domain_id)))
    collected: list[dict] = []
    cursor = "*"
    pages = 0
    while len(collected) < limit and cursor and pages < 8:
        try:
            payload = openalex_get(
                "/authors",
                {
                    "filter": (
                        f"last_known_institutions.id:{institution_id},"
                        f"topics.subfield.id:{subfields}"
                    ),
                    "sort": "cited_by_count:desc",
                    "per-page": "50",
                    "cursor": cursor,
                    "select": (
                        "id,display_name,cited_by_count,works_count,ids,"
                        "last_known_institutions,topics"
                    ),
                },
            )
        except (requests.RequestException, OSError, ValueError) as error:
            print(f"  OpenAlex authors failed for {institution_id}: {error}", flush=True)
            break
        pages += 1
        results = payload.get("results") or []
        if not isinstance(results, list) or not results:
            break
        for author in results:
            if not isinstance(author, dict):
                continue
            if int(author.get("works_count") or 0) < MIN_WORKS_COUNT:
                continue
            if not author_is_civil(author, domain_id):
                continue
            collected.append(author)
            if len(collected) >= limit:
                break
        cursor = (payload.get("meta") or {}).get("next_cursor") or ""
        if not results:
            break
    return collected[:limit]


def print_schools(schools: list[dict]) -> None:
    print(
        f"{len(schools)} allowed-country QS Civil & Structural schools "
        "(Europe, US, Canada, Japan, Singapore, Australia, New Zealand):"
    )
    for school in schools:
        print(
            f"  {school['rank_display']:>7}  {school['university']} "
            f"({school['country_code']})"
        )


def main() -> int:
    args = parse_args()
    ranking = load_ranking(args.ranking)
    schools = allowed_universities(ranking, args.top)
    if not schools:
        raise RuntimeError(
            f"No allowed-country universities in {args.ranking}. "
            "Check country_code values against domains.ALLOWED_COUNTRY_CODES."
        )

    print(
        f"QS Civil 2025: {len(ranking)} bundled institutions; "
        f"{sum(1 for row in ranking if domains.country_allowed(row['country_code']))} "
        f"in allowed countries; using top {len(schools)}.",
        flush=True,
    )
    print_schools(schools)
    if args.list_schools:
        return 0

    targets: list[dict] = []
    seen: set[tuple[str, str]] = set()
    seen_openalex: set[str] = set()

    for school in schools:
        if len(targets) >= args.max_faculty:
            break
        print(f"Resolving {school['university']} (QS {school['rank_display']})...", flush=True)
        institution = find_institution(school["university"], school["country_code"])
        if not institution or not institution.get("id"):
            continue
        inst_id = short_id(str(institution["id"]))
        if not inst_id:
            continue
        inst_name = str(institution.get("display_name") or school["university"])
        inst_home = (
            str(institution.get("homepage_url") or "").strip()
            or f"https://openalex.org/{inst_id}"
        )
        authors = authors_at_institution(
            inst_id, args.domain, args.max_per_institution
        )
        print(f"  {len(authors)} civil authors from OpenAlex.", flush=True)
        remaining = args.max_faculty - len(targets)
        for author in authors[:remaining]:
            name = re.sub(r"\s+", " ", str(author.get("display_name") or "")).strip()
            if not name:
                continue
            openalex_id = short_id(str(author.get("id") or ""))
            if openalex_id and openalex_id in seen_openalex:
                continue
            key = (name, inst_name)
            if key in seen:
                continue
            ids = author.get("ids") if isinstance(author.get("ids"), dict) else {}
            orcid = ids.get("orcid") if isinstance(ids, dict) else None
            homepage = homepage_for_author(author, inst_home)
            if not homepage:
                continue
            targets.append(
                {
                    "name": name,
                    "institution": inst_name,
                    "homepage": homepage,
                    "openalex_id": openalex_id,
                    "orcid": orcid,
                    "institution_rank": school["rank"],
                    "institution_score": school["rank"],
                    "author_score": int(author.get("cited_by_count") or 0),
                    "works_count": int(author.get("works_count") or 0),
                    "country": school["country"],
                    "country_code": school["country_code"],
                    "qs_university": school["university"],
                    "qs_rank_display": school["rank_display"],
                }
            )
            seen.add(key)
            if openalex_id:
                seen_openalex.add(openalex_id)

    targets.sort(
        key=lambda person: (
            int(person["institution_rank"]),
            -float(person["author_score"]),
            person["name"],
        )
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(targets, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    used_schools = {person["institution"] for person in targets}
    print(
        f"Domain {args.domain} ({domains.domain_label(args.domain)}). "
        f"Found {len(targets)} unique faculty (cap {args.max_faculty}) "
        f"across {len(used_schools)} institutions. Wrote {args.output}"
    )
    print("Send priority (1 = first). Ranked by QS Civil university order, then citations:")
    printed: set[str] = set()
    for school in schools:
        if school["university"] in printed:
            continue
        printed.add(school["university"])
        print(f"  {school['rank_display']:>7}. {school['university']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
