"""
review_bot.py

Sends the Terraform diff of a pull request to the Google Gemini API for
automated review, then posts the findings as a PR comment via the
GitHub REST API.

Environment variables expected (set as GitHub Actions secrets/context):
    GEMINI_API_KEY       - Google Gemini API key (free tier, no card required)
    GITHUB_TOKEN         - provided automatically by GitHub Actions
    GITHUB_REPOSITORY    - e.g. "nishav98/terraform-review-bot"
    PR_NUMBER            - pull request number being reviewed
    BASE_SHA             - base commit SHA of the PR
    HEAD_SHA             - head commit SHA of the PR
"""

import json
import os
import subprocess
import sys

import requests

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPOSITORY = os.environ["GITHUB_REPOSITORY"]
PR_NUMBER = os.environ["PR_NUMBER"]
BASE_SHA = os.environ["BASE_SHA"]
HEAD_SHA = os.environ["HEAD_SHA"]

# Check https://ai.google.dev/gemini-api/docs/models for the latest free-tier model names.
GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPOSITORY}"

SYSTEM_PROMPT = """You are an expert Terraform code reviewer specializing in AWS infrastructure.
Review the given Terraform diff and flag issues in these categories only:
1. Security: hardcoded secrets/credentials, overly permissive security groups (0.0.0.0/0 on
   sensitive ports), publicly accessible resources that shouldn't be, missing encryption.
2. Cost: oversized instance types for no stated reason, missing lifecycle rules on storage,
   resources that will incur ongoing cost without clear justification.
3. Best practices: missing resource tags, inconsistent naming conventions, copy-pasted
   near-identical resources that should use count/for_each, missing versioning on state-critical
   resources.

Respond with ONLY a JSON array, no other text, no markdown fences. Each element:
{"file": "<filename>", "severity": "<high|medium|low>", "issue": "<one sentence>", "suggestion": "<one sentence>"}

If there are no issues, respond with an empty array: []
"""


def get_diff() -> str:
    """Get the diff of only .tf files between base and head commits."""
    result = subprocess.run(
        ["git", "diff", f"{BASE_SHA}...{HEAD_SHA}", "--", "*.tf"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def review_with_gemini(diff: str) -> list:
    if not diff.strip():
        print("No .tf file changes in this PR, skipping review.")
        return []

    headers = {"content-type": "application/json"}
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [
            {"parts": [{"text": f"Review this Terraform diff:\n\n{diff}"}]}
        ],
        "generationConfig": {"maxOutputTokens": 2000},
    }

    response = requests.post(
        f"{GEMINI_API_URL}?key={GEMINI_API_KEY}",
        headers=headers,
        json=payload,
        timeout=60,
    )
    if not response.ok:
        print(f"Gemini API error {response.status_code}: {response.text}", file=sys.stderr)
    response.raise_for_status()
    data = response.json()

    text = data["candidates"][0]["content"]["parts"][0]["text"]

    # Gemini sometimes wraps JSON in markdown fences despite instructions not to; strip them.
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        findings = json.loads(text)
    except json.JSONDecodeError:
        print(f"Warning: could not parse model response as JSON:\n{text}", file=sys.stderr)
        return []

    return findings


def post_summary_comment(findings: list):
    """Post findings as a single summary comment on the PR (simpler and more
    reliable than per-line inline comments, which require diff position math)."""
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    if not findings:
        body = "**Terraform Review Bot**: No issues found in this PR's `.tf` changes. ✅"
    else:
        severity_emoji = {"high": "🔴", "medium": "🟠", "low": "🟡"}
        lines = ["**Terraform Review Bot** found the following:\n"]
        for f in findings:
            emoji = severity_emoji.get(f.get("severity", "low"), "⚪")
            lines.append(
                f"{emoji} **{f.get('severity', 'unknown').upper()}** — `{f.get('file', 'unknown file')}`\n"
                f"  - Issue: {f.get('issue', 'n/a')}\n"
                f"  - Suggestion: {f.get('suggestion', 'n/a')}\n"
            )
        body = "\n".join(lines)

    url = f"{GITHUB_API_URL}/issues/{PR_NUMBER}/comments"
    response = requests.post(url, headers=headers, json={"body": body}, timeout=30)
    response.raise_for_status()
    print(f"Posted comment with {len(findings)} finding(s).")


def main():
    diff = get_diff()
    findings = review_with_gemini(diff)
    post_summary_comment(findings)


if __name__ == "__main__":
    main()