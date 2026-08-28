# AI-Assisted Terraform Code Review Bot

A GitHub Actions workflow that sends every Terraform pull request's diff (plus full file
context) to Google Gemini, flags security misconfigurations, cost issues, and
naming/best-practice violations, and posts the findings as inline PR comments automatically.

## What this demonstrates

- Integrating an LLM API into a real CI workflow, not just a notebook demo
- Structured prompting: forcing JSON output, validating it, retrying on malformed responses
- Using the GitHub REST API to post automated inline PR review comments, with a
  summary-comment fallback for anything that can't be anchored to a line
- A test-fixture-driven eval set to measure the bot's actual catch rate — including two
  scoring methods (keyword match and LLM-as-judge) to be honest about the first one's limits
- Handling real-world API constraints: rate limits, daily quotas, and secret-handling mistakes,
  documented rather than hidden

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
│   ├── review_bot.py             # Core logic: diff + full-file context → Gemini → inline PR comments
│   └── eval_bot.py                # Standalone eval: measures recall with keyword + semantic scoring
├── test-fixtures/                 # Intentionally broken .tf files for testing
│   ├── database.tf                # hardcoded password
│   ├── security_group.tf          # security group open to 0.0.0.0/0
│   └── storage.tf                 # copy-pasted resources, inconsistent naming
├── .github/workflows/
│   └── terraform-review.yml       # Triggers the bot on every PR touching *.tf
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

- **Inline comments with a summary fallback.** Each finding includes a `line` number from the
  model, and the bot tries to post it directly on that line using GitHub's `line`/`side` comment
  API. Anything that can't be placed inline (no line number, or GitHub rejects the line because
  it's not part of the diff) falls back to a single summary comment, so nothing silently disappears.
- **Full-file context, not diff-only.** The bot sends both the diff AND the complete current
  content of each changed file. Local eval testing showed this measurably improves recall over
  diff-only review — a diff alone can hide an issue that's obvious with the whole resource block visible.
- **Forced JSON output with a retry.** The system prompt requires a JSON array with no markdown
  fences. If the model doesn't comply, the script retries once with a stricter reminder before
  giving up — this fixed a real parse failure found during eval testing (see below).
- **API key passed via header, not URL.** Early versions passed the Gemini API key as a URL query
  parameter, which meant it appeared in full inside error tracebacks — including one that got
  pasted into a debugging session. Switched to the `x-goog-api-key` header, which never appears
  in request URLs or the logs/errors built from them.
- **Only reviews `.tf` file diffs**, not the whole repo — keeps the prompt focused and
  avoids re-flagging unchanged code on every PR.

## Challenges hit and fixed along the way

Real debugging, not a smooth tutorial run:

- **Model names changed faster than expected.** Went through three wrong Gemini model names
  in a row (`gemini-2.0-flash`, `gemini-1.5-flash`, `gemini-2.5-flash`) before finding the
  actual current model — each failed with a different error (quota, 404 "not found", 404
  "no longer available to new users"). Stopped guessing and searched for the live, current
  answer rather than relying on stale training knowledge.
- **An API key was accidentally exposed in a debugging session** — a `429` error's traceback
  printed the full request URL, which included the key as a query parameter. Rotated the key
  immediately and fixed the root cause by switching to header-based authentication instead
  of a URL parameter, so this can't happen again regardless of what an error prints.
- **Hit the free tier's daily quota mid-testing**, not just a per-minute rate limit — retrying
  with backoff didn't help since the cap resets daily, not by the minute. Rewrote the eval
  script to fail gracefully (report "undetermined due to quota" per item) instead of crashing
  the whole run, which is the correct behavior for a real, expected failure mode on a free tier.

## Real evaluation results

Ran with `scripts/eval_bot.py` against the planted issues in `test-fixtures/`:

```bash
export GEMINI_API_KEY=your_key
python scripts/eval_bot.py
```

**Final verified result: 7/7 known planted issues caught — 100% recall (semantic scoring)**,
up from 86% keyword-match recall in the prior run — see "iteration history" below.

| File | Findings | Keyword match | Semantic judge |
|---|---|---|---|
| `database.tf` | 3 | 3/3 ✅ | 3/3 ✅ |
| `security_group.tf` | 3 | 2/2 ✅ | 2/2 ✅ |
| `storage.tf` | 6 | 1/2 (missed `for_each`) | 2/2 ✅ |
| **Total** | 12 | **6/7 (86%)** | **7/7 (100%)** |

**Honest takeaways:**

1. **Full-file review outperformed diff-only review**, which is why the bot sends full file
   content alongside the diff on every PR review, not just the changed lines (see `review_bot.py`).
2. **A retry-with-stricter-prompt fallback fixed a real failure mode.** An earlier run had
   `storage.tf`'s response fail to parse as valid JSON, silently recording zero findings. One
   automatic retry with a stricter "respond with ONLY raw JSON" reminder fixed this entirely.
3. **The keyword-match "miss" on `for_each` was confirmed to be a measurement artifact, not a
   real bot failure.** The semantic (LLM-as-judge) scoring method — which asks the model directly
   whether a finding matches a known issue's *meaning*, not its exact wording — confirmed the
   bot did correctly catch the copy-pasted-resources issue, just phrased differently than the
   keyword expected. This is exactly why the semantic method was built: naive keyword matching
   systematically undercounts a model's real performance.
4. **Hit the free tier's daily quota mid-testing** while building the semantic scorer (7 extra
   judge calls per run adds up fast). Waiting for the daily reset — not a longer per-minute
   backoff — was the actual fix; the eval script now also fails gracefully with an "undetermined"
   result per item instead of crashing when this happens.

### Iteration history

| Version | Recall | What changed |
|---|---|---|
| v1 (diff-only, single API call, no retry) | 71% (5/7) | Baseline — live PR test missed the hardcoded password and one open port when reviewing only the diff |
| v2 (+ full-file context, + retry-on-parse-failure, + inline comments) | 86% (6/7, keyword match) | Full-file context and the retry logic fixed the diff-only blind spots and the `storage.tf` parse failure |
| v3 (+ semantic/LLM-as-judge scoring) | **100% (7/7, semantic)** | Revealed the "missed" `for_each` result was a measurement limitation, not a real gap — the bot had actually caught it all along |

## What's next

- [x] Run against a real PR — **done**, verified live via an actual PR comment
- [x] Eval script for measuring precision/recall — `scripts/eval_bot.py`, real verified result: 100% recall via semantic scoring (86% via naive keyword matching, see comparison above)
- [x] Retry-with-stricter-prompt fallback for parse failures — **done**, fixed the `storage.tf` failure, verified
- [x] Full-file context alongside the diff — **done**, directly improved recall as hypothesized, verified
- [x] Semantic (LLM-as-judge) scoring in the eval script — **done, fully verified**: confirmed 7/7 recall, revealing the keyword method had undercounted by one
- [ ] Inline (line-level) PR comments via GitHub's `line`/`side` API, with summary-comment fallback — **code written, not yet verified live**. A daily API quota wall blocked testing this against a real PR; still needs one genuine end-to-end confirmation

**Honest note:** every claim above with "verified" next to it has been run and confirmed for real — the eval numbers in this README come directly from actual terminal output, not estimates. Inline comments are the one remaining piece that's written but not yet proven to work against a live GitHub PR.