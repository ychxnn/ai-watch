#!/usr/bin/env python3
"""
AI Watch — Daily Digest

Pipeline:
  1. Pull RSS / scrape from each source
  2. Filter to the time window (yesterday 10:00 → today 10:00 KST)
  3. Cheap keyword pre-filter (multilingual)
  4. LLM relevance classifier (Claude)  ── stage 1
  5. Full-text fetch for non-paywalled sources
  6. LLM summarizer + categorizer        ── stage 2
  7. Cluster / dedupe across sources     ── stage 3
  8. Rank top stories                    ── stage 4
  9. Format markdown digest
 10. Deliver (file always; email/Slack if configured)

Run:  python main.py
Config:  config.yaml
Secrets:  .env  (or environment variables)
"""

from __future__ import annotations

import json
import logging
import os
import re
import smtplib
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import feedparser
import requests
import trafilatura
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai
from google.genai import types

# ──────────────────────────────────────────────────────────────────────────
# Setup
# ──────────────────────────────────────────────────────────────────────────

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("ai-watch")

ROOT = Path(__file__).parent
KST = ZoneInfo("Asia/Seoul")

GEMINI = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
# Free tier on aistudio.google.com — both calls use Flash; well within free limits.
MODEL_FAST = "gemini-2.5-flash-lite"
MODEL_SMART = "gemini-2.5-flash-lite"

USER_AGENT = (
    # 주석: GitHub URL을 본인 저장소 URL로 변경하세요 (선택 사항)
    # NOTE: change this to your own GitHub repo URL (optional)
    "Mozilla/5.0 (compatible; AI-Watch/1.0; +https://github.com/YOUR_USERNAME/ai-watch)"
)


def gemini_json(model: str, system: str, user: str, max_tokens: int = 600) -> dict:
    """Call Gemini and return parsed JSON. Uses Gemini's native JSON mode."""
    response = GEMINI.models.generate_content(
        model=model,
        contents=user,
        config=types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            max_output_tokens=max_tokens,
            temperature=0.2,
        ),
    )
    return json.loads(response.text)


# ──────────────────────────────────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class Article:
    source: str
    title: str
    url: str
    published: datetime
    description: str = ""
    full_text: str = ""
    paywalled: bool = False

    # filled in by pipeline
    relevant: bool = False
    category: str = ""
    importance: int = 0
    reason: str = ""
    summary: str = ""
    why_it_matters: str = ""
    entities: list[str] = field(default_factory=list)
    cluster_id: int = -1

    @property
    def key(self) -> str:
        return self.url.split("?")[0].split("#")[0]


# ──────────────────────────────────────────────────────────────────────────
# Source fetchers
# ──────────────────────────────────────────────────────────────────────────


def parse_feed_date(entry) -> Optional[datetime]:
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def fetch_rss(source_name: str, feed_url: str, paywalled: bool) -> list[Article]:
    log.info(f"[{source_name}] fetching RSS: {feed_url}")
    parsed = feedparser.parse(feed_url, agent=USER_AGENT)
    if parsed.bozo and not parsed.entries:
        log.warning(f"[{source_name}] feed parse warning: {parsed.bozo_exception}")

    out: list[Article] = []
    for e in parsed.entries:
        pub = parse_feed_date(e)
        if pub is None:
            continue
        title = (e.get("title") or "").strip()
        url = (e.get("link") or "").strip()
        desc = strip_html(e.get("summary") or e.get("description") or "")
        if not title or not url:
            continue
        out.append(
            Article(
                source=source_name,
                title=title,
                url=url,
                published=pub,
                description=desc[:600],
                paywalled=paywalled,
            )
        )
    log.info(f"[{source_name}] {len(out)} entries from feed")
    return out


def strip_html(html: str) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


def extract_full_text(url: str) -> str:
    """Fetch and clean article body. Returns empty string on failure."""
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
        r.raise_for_status()
        text = trafilatura.extract(
            r.text,
            include_comments=False,
            include_tables=False,
            favor_recall=True,
        )
        return (text or "").strip()
    except Exception as exc:
        log.warning(f"extract failed for {url}: {exc}")
        return ""


# ──────────────────────────────────────────────────────────────────────────
# Time window
# ──────────────────────────────────────────────────────────────────────────


