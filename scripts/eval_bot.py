"""
eval_bot.py

Runs the review bot's core logic (Gemini call) directly against each file in
test-fixtures/, then checks the findings against a hand-labeled list of known
planted issues to compute a real precision/recall score.

This does NOT go through GitHub Actions or post PR comments — it's a
standalone local eval so the bot's accuracy can be measured and re-measured
as the prompt or model changes.

Usage:
    export GEMINI_API_KEY=your_key
    python scripts/eval_bot.py
"""

import json
import os
import sys
import time

import requests

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = "gemini-3.6-flash"
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

# Free-tier rate limits are tight (often just a few requests/minute on newer
# models). This script makes several calls per file (1 review + 1 judge call
# per known issue), so a conservative delay is needed between every call.
SECONDS_BETWEEN_CALLS = 15

SYSTEM_PROMPT = """You are an expert Terraform code reviewer specializing in AWS infrastructure.
Review the given Terraform file and flag issues in these categories only:
1. Security: hardcoded secrets/credentials, overly permissive security groups (0.0.0.0/0 on
   sensitive ports), publicly accessible resources that shouldn't be, missing encryption.
2. Cost: oversized instance types for no stated reason, missing lifecycle rules on storage,
   resources that will incur ongoing cost without clear justification.
3. Best practices: missing resource tags, inconsistent naming conventions, copy-pasted
   near-identical resources that should use count/for_each, missing versioning on state-critical
   resources.

Respond with ONLY a JSON array, no other text, no markdown fences. Each element:
{"severity": "<high|medium|low>", "issue": "<one sentence>", "suggestion": "<one sentence>"}

If there are no issues, respond with an empty array: []
"""

# Hand-labeled ground truth: what SHOULD be found in each fixture file.
# Each entry has a short keyword (for the fast/naive check) and a full
# description (for the semantic/LLM-judge check, which doesn't depend on
# exact wording).
KNOWN_ISSUES = {
    "test-fixtures/database.tf": [
        {"keyword": "password", "description": "The database password is hardcoded in plaintext instead of using a variable or secrets manager."},
        {"keyword": "encrypt", "description": "The database instance does not have storage encryption enabled."},
        {"keyword": "tag", "description": "The database resource is missing tags for organization/cost tracking."},
    ],
    "test-fixtures/security_group.tf": [
        {"keyword": "22", "description": "SSH (port 22) ingress is open to 0.0.0.0/0 (the entire internet)."},
        {"keyword": "5432", "description": "The Postgres database port (5432) ingress is open to 0.0.0.0/0 (the entire internet)."},
    ],
    "test-fixtures/storage.tf": [
        {"keyword": "for_each", "description": "Multiple near-identical S3 buckets are copy-pasted as separate resource blocks instead of using count or for_each."},
        {"keyword": "naming", "description": "The S3 bucket resource names use an inconsistent naming convention (mixing snake_case and kebab-case)."},
    ],
}


def _call_gemini(content: str, max_retries: int = 3) -> str:
    headers = {
        "content-type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY,  # header, not URL param — keeps it out of error/log URLs
    }
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": content}]}],
        "generationConfig": {"maxOutputTokens": 2000},
    }

    time.sleep(SECONDS_BETWEEN_CALLS)  # stay under free-tier rate limits

    for attempt in range(max_retries):
        response = requests.post(GEMINI_API_URL, headers=headers, json=payload, timeout=60)
        if response.status_code == 429:
            wait = 20 * (attempt + 1)  # 20s, 40s, 60s
            print(f"   Rate limited, waiting {wait}s before retry ({attempt + 1}/{max_retries})...", file=sys.stderr)
            time.sleep(wait)
            continue
        response.raise_for_status()
        data = response.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    raise RuntimeError(f"Gave up after {max_retries} retries due to repeated rate limiting.")


def review_file(path: str) -> list | None:
    """Returns the findings list, or None if the API call couldn't complete
    at all (e.g. daily quota exhausted) — distinct from an empty list, which
    means the model ran successfully and found nothing."""
    with open(path) as f:
        content = f.read()

    prompt = f"Review this Terraform file:\n\n{content}"
    try:
        text = _call_gemini(prompt)
    except RuntimeError as e:
        print(f"   Could not review {path}: {e}", file=sys.stderr)
        return None

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print(f"   First response for {path} wasn't valid JSON, retrying...", file=sys.stderr)

    retry_prompt = (
        prompt
        + "\n\nIMPORTANT: your previous response was not valid JSON. "
        + "Respond with ONLY a raw JSON array — no markdown fences, no commentary, no explanation."
    )
    try:
        text = _call_gemini(retry_prompt)
    except RuntimeError as e:
        print(f"   Could not retry {path}: {e}", file=sys.stderr)
        return None

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print(f"Warning: retry also failed to parse response for {path}", file=sys.stderr)
        return []


