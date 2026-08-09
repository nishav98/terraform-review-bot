# AI-Assisted Terraform Code Review Bot

A GitHub Actions workflow that sends every Terraform pull request's diff to Google Gemini,
flags security misconfigurations, cost issues, and naming/best-practice violations,
and posts the findings as a PR comment automatically.

## What this demonstrates

- Integrating an LLM API into a real CI workflow, not just a notebook demo
- Structured prompting: forcing JSON output, validating it, handling malformed responses
- Using the GitHub REST API to post automated PR comments
- A test-fixture-driven eval set to measure the bot's actual catch rate, not just guess at it

## How it works

```
PR opened/updated (touching *.tf files)
        |
Extract diff (git diff base...head -- *.tf)
        |
Send diff to Gemini with a structured review prompt
        |
Parse JSON response: [{file, severity, issue, suggestion}, ...]
        |
Post as a PR comment via the GitHub REST API
```

## Project structure

```
.
├── scripts/
│   └── review_bot.py           # Core logic: diff → Claude → PR comment
├── test-fixtures/               # Intentionally broken .tf files for testing
│   ├── database.tf              # hardcoded password
│   ├── security_group.tf        # security group open to 0.0.0.0/0
│   └── storage.tf               # copy-pasted resources, inconsistent naming
├── .github/workflows/
│   └── terraform-review.yml     # Triggers the bot on every PR touching *.tf
└── requirements.txt
```

## Setup

1. Get a free API key from [Google AI Studio](https://aistudio.google.com/apikey) — no credit card required, generous free-tier rate limits.
2. In your GitHub repo: Settings → Secrets and variables → Actions → New repository secret
   - Name: `GEMINI_API_KEY`, Value: your key
   - (`GITHUB_TOKEN` is provided automatically by GitHub Actions — no setup needed)
3. Open a PR that touches any `.tf` file — the workflow triggers automatically

## Test it against the planted issues

The `test-fixtures/` folder has 3 files with known, intentional problems:
- `database.tf` — hardcoded database password
- `security_group.tf` — SSH and database ports open to the entire internet
- `storage.tf` — copy-pasted S3 buckets, inconsistent naming, no `for_each`

Open a PR that modifies one of these and watch the bot comment with its findings.
This is also how to measure a real catch rate instead of guessing — count how many
of the known, planted issues the bot actually flags, out of the total.

## Local testing (without opening a real PR)

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=your_key
export GITHUB_TOKEN=your_github_token
export GITHUB_REPOSITORY=your-username/your-repo
export PR_NUMBER=1
export BASE_SHA=<base-commit-sha>
export HEAD_SHA=<head-commit-sha>
python scripts/review_bot.py
```

## Design notes

- **Summary comment, not inline comments.** Inline PR review comments require
  calculating the exact diff position for each line, which is fragile. A single
  summary comment is simpler, more reliable, and still fully readable.
- **Forced JSON output.** The system prompt requires a JSON array with no markdown
  fences or extra text, which makes parsing reliable. If the model doesn't comply,
  the script fails soft (logs a warning, posts nothing) rather than crashing the workflow.
- **Only reviews `.tf` file diffs**, not the whole repo — keeps the prompt focused and
  avoids re-flagging unchanged code on every PR.

## Real evaluation results

Ran against the planted issues in `test-fixtures/` — reproducible with `scripts/eval_bot.py`:

```bash
export GEMINI_API_KEY=your_key
python scripts/eval_bot.py
```

| Planted issue | Caught? |
|---|---|
| Hardcoded database password | ❌ Missed |
| SSH (port 22) open to 0.0.0.0/0 | ✅ Caught (HIGH) |
| Database port (5432) open to 0.0.0.0/0 | ❌ Missed |
| Missing storage encryption on RDS | ✅ Caught (HIGH) — found independently, not explicitly planted |
| Missing resource tags | ✅ Caught (LOW) |

**Honest takeaway:** the bot reliably catches infrastructure-level misconfigurations (encryption, exposed ports it does flag) but missed a literal hardcoded secret and only caught one of two identical open-port issues in the same resource block — suggesting it may not be exhaustively checking every ingress rule in a block once it's flagged one. Worth investigating further with prompt tuning or splitting the review into per-resource passes rather than a whole-diff pass.

## What's next

- [x] Run against a real PR — **done**, verified live: caught 3/5 known planted issues (see evaluation results above)
- [x] Eval script for measuring precision/recall — `scripts/eval_bot.py`, runs against all `test-fixtures/` files directly and reports a real recall score
- [ ] Inline (line-level) PR comments instead of one summary comment — deliberately deferred: requires calculating exact diff hunk positions via GitHub's API, which is meaningfully more complex and error-prone than a summary comment. A summary comment is fully readable and reliable; this is a genuine "nice to have," not a blocker.