def time_window(now_kst: datetime) -> tuple[datetime, datetime]:
    """Returns (start, end) in UTC. Window = yesterday 10:00 KST → today 10:00 KST."""
    today_10 = now_kst.replace(hour=10, minute=0, second=0, microsecond=0)
    if now_kst < today_10:
        today_10 -= timedelta(days=1)
    yesterday_10 = today_10 - timedelta(days=1)
    return (
        yesterday_10.astimezone(timezone.utc),
        today_10.astimezone(timezone.utc),
    )


# ──────────────────────────────────────────────────────────────────────────
# Keyword pre-filter (broad — catches candidates, LLM does precision)
# ──────────────────────────────────────────────────────────────────────────

KEYWORDS_EN = [
    r"\bAI\b", r"\bA\.I\.\b", r"artificial intelligence",
    r"machine learning", r"\bLLM\b", r"large language model",
    r"machine unlearning", r"\bunlearning\b",
    r"responsible AI", r"trustworthy AI", r"AI safety",
    r"AI ethics", r"AI governance", r"AI regulation", r"AI policy",
    r"data privacy", r"data protection", r"\bGDPR\b", r"\bCCPA\b",
    r"AI privacy", r"model privacy", r"differential privacy",
    r"deepfake", r"AI bias", r"algorithmic", r"facial recognition",
    r"surveillance", r"AI act",
]

KEYWORDS_KO = [
    "인공지능", "기계학습", "머신러닝", "딥러닝",
    "기계 언러닝", "언러닝", "기계 학습 해제",
    "신뢰 가능한 AI", "신뢰할 수 있는 AI", "책임 있는 AI", "책임감 있는 AI",
    "AI 윤리", "AI 거버넌스", "AI 규제",
    "개인정보", "프라이버시", "정보보호",
    "딥페이크", "안면 인식", "얼굴 인식",
]

KW_RE = re.compile(
    "|".join(KEYWORDS_EN + [re.escape(k) for k in KEYWORDS_KO]),
    re.IGNORECASE,
)


def keyword_match(article: Article) -> bool:
    blob = f"{article.title}\n{article.description}"
    return bool(KW_RE.search(blob))


# ──────────────────────────────────────────────────────────────────────────
# LLM stages
# ──────────────────────────────────────────────────────────────────────────

CLASSIFY_SYSTEM = """You filter news for a digest focused on:
- Machine unlearning (HIGHEST priority — surface anything that touches it)
- AI privacy (training data, model leakage, surveillance, biometrics)
- Trustworthy AI / AI safety / alignment
- Responsible AI / governance / regulation / policy

REJECT: vendor product launches, funding rounds, generic AI hype, model benchmarks
ACCEPT: research, lawsuits, regulation, leaks, breaches, policy, audits, harm reports, governance debates

Be strict. Better to drop a borderline item than dilute the digest."""

CLASSIFY_USER = """Source: {source}
Title: {title}
Description: {description}

Return ONLY JSON:
{{"relevant": true|false, "category": "unlearning|privacy|trust_safety|governance|other", "importance": 1-5, "reason": "<one sentence>"}}"""


def classify(article: Article) -> None:
    user = CLASSIFY_USER.format(
        source=article.source,
        title=article.title,
        description=article.description[:400],
    )
    try:
        data = gemini_json(MODEL_FAST, CLASSIFY_SYSTEM, user, max_tokens=300)
        article.relevant = bool(data.get("relevant"))
        article.category = data.get("category", "other")
        article.importance = int(data.get("importance", 0))
        article.reason = data.get("reason", "")
    except Exception as exc:
        log.warning(f"classify failed for {article.url}: {exc}")
        article.relevant = False


SUMMARIZE_SYSTEM = """You summarize news for an AI policy/research practitioner.
Style: substantive, specific, no marketing fluff. Lead with the concrete development.
Include numbers, names, jurisdictions, and policy mechanisms when present.
Output English. If source is Korean, preserve key Korean terminology in (parens)."""

SUMMARIZE_USER = """Source: {source}
Title: {title}
URL: {url}

Article text:
{text}

Return ONLY JSON:
{{
  "summary": "<3-4 sentences, substantive>",
  "why_it_matters": "<1 sentence on implication for AI trust/privacy/governance>",
  "entities": ["<key org/person/law>", ...],
  "category": "unlearning|privacy|trust_safety|governance|other",
  "importance": 1-5
}}"""


