# AI Watch

Automated daily digest of news on **trustworthy AI**, **responsible AI**, **AI privacy**, and **machine unlearning**, pulled from WSJ, Bloomberg, The Markup, TechCrunch, and 보안뉴스.

Runs every day at 10:00 KST. Window: yesterday 10:00 → today 10:00 KST.

## What it actually does

```
┌──────────┐   ┌──────────┐   ┌────────┐   ┌──────────┐   ┌─────────┐   ┌────────┐   ┌─────────┐
│  Fetch   │ → │  Window  │ → │ Keyword│ → │ Classify │ → │ Extract │ → │Summary │ → │ Cluster │ → render → email
│  RSS×5   │   │  filter  │   │  regex │   │  (Haiku) │   │ full    │   │(Sonnet)│   │ (Haiku) │
│          │   │          │   │ (EN+KO)│   │  stage 1 │   │ text    │   │ stage 2│   │ stage 3 │
└──────────┘   └──────────┘   └────────┘   └──────────┘   └─────────┘   └────────┘   └─────────┘
```

Two LLM stages because precision matters: a cheap Haiku call kills 90%+ of false positives, then Sonnet does substantive summarization only on what survives. Final clustering pass groups the same story showing up in 3 outlets into one item.

WSJ + Bloomberg are paywalled — the script doesn't try to bypass that. They're surfaced as **headline + RSS blurb** in a separate "Paywalled Headlines" section so you can decide whether to open them.

## Output format

Each digest is `digests/YYYY-MM-DD.md`:
<hr>

# AI Watch — Daily Digest
**Window:** 2026-05-06 10:00 KST → 2026-05-07 10:00 KST
**Generated:** 2026-05-07 10:00:23 KST
**Sources scanned:** 5 · Articles seen: 142 · After keyword filter: 38 · Relevant: 9 · Story clusters: 7

## 🔴 Top Stories
### [Headline]
_Source · timestamp · importance 5/5 · category_
[3-4 sentence substantive summary]
**Why it matters:** [implication]
**Entities:** [orgs, laws, people]
[Read →]

## 📂 By Category
### 🧠 Machine Unlearning
### 🔐 AI Privacy
### 🛡️  Trust & Safety
### ⚖️  Governance & Policy

## 🔒 Paywalled Headlines
- **[WSJ]** Title (link) — RSS blurb

</hr>

## Setup

### 1. Create a GitHub repo and push these files

```bash
git init
git add .
git commit -m "init"
git remote add origin git@github.com:YOURNAME/ai-watch.git
git push -u origin main
```

### 2. Add repo secrets

Repo → Settings → Secrets and variables → Actions → New repository secret:

| Secret | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your key from console.anthropic.com |
| `SMTP_HOST` | `smtp.gmail.com` (or your provider) |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | Your email |
| `SMTP_PASSWORD` | Gmail **App Password** ([create here](https://myaccount.google.com/apppasswords)) — not your regular password |
| `SLACK_WEBHOOK_URL` | Optional, only if Slack delivery enabled |

### 3. Edit `config.yaml`

Set `email.to` to your address. If you don't want email, set `email.enabled: false`.

### 4. Test it manually

GitHub repo → Actions tab → "Daily AI Watch Digest" → "Run workflow". First run will commit a digest to `digests/`.

After that, it runs every day at 10:00 KST automatically.

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then fill in
python main.py
```

Output appears in `digests/YYYY-MM-DD.md`.

## Tuning quality

The prompts in `main.py` are where quality lives.

- **Too much noise?** Edit `CLASSIFY_SYSTEM` to be stricter. The current prompt rejects vendor PR and product launches; you can extend the reject list.
- **Missing important stories?** Loosen the keyword regex (`KEYWORDS_EN` / `KEYWORDS_KO`) — the keyword filter is an OR with the LLM classifier, so broader keywords just give the LLM more candidates, not more output.
- **Want longer/shorter summaries?** Edit `SUMMARIZE_USER` and bump/cut `max_tokens` in `summarize()`.
- **Care more about unlearning than other categories?** The classifier already prioritizes it. To weight further, edit the importance heuristic in `render_digest`.

## Cost

Per run, with ~30-50 candidates surviving the keyword filter:
- Classification: 30-50 × Haiku calls ≈ $0.01
- Summarization: 5-10 × Sonnet calls ≈ $0.05-0.15
- Clustering: 1 × Haiku call ≈ $0.001

**~$0.05-0.20/day**, or **~$2-6/month**. GitHub Actions cron is free for public repos and within the free tier for private repos at this volume.

## Worth adding later

- **arXiv feed**: cs.LG + privacy/unlearning keywords. Catches papers ~1 week before journalists cover them. Add as another source with `paywalled: false`.
- **Embedding-based dedup** instead of LLM clustering — faster and cheaper if you ever process >100 relevant items/day. Not needed at current volume.
- **Trend tracking**: compare today's entities/topics to last 7 days to surface "this is the 3rd day this story is moving" signals. Useful if a topic is heating up.

## Troubleshooting

- **No items found** → check the time window in logs; if your `TZ` is wrong on a local run, the window may be off.
- **One source missing** → check the "Pipeline Notes" section at the bottom of the digest. Errors are surfaced there, not silently swallowed.
- **Bloomberg feed empty** → Google News RSS rate-limits aggressively. The fallback is to web-search Bloomberg's site manually for that day; rarely happens.
- **보안뉴스 RSS shape changed** → The `https://www.boannews.com/media/news_rss.xml` URL is what their site exposes; if they move it, update `config.yaml`.
