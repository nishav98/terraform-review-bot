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

import requests

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = "gemini-3.6-flash"
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

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
# Each entry is a short keyword/phrase that should appear somewhere in the
# bot's findings (issue text) if it correctly caught that specific problem.
KNOWN_ISSUES = {
    "test-fixtures/database.tf": [
        "password",       # hardcoded credential
        "encrypt",        # missing storage encryption
        "tag",            # missing resource tags
    ],
    "test-fixtures/security_group.tf": [
        "22",              # SSH port open
        "5432",            # Postgres port open
    ],
    "test-fixtures/storage.tf": [
        "for_each",        # copy-pasted resources
        "naming",          # inconsistent naming
    ],
}


def review_file(path: str) -> list:
    with open(path) as f:
        content = f.read()

    headers = {"content-type": "application/json"}
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": f"Review this Terraform file:\n\n{content}"}]}],
        "generationConfig": {"maxOutputTokens": 2000},
    }

    response = requests.post(
        f"{GEMINI_API_URL}?key={GEMINI_API_KEY}", headers=headers, json=payload, timeout=60
    )
    response.raise_for_status()
    data = response.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print(f"Warning: could not parse response for {path}", file=sys.stderr)
        return []


def score_findings(findings: list, expected_keywords: list) -> tuple:
    """Returns (caught_keywords, missed_keywords) by checking if each expected
    keyword appears in any finding's issue text, case-insensitive."""
    all_issue_text = " ".join(f.get("issue", "").lower() for f in findings)
    caught = [kw for kw in expected_keywords if kw.lower() in all_issue_text]
    missed = [kw for kw in expected_keywords if kw.lower() not in all_issue_text]
    return caught, missed


def main():
    total_expected = 0
    total_caught = 0
    total_findings = 0

    print("=" * 60)
    print("Terraform Review Bot — Evaluation Run")
    print("=" * 60)

    for path, expected_keywords in KNOWN_ISSUES.items():
        print(f"\n📄 {path}")
        findings = review_file(path)
        caught, missed = score_findings(findings, expected_keywords)

        total_findings += len(findings)
        total_expected += len(expected_keywords)
        total_caught += len(caught)

        print(f"   Findings returned: {len(findings)}")
        print(f"   Expected issues caught: {len(caught)}/{len(expected_keywords)} {caught}")
        if missed:
            print(f"   Missed: {missed}")

    recall = total_caught / total_expected if total_expected else 0
    print("\n" + "=" * 60)
    print(f"RESULTS: caught {total_caught}/{total_expected} known planted issues")
    print(f"Recall: {recall:.0%}")
    print(f"Total findings across all files: {total_findings}")
    print("=" * 60)
    print("\nNote: this measures recall (did it catch known issues), not precision")
    print("(false positives). For precision, manually review the 'findings' output")
    print("above for anything flagged that isn't a real problem.")


if __name__ == "__main__":
    main()
