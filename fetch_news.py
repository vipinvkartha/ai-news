#!/usr/bin/env python3
"""
Fetch latest AI news from RSS feeds + free public APIs (Hacker News, Reddit,
arXiv), dedupe, rank by engagement + recency, and write JSON data files.

Outputs:
  data/news-YYYY-MM-DD.json   (daily archive)
  data/latest.json            (most recent run, read by the website)

No API keys required. Configure sources in the CONFIG section below.
"""

import datetime as dt
import json
import os
import re
import sys
import time
from urllib.parse import urlparse

import feedparser
import requests

# --------------------------------------------------------------------------
# CONFIG -- edit these lists to add/remove sources
# --------------------------------------------------------------------------

# Plain RSS/Atom feeds. (title, url)
RSS_FEEDS = [
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/"),
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("MIT Tech Review AI", "https://www.technologyreview.com/topic/artificial-intelligence/feed"),
    ("Google AI Blog", "https://blog.google/technology/ai/rss/"),
    ("OpenAI Blog", "https://openai.com/news/rss.xml"),
    ("DeepMind Blog", "https://deepmind.google/blog/rss.xml"),
    ("Hugging Face Blog", "https://huggingface.co/blog/feed.xml"),
    ("MarkTechPost", "https://www.marktechpost.com/feed/"),
]

# Hacker News (Algolia API). Queries searched among recent front-page-ish stories.
HN_QUERIES = ["AI", "LLM", "machine learning", "OpenAI", "Anthropic", "GPT"]
HN_MIN_POINTS = 20          # ignore low-signal HN stories
HN_LOOKBACK_HOURS = 48

# Reddit subreddits (top of the day). No auth, just a custom User-Agent.
REDDIT_SUBS = ["MachineLearning", "artificial", "LocalLLaMA"]
REDDIT_MIN_UPS = 30

# arXiv categories (newest submissions).
ARXIV_CATEGORIES = ["cs.AI", "cs.LG", "cs.CL"]
ARXIV_MAX_PER_CAT = 10

# Ranking weights
ENGAGEMENT_WEIGHT = 10.0     # multiplies log2(points+1)
RECENCY_WEIGHT = 12.0        # max boost for a brand-new item
RECENCY_WINDOW_HOURS = 48    # how fast recency boost decays to 0

# How many items to keep in the final list
TOP_N = 60

USER_AGENT = "ai-news-blog/1.0 (https://github.com/)"
HTTP_TIMEOUT = 20

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

NOW = dt.datetime.now(dt.timezone.utc)


def log(*a):
    print(*a, file=sys.stderr)


def to_utc(struct_time):
    """Convert a feedparser time.struct_time (UTC) to aware datetime."""
    if not struct_time:
        return None
    try:
        return dt.datetime.fromtimestamp(time.mktime(struct_time), dt.timezone.utc)
    except Exception:
        return None


def hours_old(when):
    if not when:
        return 9999.0
    return max(0.0, (NOW - when).total_seconds() / 3600.0)


def recency_boost(when):
    h = hours_old(when)
    if h >= RECENCY_WINDOW_HOURS:
        return 0.0
    return RECENCY_WEIGHT * (1.0 - h / RECENCY_WINDOW_HOURS)


def engagement_score(points):
    import math
    return ENGAGEMENT_WEIGHT * math.log2((points or 0) + 1)


def norm_title(title):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", (title or "").lower())).strip()


def norm_url(url):
    try:
        p = urlparse(url)
        return (p.netloc.lower().replace("www.", "") + p.path.rstrip("/")).lower()
    except Exception:
        return (url or "").lower()


def make_item(title, url, source, published, points=0, tags=None):
    return {
        "title": (title or "").strip(),
        "url": url,
        "source": source,
        "published_at": published.isoformat() if published else None,
        "points": int(points or 0),
        "tags": tags or [],
    }


# --------------------------------------------------------------------------
# Fetchers (each is best-effort; failures are logged and skipped)
# --------------------------------------------------------------------------

def fetch_rss():
    items = []
    for source, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": USER_AGENT})
            for e in feed.entries[:15]:
                published = to_utc(getattr(e, "published_parsed", None)) or \
                            to_utc(getattr(e, "updated_parsed", None))
                items.append(make_item(
                    e.get("title"), e.get("link"), source, published,
                    points=0, tags=["news"]))
            log(f"[rss] {source}: {len(feed.entries)} entries")
        except Exception as ex:
            log(f"[rss] {source} FAILED: {ex}")
    return items


