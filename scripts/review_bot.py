"""
review_bot.py

Sends the Terraform diff of a pull request to the Claude API for automated
review, then posts the findings as inline PR review comments via the
GitHub REST API.

Environment variables expected (set as GitHub Actions secrets/context):
    ANTHROPIC_API_KEY   - Claude API key
    GITHUB_TOKEN        - provided automatically by GitHub Actions
    GITHUB_REPOSITORY   - e.g. "nishav98/terraform-review-bot"
    PR_NUMBER           - pull request number being reviewed
    BASE_SHA            - base commit SHA of the PR
    HEAD_SHA            - head commit SHA of the PR
"""

import json
import os
import subprocess
import sys

import requests

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPOSITORY = os.environ["GITHUB_REPOSITORY"]
PR_NUMBER = os.environ["PR_NUMBER"]
BASE_SHA = os.environ["BASE_SHA"]
HEAD_SHA = os.environ["HEAD_SHA"]

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
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


def review_with_claude(diff: str) -> list:
    if not diff.strip():
        print("No .tf file changes in this PR, skipping review.")
        return []

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        # Check https://docs.claude.com for the latest model names before running this for real.
        "model": "claude-sonnet-4-5",
        "max_tokens": 2000,
        "system": SYSTEM_PROMPT,
        "messages": [
            {"role": "user", "content": f"Review this Terraform diff:\n\n{diff}"}
        ],
    }

    response = requests.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=60)
    response.raise_for_status()
    data = response.json()

    text = "".join(block["text"] for block in data["content"] if block["type"] == "text")

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
    findings = review_with_claude(diff)
    post_summary_comment(findings)


if __name__ == "__main__":
    main()