def summarize(article: Article) -> None:
    text = article.full_text or article.description
    if not text:
        return
    user = SUMMARIZE_USER.format(
        source=article.source,
        title=article.title,
        url=article.url,
        text=text[:8000],
    )
    try:
        data = gemini_json(MODEL_SMART, SUMMARIZE_SYSTEM, user, max_tokens=800)
        article.summary = data.get("summary", "")
        article.why_it_matters = data.get("why_it_matters", "")
        article.entities = data.get("entities", [])[:6]
        article.category = data.get("category", article.category)
        article.importance = max(article.importance, int(data.get("importance", 0)))
    except Exception as exc:
        log.warning(f"summarize failed for {article.url}: {exc}")


CLUSTER_SYSTEM = """You group news articles that cover the same underlying story.
Articles about the same lawsuit, same regulation, same incident → same cluster.
Articles on related-but-distinct stories → separate clusters."""

CLUSTER_USER = """Articles:
{listing}

Return ONLY JSON:
{{"clusters": [[<article_indices>], ...]}}
Every article index must appear in exactly one cluster."""


def cluster(articles: list[Article]) -> None:
    if len(articles) <= 1:
        for i, a in enumerate(articles):
            a.cluster_id = i
        return
    listing = "\n".join(
        f"[{i}] ({a.source}) {a.title}" for i, a in enumerate(articles)
    )
    try:
        data = gemini_json(
            MODEL_FAST,
            CLUSTER_SYSTEM,
            CLUSTER_USER.format(listing=listing),
            max_tokens=600,
        )
        for cid, indices in enumerate(data.get("clusters", [])):
            for i in indices:
                if 0 <= i < len(articles):
                    articles[i].cluster_id = cid
    except Exception as exc:
        log.warning(f"cluster failed: {exc}")
        for i, a in enumerate(articles):
            a.cluster_id = i

    # any unassigned → singleton
    next_id = max((a.cluster_id for a in articles), default=-1) + 1
    for a in articles:
        if a.cluster_id < 0:
            a.cluster_id = next_id
            next_id += 1


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