def score_findings_keyword(findings: list, known_issues: list) -> tuple:
    """Naive method: checks if each expected keyword appears literally in any
    finding's issue text. Fast, free, but undercounts issues the bot caught
    using different wording than our label."""
    all_issue_text = " ".join(f.get("issue", "").lower() for f in findings)
    caught = [k["keyword"] for k in known_issues if k["keyword"].lower() in all_issue_text]
    missed = [k["keyword"] for k in known_issues if k["keyword"].lower() not in all_issue_text]
    return caught, missed


JUDGE_PROMPT = """You are grading whether a code review tool caught a specific known issue.

Known issue: "{description}"

The tool's findings for this file:
{findings_text}

Did any of the tool's findings correctly identify this SPECIFIC known issue (even if worded
differently)? Respond with ONLY the single word "yes" or "no", nothing else."""


def score_findings_semantic(findings: list, known_issues: list) -> tuple:
    """LLM-as-judge method: asks the model directly whether each known issue
    was semantically caught by any finding, regardless of exact wording.
    Slower and costs extra API calls, but more accurate than keyword matching.

    If the free-tier quota runs out partway through, this returns whatever
    was determined so far rather than crashing the whole eval run — quota
    exhaustion mid-run is a real, expected failure mode on the free tier."""
    findings_text = "\n".join(
        f"- [{f.get('severity', '?')}] {f.get('issue', '')}" for f in findings
    ) or "(no findings)"

    caught, missed, undetermined = [], [], []
    for known in known_issues:
        prompt = JUDGE_PROMPT.format(description=known["description"], findings_text=findings_text)
        try:
            answer = _call_gemini(prompt).strip().lower()
        except RuntimeError:
            print(f"   Quota exhausted judging '{known['keyword']}' — leaving undetermined.", file=sys.stderr)
            undetermined.append(known["keyword"])
            continue
        if answer.startswith("yes"):
            caught.append(known["keyword"])
        else:
            missed.append(known["keyword"])
    return caught, missed, undetermined


def main():
    keyword_expected = 0
    keyword_caught = 0
    semantic_expected = 0
    semantic_caught = 0
    semantic_undetermined = 0
    total_findings = 0

    print("=" * 60)
    print("Terraform Review Bot — Evaluation Run")
    print("=" * 60)

    for path, known_issues in KNOWN_ISSUES.items():
        print(f"\n📄 {path}")
        findings = review_file(path)

        if findings is None:
            print(f"   Skipping scoring for {path} — could not get a response (likely daily quota exhausted).")
            keyword_expected += len(known_issues)
            semantic_expected += len(known_issues)
            semantic_undetermined += len(known_issues)
            continue

        total_findings += len(findings)
        print(f"   Findings returned: {len(findings)}")

        kw_caught, kw_missed = score_findings_keyword(findings, known_issues)
        keyword_expected += len(known_issues)
        keyword_caught += len(kw_caught)
        print(f"   [keyword match]  caught {len(kw_caught)}/{len(known_issues)} — missed: {kw_missed}")

        sem_caught, sem_missed, sem_undetermined = score_findings_semantic(findings, known_issues)
        semantic_expected += len(known_issues)
        semantic_caught += len(sem_caught)
        semantic_undetermined += len(sem_undetermined)
        print(f"   [semantic judge] caught {len(sem_caught)}/{len(known_issues)} — missed: {sem_missed}", end="")
        if sem_undetermined:
            print(f" — undetermined (quota): {sem_undetermined}")
        else:
            print()

    kw_recall = keyword_caught / keyword_expected if keyword_expected else 0
    sem_determined = semantic_expected - semantic_undetermined
    sem_recall = semantic_caught / sem_determined if sem_determined else 0

    print("\n" + "=" * 60)
    print("RESULTS")
    print(f"  Keyword-match recall:  {keyword_caught}/{keyword_expected} ({kw_recall:.0%})")
    print(f"  Semantic-judge recall: {semantic_caught}/{sem_determined} ({sem_recall:.0%})", end="")
    if semantic_undetermined:
        print(f"  [{semantic_undetermined} undetermined due to free-tier rate limits]")
    else:
        print()
    print(f"  Total findings across all files: {total_findings}")
    print("=" * 60)
    print("\nThe semantic score is the more trustworthy number — keyword matching")
    print("undercounts issues the bot caught but described in different words.")


if __name__ == "__main__":
    main()