def fetch_hn():
    items = []
    cutoff = int((NOW - dt.timedelta(hours=HN_LOOKBACK_HOURS)).timestamp())
    for q in HN_QUERIES:
        try:
            r = requests.get(
                "https://hn.algolia.com/api/v1/search",
                params={
                    "query": q,
                    "tags": "story",
                    "numericFilters": f"created_at_i>{cutoff},points>={HN_MIN_POINTS}",
                    "hitsPerPage": 20,
                },
                headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            for h in r.json().get("hits", []):
                url = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
                published = dt.datetime.fromtimestamp(h["created_at_i"], dt.timezone.utc) \
                    if h.get("created_at_i") else None
                items.append(make_item(
                    h.get("title"), url, "Hacker News", published,
                    points=h.get("points", 0), tags=["hn", f"q:{q}"]))
            log(f"[hn] '{q}': ok")
        except Exception as ex:
            log(f"[hn] '{q}' FAILED: {ex}")
    return items


def fetch_reddit():
    """Try the JSON API first (gives upvote counts). On failure -- common from
    cloud/datacenter IPs like GitHub Actions, which Reddit blocks with 403 --
    fall back to the .rss feed, which is more tolerant but has no vote counts
    (those items are then ranked by recency only)."""
    items = []
    for sub in REDDIT_SUBS:
        try:
            r = requests.get(
                f"https://www.reddit.com/r/{sub}/top.json",
                params={"t": "day", "limit": 25},
                headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            for child in r.json().get("data", {}).get("children", []):
                d = child.get("data", {})
                ups = d.get("ups", 0)
                if ups < REDDIT_MIN_UPS:
                    continue
                published = dt.datetime.fromtimestamp(d["created_utc"], dt.timezone.utc) \
                    if d.get("created_utc") else None
                permalink = "https://www.reddit.com" + d.get("permalink", "")
                items.append(make_item(
                    d.get("title"), d.get("url") or permalink, f"r/{sub}", published,
                    points=ups, tags=["reddit"]))
            log(f"[reddit] r/{sub}: ok (json)")
        except Exception as ex:
            log(f"[reddit] r/{sub} json failed ({ex}); trying rss")
            try:
                feed = feedparser.parse(
                    f"https://www.reddit.com/r/{sub}/top/.rss?t=day",
                    request_headers={"User-Agent": USER_AGENT})
                for e in feed.entries[:25]:
                    published = to_utc(getattr(e, "published_parsed", None)) or \
                                to_utc(getattr(e, "updated_parsed", None))
                    items.append(make_item(
                        e.get("title"), e.get("link"), f"r/{sub}", published,
                        points=0, tags=["reddit"]))
                log(f"[reddit] r/{sub}: ok (rss, {len(feed.entries)} entries)")
            except Exception as ex2:
                log(f"[reddit] r/{sub} FAILED: {ex2}")
    return items


def fetch_arxiv():
    items = []
    for cat in ARXIV_CATEGORIES:
        try:
            url = ("http://export.arxiv.org/api/query?search_query=cat:"
                   f"{cat}&sortBy=submittedDate&sortOrder=descending"
                   f"&max_results={ARXIV_MAX_PER_CAT}")
            feed = feedparser.parse(url)
            for e in feed.entries:
                published = to_utc(getattr(e, "published_parsed", None))
                items.append(make_item(
                    e.get("title"), e.get("link"), f"arXiv {cat}", published,
                    points=0, tags=["paper", cat]))
            log(f"[arxiv] {cat}: {len(feed.entries)} entries")
        except Exception as ex:
            log(f"[arxiv] {cat} FAILED: {ex}")
    return items


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------

def dedupe(items):
    seen_url, seen_title, out = set(), set(), []
    # Sort so the higher-engagement copy of a dupe wins.
    for it in sorted(items, key=lambda x: x["points"], reverse=True):
        if not it["title"] or not it["url"]:
            continue
        u, t = norm_url(it["url"]), norm_title(it["title"])
        if u in seen_url or (t and t in seen_title):
            continue
        seen_url.add(u)
        if t:
            seen_title.add(t)
        out.append(it)
    return out


def rank(items):
    for it in items:
        when = dt.datetime.fromisoformat(it["published_at"]) if it["published_at"] else None
        it["score"] = round(engagement_score(it["points"]) + recency_boost(when), 3)
        it["hours_old"] = round(hours_old(when), 1)
    return sorted(items, key=lambda x: x["score"], reverse=True)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(here, "data")
    os.makedirs(data_dir, exist_ok=True)

    items = []
    items += fetch_rss()
    items += fetch_hn()
    items += fetch_reddit()
    items += fetch_arxiv()
    log(f"collected {len(items)} raw items")

    items = dedupe(items)
    items = rank(items)[:TOP_N]
    log(f"kept {len(items)} after dedupe+rank")

    payload = {
        "generated_at": NOW.isoformat(),
        "count": len(items),
        "items": items,
    }

    today = NOW.strftime("%Y-%m-%d")
    with open(os.path.join(data_dir, f"news-{today}.json"), "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    with open(os.path.join(data_dir, "latest.json"), "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    log(f"wrote data/news-{today}.json and data/latest.json")


if __name__ == "__main__":
    main()