def extract_json(text: str) -> dict:
    """Pull the first {...} block out of LLM output, tolerating ``` fences."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError(f"no JSON object in: {text[:200]}")
    return json.loads(text[start : end + 1])


def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"sent": []}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ──────────────────────────────────────────────────────────────────────────
# Formatter
# ──────────────────────────────────────────────────────────────────────────

CATEGORY_ORDER = ["unlearning", "privacy", "trust_safety", "governance", "other"]
CATEGORY_LABEL = {
    "unlearning": "🧠 Machine Unlearning",
    "privacy": "🔐 AI Privacy",
    "trust_safety": "🛡️  Trust & Safety",
    "governance": "⚖️  Governance & Policy",
    "other": "📌 Other",
}


def fmt_kst(dt: datetime) -> str:
    return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")


def render_digest(
    articles: list[Article],
    paywalled_headlines: list[Article],
    window_start: datetime,
    window_end: datetime,
    scraped_at: datetime,
    stats: dict,
    errors: list[str],
) -> str:
    lines: list[str] = []
    lines.append("# AI Watch — Daily Digest")
    lines.append("")
    lines.append(f"**Window:** {fmt_kst(window_start)} → {fmt_kst(window_end)}  ")
    lines.append(f"**Generated:** {fmt_kst(scraped_at)}  ")
    lines.append(
        f"**Sources scanned:** {stats['sources']} · "
        f"**Articles seen:** {stats['seen']} · "
        f"**After keyword filter:** {stats['kw']} · "
        f"**Relevant:** {stats['relevant']} · "
        f"**Story clusters:** {stats['clusters']}"
    )
    lines.append("")

    relevant = [a for a in articles if a.relevant]

    # ─── Top stories ─────────────────────────────────────────────────────
    top = sorted(relevant, key=lambda a: (-a.importance, a.source))[:3]
    if top:
        lines.append("## 🔴 Top Stories")
        lines.append("")
        for a in top:
            lines.append(f"### {a.title}")
            lines.append(
                f"_{a.source} · {fmt_kst(a.published)} · "
                f"importance {a.importance}/5 · {a.category}_"
            )
            lines.append("")
            if a.summary:
                lines.append(a.summary)
                lines.append("")
            if a.why_it_matters:
                lines.append(f"**Why it matters:** {a.why_it_matters}")
                lines.append("")
            if a.entities:
                lines.append(f"**Entities:** {', '.join(a.entities)}")
                lines.append("")
            lines.append(f"[Read →]({a.url})")
            lines.append("")
            lines.append("---")
            lines.append("")

    # ─── By category ─────────────────────────────────────────────────────
    by_cat: dict[str, list[Article]] = {c: [] for c in CATEGORY_ORDER}
    top_urls = {a.url for a in top}
    seen_clusters: set[int] = set()
    for a in sorted(relevant, key=lambda a: -a.importance):
        if a.url in top_urls or a.cluster_id in seen_clusters:
            continue
        seen_clusters.add(a.cluster_id)
        by_cat.setdefault(a.category, []).append(a)

    has_more = any(by_cat[c] for c in by_cat)
    if has_more:
        lines.append("## 📂 By Category")
        lines.append("")
        for cat in CATEGORY_ORDER:
            items = by_cat.get(cat, [])
            if not items:
                continue
            lines.append(f"### {CATEGORY_LABEL[cat]}")
            lines.append("")
            for a in items:
                lines.append(f"**[{a.title}]({a.url})**")
                lines.append(
                    f"_{a.source} · {fmt_kst(a.published)} · importance {a.importance}/5_"
                )
                lines.append("")
                if a.summary:
                    lines.append(a.summary)
                    lines.append("")
                if a.why_it_matters:
                    lines.append(f"_Why it matters:_ {a.why_it_matters}")
                    lines.append("")
            lines.append("")

    # ─── Paywalled headlines ─────────────────────────────────────────────
    if paywalled_headlines:
        lines.append("## 🔒 Paywalled Headlines")
        lines.append("")
        lines.append("_Headlines only — bodies behind paywall._")
        lines.append("")
        for a in sorted(paywalled_headlines, key=lambda a: a.source):
            tag = f"`{a.category}`" if a.category and a.category != "other" else ""
            lines.append(f"- **[{a.source}]** [{a.title}]({a.url}) {tag}")
            if a.description:
                lines.append(f"  > {a.description[:200]}")
        lines.append("")

    # ─── Errors ──────────────────────────────────────────────────────────
    if errors:
        lines.append("## ⚠️  Pipeline Notes")
        lines.append("")
        for e in errors:
            lines.append(f"- {e}")
        lines.append("")

    if not relevant and not paywalled_headlines:
        lines.append("_No relevant articles in this window._")
        lines.append("")

    lines.append("---")
    lines.append(f"_AI Watch · {fmt_kst(scraped_at)}_")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────
# Delivery
# ──────────────────────────────────────────────────────────────────────────


def send_email(cfg: dict, subject: str, body_md: str) -> None:
    if not cfg.get("enabled"):
        return
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    pwd = os.environ.get("SMTP_PASSWORD")
    if not all([host, user, pwd]):
        log.warning("email enabled but SMTP creds missing — skipping")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg.get("from", user)
    msg["To"] = cfg["to"]
    msg.attach(MIMEText(body_md, "plain", "utf-8"))
    msg.attach(MIMEText(md_to_html(body_md), "html", "utf-8"))

    with smtplib.SMTP(host, port) as s:
        s.starttls()
        s.login(user, pwd)
        s.send_message(msg)
    log.info(f"email sent → {cfg['to']}")


def md_to_html(md: str) -> str:
    """Tiny markdown → HTML; good enough for email."""
    try:
        import markdown  # type: ignore
        return markdown.markdown(md, extensions=["extra"])
    except ImportError:
        return f"<pre style='font-family:ui-monospace,monospace;white-space:pre-wrap'>{md}</pre>"


def send_slack(webhook_url: str, body_md: str) -> None:
    if not webhook_url:
        return
    try:
        requests.post(webhook_url, json={"text": body_md[:39000]}, timeout=10)
        log.info("slack delivered")
    except Exception as exc:
        log.warning(f"slack failed: {exc}")


def send_mac_notification(title: str, message: str, digest_path: Path) -> None:
    """Pop a native macOS banner. Click does nothing fancy — just dismisses.
    To actually open the digest, the user runs: open <digest_path>"""
    import subprocess
    # Escape double quotes for AppleScript
    safe_title = title.replace('"', '\\"')
    safe_msg = message.replace('"', '\\"')
    script = (
        f'display notification "{safe_msg}" '
        f'with title "{safe_title}" '
        f'sound name "Glass"'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=False, timeout=5, capture_output=True,
        )
        log.info("mac notification sent")
    except Exception as exc:
        log.warning(f"mac notification failed: {exc}")


# ──────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────


def main() -> int:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    state_path = ROOT / "state" / "sent.json"
    state_path.parent.mkdir(exist_ok=True)
    state = load_state(state_path)
    sent_urls: set[str] = set(state.get("sent", []))

    now_kst = datetime.now(KST)
    win_start, win_end = time_window(now_kst)
    log.info(f"window UTC: {win_start.isoformat()} → {win_end.isoformat()}")

    errors: list[str] = []
    raw: list[Article] = []

    # 1. fetch
    for src in cfg["sources"]:
        try:
            for feed_url in src["feeds"]:
                raw += fetch_rss(src["name"], feed_url, src.get("paywalled", False))
        except Exception as exc:
            errors.append(f"{src['name']}: fetch failed — {exc}")
            log.error(errors[-1])

    seen = len(raw)

    # 2. window + dedupe by URL + remove already-sent
    in_window = [
        a for a in raw
        if win_start <= a.published <= win_end and a.key not in sent_urls
    ]
    by_url: dict[str, Article] = {}
    for a in in_window:
        if a.key not in by_url:
            by_url[a.key] = a
    in_window = list(by_url.values())
    log.info(f"in window: {len(in_window)}")

    # 3. keyword pre-filter
    candidates = [a for a in in_window if keyword_match(a)]
    log.info(f"after keyword filter: {len(candidates)}")

    # 4. LLM classify
    for a in candidates:
        try:
            classify(a)
            time.sleep(6.5)  # Gemini free tier: 10 req/min on flash-lite → ~9/min safe
        except Exception as exc:
            errors.append(f"classify error on {a.url}: {exc}")

    relevant = [a for a in candidates if a.relevant]
    log.info(f"relevant: {len(relevant)}")

    # 5. fetch full text for non-paywalled, then summarize
    for a in relevant:
        if not a.paywalled:
            a.full_text = extract_full_text(a.url)
        try:
            summarize(a)
            time.sleep(6.5)  # Gemini free tier: 10 req/min on flash → ~9/min safe
        except Exception as exc:
            errors.append(f"summarize error on {a.url}: {exc}")

    # 6. cluster
    try:
        cluster(relevant)
    except Exception as exc:
        errors.append(f"cluster error: {exc}")

    # 7. split paywalled headlines
    paywalled = [a for a in relevant if a.paywalled]
    full = [a for a in relevant if not a.paywalled]

    # 8. render
    scraped_at = datetime.now(timezone.utc)
    stats = {
        "sources": len(cfg["sources"]),
        "seen": seen,
        "kw": len(candidates),
        "relevant": len(relevant),
        "clusters": len({a.cluster_id for a in full}) if full else 0,
    }
    digest = render_digest(
        full, paywalled, win_start, win_end, scraped_at, stats, errors
    )

    # 9. write file
    out_dir = ROOT / "digests"
    out_dir.mkdir(exist_ok=True)
    fname = now_kst.strftime("%Y-%m-%d") + ".md"
    out_path = out_dir / fname
    out_path.write_text(digest, encoding="utf-8")
    log.info(f"wrote {out_path}")

    # 10. deliver
    subject = f"AI Watch — {now_kst.strftime('%Y-%m-%d')} ({len(relevant)} items)"
    try:
        send_email(cfg.get("email", {}), subject, digest)
    except Exception as exc:
        log.error(f"email failed: {exc}")
    slack_url = os.environ.get("SLACK_WEBHOOK_URL", "")
    if slack_url and cfg.get("slack", {}).get("enabled"):
        send_slack(slack_url, digest)

    # macOS notification (always on; harmless if not on a Mac)
    if cfg.get("mac_notification", {}).get("enabled", True):
        if relevant:
            cats = sorted({a.category for a in relevant})
            msg = f"{len(relevant)} items · {', '.join(cats)}"
        else:
            msg = "No relevant articles in this window."
        send_mac_notification(
            f"AI Watch — {now_kst.strftime('%b %d')}",
            msg,
            out_path,
        )

    # 11. update state
    state["sent"] = list(sent_urls | {a.key for a in relevant})[-5000:]
    state["last_run"] = scraped_at.isoformat()
    save_state(state_path, state)

    return 0


if __name__ == "__main__":
    sys.exit(main())
