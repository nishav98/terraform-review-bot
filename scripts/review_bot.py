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

# Check https://ai.google.dev/gemini-api/docs/models for the latest model names —
# Google's Gemini lineup changes often; this was current as of Aug 2026.
GEMINI_MODEL = "gemini-3.6-flash"
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPOSITORY}"

SYSTEM_PROMPT = """You are an expert Terraform code reviewer specializing in AWS infrastructure.
You will be given the diff of a pull request AND the full current content of each changed file,
for context. Review it and flag issues in these categories only:
1. Security: hardcoded secrets/credentials, overly permissive security groups (0.0.0.0/0 on
   sensitive ports), publicly accessible resources that shouldn't be, missing encryption.
2. Cost: oversized instance types for no stated reason, missing lifecycle rules on storage,
   resources that will incur ongoing cost without clear justification.
3. Best practices: missing resource tags, inconsistent naming conventions, copy-pasted
   near-identical resources that should use count/for_each, missing versioning on state-critical
   resources.

Check EVERY instance of a problem, not just the first one you find — for example, if a security
group has multiple ports open to 0.0.0.0/0, flag each one separately.

Respond with ONLY a JSON array, no other text, no markdown fences. Each element:
{"file": "<filename>", "line": <int line number in the file, or null if unsure>,
 "severity": "<high|medium|low>", "issue": "<one sentence>", "suggestion": "<one sentence>"}

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


def get_changed_files() -> list:
    """List which .tf files actually changed (filenames only)."""
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{BASE_SHA}...{HEAD_SHA}", "--", "*.tf"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [f for f in result.stdout.splitlines() if f.strip()]


def get_full_file_content(path: str) -> str:
    """Get the full content of a file as it exists at HEAD_SHA. Sending this
    alongside the diff gives the model full context, not just the changed
    lines — this measurably improved recall in local eval testing (see README)."""
    result = subprocess.run(
        ["git", "show", f"{HEAD_SHA}:{path}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def call_gemini(user_content: str) -> list:
    """Calls Gemini and parses the JSON response. Retries once with a
    stricter reminder if the first response isn't valid JSON, instead of
    silently giving up (this is what caused the storage.tf failure in eval
    testing — see README)."""
    headers = {
        "content-type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY,  # header, not URL param — keeps it out of error/log URLs
    }

    def _call(content: str) -> str:
        payload = {
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"parts": [{"text": content}]}],
            "generationConfig": {"maxOutputTokens": 2000},
        }
        response = requests.post(GEMINI_API_URL, headers=headers, json=payload, timeout=60)
        if not response.ok:
            print(f"Gemini API error {response.status_code}: {response.text}", file=sys.stderr)
        response.raise_for_status()
        data = response.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    text = _call(user_content)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print("Warning: first response wasn't valid JSON, retrying with a stricter reminder...", file=sys.stderr)

    retry_content = (
        user_content
        + "\n\nIMPORTANT: your previous response was not valid JSON. "
        + "Respond with ONLY a raw JSON array — no markdown fences, no commentary, no explanation."
    )
    text = _call(retry_content)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print(f"Warning: retry also failed to parse as JSON:\n{text}", file=sys.stderr)
        return []


def review_with_gemini(diff: str, changed_files: list) -> list:
    if not diff.strip():
        print("No .tf file changes in this PR, skipping review.")
        return []

    # Send the diff AND the full current content of each changed file, since
    # full-file context measurably caught more real issues than diff alone
    # in local eval testing.
    context_sections = []
    for path in changed_files:
        try:
            content = get_full_file_content(path)
            context_sections.append(f"--- Full content of {path} ---\n{content}")
        except subprocess.CalledProcessError:
            continue  # file may have been deleted in this PR

    user_content = (
        f"Diff of this pull request:\n\n{diff}\n\n"
        + "\n\n".join(context_sections)
    )

    return call_gemini(user_content)


def post_inline_comments(findings: list) -> list:
    """Try to post each finding as an inline (line-level) PR review comment,
    anchored to the actual line in the file. Returns the list of findings
    that could NOT be posted inline (e.g. no line number, or the line isn't
    part of this PR's diff) so they can fall back to a summary comment."""
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    severity_emoji = {"high": "🔴", "medium": "🟠", "low": "🟡"}
    url = f"{GITHUB_API_URL}/pulls/{PR_NUMBER}/comments"

    unplaced = []
    for f in findings:
        line = f.get("line")
        path = f.get("file")
        if not line or not path:
            unplaced.append(f)
            continue

        emoji = severity_emoji.get(f.get("severity", "low"), "⚪")
        body = (
            f"{emoji} **{f.get('severity', 'unknown').upper()}** (Terraform Review Bot)\n\n"
            f"**Issue:** {f.get('issue', 'n/a')}\n\n"
            f"**Suggestion:** {f.get('suggestion', 'n/a')}"
        )
        payload = {
            "body": body,
            "commit_id": HEAD_SHA,
            "path": path,
            "line": int(line),
            "side": "RIGHT",
        }
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        if response.status_code == 201:
            print(f"Posted inline comment on {path}:{line}")
        else:
            # Common cause: the line isn't part of this PR's actual diff hunk,
            # which GitHub's API requires for inline comments. Fall back to summary.
            print(
                f"Could not post inline comment on {path}:{line} "
                f"({response.status_code}), falling back to summary.",
                file=sys.stderr,
            )
            unplaced.append(f)

    return unplaced


def post_summary_comment(findings: list, inline_count: int = 0):
    """Post any findings that couldn't be placed inline as a single summary
    comment. If everything was placed inline and there's nothing left, post
    a short confirmation instead of a full empty-findings message."""
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    if not findings and inline_count == 0:
        body = "**Terraform Review Bot**: No issues found in this PR's `.tf` changes. ✅"
    elif not findings:
        body = f"**Terraform Review Bot**: {inline_count} finding(s) posted as inline comments above. ✅"
    else:
        severity_emoji = {"high": "🔴", "medium": "🟠", "low": "🟡"}
        lines = ["**Terraform Review Bot** — additional findings (could not be anchored to a specific line):\n"]
        for f in findings:
            emoji = severity_emoji.get(f.get("severity", "low"), "⚪")
            lines.append(
                f"{emoji} **{f.get('severity', 'unknown').upper()}** — `{f.get('file', 'unknown file')}`\n"
                f"  - Issue: {f.get('issue', 'n/a')}\n"
                f"  - Suggestion: {f.get('suggestion', 'n/a')}\n"
            )
        if inline_count:
            lines.insert(1, f"_({inline_count} additional finding(s) posted as inline comments above.)_\n")
        body = "\n".join(lines)

    url = f"{GITHUB_API_URL}/issues/{PR_NUMBER}/comments"
    response = requests.post(url, headers=headers, json={"body": body}, timeout=30)
    response.raise_for_status()
    print(f"Posted summary comment ({len(findings)} finding(s) in it).")


def main():
    diff = get_diff()
    changed_files = get_changed_files()
    findings = review_with_gemini(diff, changed_files)

    total_findings = len(findings)
    unplaced = post_inline_comments(findings)
    inline_count = total_findings - len(unplaced)

    post_summary_comment(unplaced, inline_count=inline_count)


if __name__ == "__main__":
    main()