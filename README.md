# AI Watch

Automated daily digest of news on **trustworthy AI**, **responsible AI**, **AI privacy**, and **machine unlearning**, pulled from WSJ, Bloomberg, The Markup, TechCrunch, and 보안뉴스 (Boannews).

Runs every day at 10:00 KST. Window: yesterday 10:00 → today 10:00 KST.

## What it actually does

```
┌──────────┐   ┌──────────┐   ┌────────┐   ┌──────────┐   ┌─────────┐   ┌────────┐   ┌─────────┐
│  Fetch   │ → │  Window  │ → │ Keyword│ → │ Classify │ → │ Extract │ → │Summary │ → │ Cluster │ → render → notify
│  RSS×5   │   │  filter  │   │  regex │   │  (LLM)   │   │ full    │   │ (LLM)  │   │  (LLM)  │
│          │   │          │   │ (EN+KO)│   │  stage 1 │   │ text    │   │ stage 2│   │ stage 3 │
└──────────┘   └──────────┘   └────────┘   └──────────┘   └─────────┘   └────────┘   └─────────┘
```

Two LLM stages because precision matters: a cheap classify call kills 90%+ of false positives, then a full summarize pass runs only on what survives. Final clustering pass groups the same story showing up in multiple outlets into one item.

WSJ + Bloomberg are paywalled — the script doesn't try to bypass that. They're surfaced as **headline + RSS blurb** in a separate "Paywalled Headlines" section so you can decide whether to open them.

## Output format

Each digest is `digests/YYYY-MM-DD.md`:

```
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
```

## Setup (macOS)

This guide assumes macOS + launchd for scheduling. If you're on Linux, swap launchd for cron — the Python side is identical.

### 1. Clone and install

```bash
git clone https://github.com/YOUR_USERNAME/ai-watch.git
cd ai-watch
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# 주석: lxml 호환성을 위해 추가 패키지가 필요할 수 있습니다
pip install lxml_html_clean
```

### 2. Get a Gemini API key

Free tier — no credit card needed. Get a key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey). Click "Create API key" → "Create API key in new project."

### 3. Create `.env`

```bash
cp .env.example .env
nano .env
```

Fill in:

```
GEMINI_API_KEY=your-gemini-api-key-here
```

If you also want email delivery (optional), add SMTP credentials. Otherwise leave them blank and set `email.enabled: false` in `config.yaml`.


### 4. Edit `config.yaml`

Set your email address if using email delivery, or leave defaults if not. The default config has email disabled.

### 5. Test it manually

```bash
python3 main.py
```

This runs once and writes `digests/YYYY-MM-DD.md`. Look at the file — if it has real article summaries, you're good. The first run takes ~4 minutes due to Gemini free tier rate limits (10 req/min).

### 6. Schedule with launchd (macOS)

Create `~/Library/LaunchAgents/com.YOURNAME.aiwatch.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.YOURNAME.aiwatch</string>

    <key>ProgramArguments</key>
    <array>
        <!-- Change to your venv Python path -->
        <string>/path/to/your/ai-watch/.venv/bin/python</string>
        <!-- Change to absolute path of your main.py -->
        <string>/path/to/your/ai-watch/main.py</string>
    </array>

    <key>WorkingDirectory</key>
    <!--Absolute path to your project directory -->
    <string>/path/to/your/ai-watch</string>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>10</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>/path/to/your/ai-watch/launchd.log</string>

    <key>StandardErrorPath</key>
    <string>/path/to/your/ai-watch/launchd.error.log</string>

    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
```

Load it:

```bash
launchctl load ~/Library/LaunchAgents/com.YOURNAME.aiwatch.plist
```

Verify:

```bash
launchctl list | grep aiwatch
```

Test by triggering immediately:

```bash
launchctl start com.YOURNAME.aiwatch
```

### 7. (Optional) macOS notifications when the digest is ready

If you want a banner notification when each run finishes, install:

```bash
brew install terminal-notifier
```

Without `terminal-notifier`, the script falls back to `osascript`, which doesn't reliably work from launchd-triggered background processes.

## Running on Linux (alternative to macOS)

Same Python setup. For scheduling, use cron instead of launchd:

```bash
crontab -e
```


## Tuning quality

The prompts in `main.py` are where quality lives.

- **Too much noise?** Edit `CLASSIFY_SYSTEM` to be stricter. The current prompt rejects vendor PR and product launches; you can extend the reject list.
- **Missing important stories?** Loosen the keyword regex (`KEYWORDS_EN` / `KEYWORDS_KO`) — the keyword filter is an OR with the LLM classifier, so broader keywords just give the LLM more candidates, not more output.
- **Want longer/shorter summaries?** Edit `SUMMARIZE_USER` and bump/cut `max_tokens` in `summarize()`.
- **Care more about a specific category?** The classifier already prioritizes machine unlearning. To weight others, edit the importance heuristic in `render_digest`.

## Cost

**Free** — uses Gemini's free API tier (no credit card needed).

Free tier limits on `gemini-2.5-flash` are around 10 req/min and ~250 req/day, well within what one daily run uses (~30-60 calls).


## Worth adding later

- **arXiv feed**: cs.LG + privacy/unlearning keywords. Catches papers ~1 week before journalists cover them. Add as another source with `paywalled: false`.
- **Embedding-based dedup** instead of LLM clustering — faster and cheaper if you ever process >100 relevant items/day. Not needed at current volume.
- **Trend tracking**: compare today's entities/topics to last 7 days to surface "this is the 3rd day this story is moving" signals.

## Troubleshooting

- **No articles found** → check the time window in logs; if your `TZ` is wrong, the window may be off. Verify with `date` on your server.
- **All classify calls hit 429** → Gemini quota issue. Either wait for daily reset (midnight Pacific time) or swap `MODEL_FAST` to a different model in `main.py`.
- **`limit: 0` errors** → your Google account isn't getting free tier (often happens when billing has ever touched the account). Try a fresh Google account, or switch to a different LLM provider.
- **One source missing** → check the "Pipeline Notes" section at the bottom of the digest. Errors are surfaced there, not silently swallowed.
- **launchd job runs but no notification** → install `terminal-notifier` (see Setup step 7). `osascript`-based notifications don't work reliably from launchd background processes.
- **보안뉴스 RSS shape changed** → the RSS URL in `config.yaml` may need updating if Boannews moves it.

## License

MIT — do whatever you want with it.
