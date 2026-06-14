# 🤖 AI News — Daily

A zero-cost, no-API-key AI news aggregator. A scheduled job fetches the latest
AI stories from RSS feeds and free public APIs, ranks them by **engagement +
recency**, and writes static JSON. A static site reads that JSON and renders a
clean, filterable list. Everything runs on free GitHub infrastructure.

## How it works

```
GitHub Actions (daily cron)
        │  runs fetch_news.py
        ▼
data/latest.json  +  data/news-YYYY-MM-DD.json   (committed back to the repo)
        ▲
        │  fetch()
GitHub Pages  ──►  index.html  (static site)
```

No servers, no databases, no API keys.

## Sources

- **RSS/Atom**: TechCrunch, VentureBeat, The Verge, MIT Tech Review, Google AI,
  OpenAI, DeepMind, Hugging Face, MarkTechPost
- **Hacker News** (Algolia API) — uses story points as the engagement signal
- **Reddit** — r/MachineLearning, r/artificial, r/LocalLLaMA (top of the day)
- **arXiv** — cs.AI, cs.LG, cs.CL (newest submissions)

Edit the `CONFIG` section at the top of `fetch_news.py` to add or remove sources
and tune thresholds.

## Ranking

Each story gets a score:

```
score = ENGAGEMENT_WEIGHT * log2(points + 1) + recency_boost
```

- `points` = HN points / Reddit upvotes (0 for plain RSS/arXiv items)
- `recency_boost` decays linearly to 0 over `RECENCY_WINDOW_HOURS` (48h)

So highly-upvoted stories rank near the top, while fresh news still surfaces.
Weights live in `fetch_news.py`.

## Run locally

```bash
pip install -r requirements.txt
python fetch_news.py
# then serve the folder and open index.html:
python -m http.server 8000
# visit http://localhost:8000
```

(The site must be served over HTTP, not opened as a `file://` path, or the
`fetch()` for `data/latest.json` will be blocked by the browser.)

## Deploy (one-time setup)

1. Create a GitHub repo and push this folder.
2. **Settings → Pages** → Source: *Deploy from a branch* → `main` / root.
3. **Settings → Actions → General** → Workflow permissions →
   *Read and write permissions* (lets the workflow commit data back).
4. **Actions** tab → run **Fetch AI News** once via *Run workflow* to seed data.

After that it updates itself every day at 06:00 UTC. Change the time by editing
the `cron` line in `.github/workflows/fetch-news.yml`.

## Data format

`data/latest.json`:

```json
{
  "generated_at": "2026-06-14T06:00:00+00:00",
  "count": 60,
  "items": [
    {
      "title": "…",
      "url": "https://…",
      "source": "Hacker News",
      "published_at": "2026-06-14T03:12:00+00:00",
      "points": 342,
      "tags": ["hn", "q:AI"],
      "score": 78.1,
      "hours_old": 2.8
    }
  ]
}
```

## Possible future improvements

- LLM pass to dedupe near-identical stories and write one-line summaries
- Category pages / search
- Email or RSS digest of the top N
- Per-source weighting
