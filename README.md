# Civil faculty outreach

Hey — this is the script I use to email civil / structural engineering professors from my Gmail.

You fill in who you are. It finds faculty at QS-ranked schools, picks a recent paper, writes a short personal email, and sends it. One command after setup:

```powershell
python run_outreach.py
```

That run scrapes about **500** people, looks up emails, and sends **every address it finds**. People you already emailed are skipped.

Civil is **one field**. Structures, geotech, transportation, water, construction — all in the same list. No CS-style “pick a domain” prompt.

Schools are only in **Europe, the US, Canada, Japan, Singapore, Australia, and New Zealand**.

---

## What you need

| Thing | Why | Where |
|---|---|---|
| Python 3.10+ | Runs the scripts | [python.org/downloads](https://www.python.org/downloads/) |
| `GEMINI_API_KEY` | Writes each email | [Google AI Studio](https://aistudio.google.com/apikey) |
| `GROQ_API_KEYS` (optional) | Backup if Gemini rate-limits you | [Groq console](https://console.groq.com/keys) |
| `credentials.json` | Lets the script send as you | Google Cloud Desktop OAuth (step 3 below) |
| `EMAIL_USER` | Your Gmail address | Same Gmail you sign into for send |
| `profile.json` | Your name, school, CPI, projects | Copy `profile.example.json` and edit |

QS ranking is already in the repo (`data/qs_civil_ranking.json`). Faculty lists and papers come from [OpenAlex](https://openalex.org/) and Crossref — free, no extra keys.

**Never commit** `.env`, `credentials.json`, `token.json`, `profile.json`, or `*.db`. They are gitignored on purpose.

If you already set up the CS outreach repo, you can copy `.env`, `credentials.json`, and `token.json` from there. Still rewrite `profile.json` so it reads as civil, not CS.

---

## Setup (once)

Open a terminal **in this folder**.

- **Windows:** in File Explorer, click the address bar, type `powershell`, Enter.
- **Mac/Linux:** `cd` into the cloned folder.

If `python` is missing on Windows, use `py` instead in every command.

### 1. Clone and install

```powershell
git clone https://github.com/Heet79908/outreach-civil.git
cd outreach-civil
python --version
python -m pip install -r requirements.txt
```

### 2. Gemini key

1. Open [Google AI Studio — API keys](https://aistudio.google.com/apikey).
2. Sign in → **Create API key**.
3. Copy it (starts with `AIza`).
4. You will paste it into `.env` in step 4.

The model is `gemini-3.5-flash-lite`. You do not pick one.

### 3. Gmail send (Google Cloud OAuth)

This is the annoying part. After it works once, you mostly leave it alone.

Use the Gmail that should show up in the **From** field.

1. Open [Google Cloud Console](https://console.cloud.google.com/) and sign in with that Gmail.
2. New project, name it something like `faculty-outreach`, select it.
3. Enable [Gmail API](https://console.cloud.google.com/apis/library/gmail.googleapis.com).
4. [OAuth consent screen](https://console.cloud.google.com/auth/overview):
   - User type: **External**
   - App name: e.g. `Faculty Outreach`
   - Support / developer email: your Gmail
   - Leave it on **Testing**
   - **Test users:** add **your full Gmail**. Skip this and login fails.
   - Scope if asked: `https://www.googleapis.com/auth/gmail.send`
5. [Credentials](https://console.cloud.google.com/auth/clients) → **Create credentials** → **OAuth client ID**
   - Application type: **Desktop app**
   - Download the JSON
6. Rename that file to `credentials.json` and put it next to `run_outreach.py`.

First send opens a browser. Sign in with the **same** Gmail. If you see **Google hasn’t verified this app**, click **Advanced** → **Go to … (unsafe)** → Allow.

That creates `token.json`. Leave it there.

Testing apps forget the login about every **7 days**. Next run will just open the browser again. That is normal.

### 4. `.env`

```powershell
copy .env.example .env
```

On Mac/Linux: `cp .env.example .env`

```
GEMINI_API_KEY=paste_your_gemini_key_here
EMAIL_USER=youremail@gmail.com
```

Optional Groq keys, only used when Gemini hits its limit:

```
GROQ_API_KEYS=gsk_key1,gsk_key2,gsk_key3
```

No quotes around values. `EMAIL_USER` must be the same Gmail as Google Cloud.

### 5. Your profile

```powershell
copy profile.example.json profile.json
```

Edit `profile.json` with your real name, university, degree, CPI, projects, and experience. The emails are short and built around one of the professor’s papers, plus a bit of you.

If you change `profile.json` later, only **unsent** drafts get rewritten. Mail already sent stays as it was.

---

## Run

```powershell
python run_outreach.py
```

That is the whole loop: scrape ~500 civil faculty, find emails, send every new address.

Useful extras:

```powershell
python run_outreach.py --skip-research
python run_outreach.py --limit 20
```

- `--skip-research` — do not scrape. Only draft/send people already in the database.
- `--limit 20` — send at most 20 this run (handy the first time, so you can check Gmail).

A full scrape run (unless `--skip-research`):

1. Load QS Civil & Structural Engineering 2025, keep allowed-country schools, take the **top 80**, then pull civil authors from OpenAlex until **500** unique faculty are stored. Look up a recent paper and hunt emails. If the live scrape dies and you already have a `targets_civil.json`, that file is reused.
2. Write a personalized paper-inquiry email (better-ranked QS schools first).
3. Send **every address found**. Anyone already emailed is skipped.

Check replies yourself in Gmail. This does not watch the inbox.

The first scrape is slow — hours is normal. `Ctrl+C` stops it; mail already sent stays sent.

Gmail personal accounts are often capped near **500 sends/day**. Do not hammer it.

---

## Who gets emailed

Source: [QS 2025 — Engineering, Civil and Structural](https://www.topuniversities.com/university-subject-rankings/civil-structural-engineering). Full table is `data/qs_civil_ranking.json`.

**In:** US, Canada, Japan, Singapore, Australia, New Zealand, Europe (EU + UK + Switzerland + Norway + Iceland + Liechtenstein, plus Balkan / EU-adjacent countries that show up in QS).

**Out:** China, India, Korea, Hong Kong, Taiwan, Middle East, Latin America, Africa.

QS ranks **universities**, not people. After the country filter, OpenAlex finds civil / structural / construction authors at those schools. Inside a school, people are ordered by citations. Send order is QS rank, then citations.

All of them land in one SQLite table: `faculty_civil`.

Want to see the school list without hitting OpenAlex?

```powershell
python 1_parse_targets.py --list-schools
python 1_parse_targets.py --list-schools --top 20
```

---

## Running one step at a time

`run_outreach.py` already does these in order. Use them only if you want to pause between steps.

| Command | What it does |
|---|---|
| `python 1_parse_targets.py` | QS country filter + OpenAlex civil authors |
| `python 2_enrich_targets.py` | Recent paper (Crossref, then OpenAlex) |
| `python 2.5_resolve_emails.py` | Emails on public pages / ORCID / Wikipedia |
| `python 3_draft_emails.py` | Write emails, do not send |
| `python 4_dispatch.py` | Send drafts that already exist |

To scrape **without sending**: run `1`, then `2`, then `2.5`.

---

## If something breaks

**`Set GEMINI_API_KEY in the local .env file`**  
`.env` is missing, in the wrong folder, or the key line is empty. It has to sit next to `run_outreach.py`.

**`OAuth client file not found`**  
The Desktop client JSON must be named exactly `credentials.json` in this folder.

**`invalid_grant` / browser opens again**  
Normal for a Testing OAuth app after ~7 days. Sign in again.

**Google hasn’t verified this app**  
**Advanced** → continue. You are the only test user.

**Drafts fail with 429**  
Gemini rate limit. Wait, then `python run_outreach.py` again.

**First run looks stuck**  
OpenAlex + homepage scraping can take hours. Paper lookup may print Crossref/OpenAlex skips and keep going.

**Same professor emailed twice**  
Sent (and bounced) rows are kept on purpose so later scrapes skip those names and addresses.

---

## Keep these off GitHub

| File | Why |
|---|---|
| `.env` | Gemini / Groq keys |
| `credentials.json` | Your Google Cloud app |
| `token.json` | Permission to send as you |
| `profile.json` | Your personal details |
| `*.db` | Names, emails, drafts, send history |

`.gitignore` already covers them, plus `targets_*.json`.
