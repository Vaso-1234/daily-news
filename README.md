# The World Today

A tiny personal briefing site: the top five stories from **The Wall Street
Journal** and the top five from **the BBC**, each rewritten to about a hundred
words of natural prose with one or two pictures. Refreshed every hour by a
GitHub Actions cron so it stays current even when your laptop is off.

Once deployed you can bookmark
`https://<your-github-username>.github.io/daily-news/` and open it any time.

---

## How it works

```
GitHub Actions cron (hourly)
        |
        v
scripts/fetch_news.py
        |
        +--> BBC RSS feeds (Top, World, Business, Tech)
        +--> Bing News RSS (site:wsj.com)          <-- WSJ
        |
        v
   pick top 5 per source (freshest, deduped)
        |
        v
   for each item:
     - fetch the article page
     - grab OG description + first ~2 paragraphs
     - grab OG image
     - dedupe near-duplicate paragraphs
     - trim to ~100 words at a sentence boundary
     - apply the style transforms (no em/en dashes,
       sentence periods become '...', trailing '...')
        |
        v
   site/data.json
        |
        v
   git commit + push -> GitHub Pages redeploy
        |
        v
   your browser (also polls data.json every 5 min)
```

### Why Bing News for WSJ

The WSJ's own public RSS at `feeds.a.dj.com` has been serving multi-month
stale content in Sep 2026. Google News RSS returns opaque protobuf redirect
URLs that we can't easily follow. **Bing News RSS** returns real destination
URLs in its `apiclick.aspx?url=...` query param, which is trivial to unwrap,
so that's the WSJ source. If WSJ's direct feed recovers, swap the entries in
`FEEDS` inside `scripts/fetch_news.py`.

### The paywall + style

WSJ articles are paywalled, but the Open Graph meta tags (`og:description`,
`og:image`) are public so search engines can index the pages. That's what we
pull, plus whatever body paragraphs are visible before the paywall wall,
giving us 40-90 word summaries with a real image. BBC is fully open so its
summaries land closer to 100 words with two images each.

Style transforms applied to every summary:

- em-dashes, en-dashes, and " - " (spaced hyphens) become ", "
- sentence-ending periods become "..."
- every summary ends with a trailing "..."

---

## One-time setup

### 1. Push this folder to a new GitHub repo

```bash
cd ~/Desktop/daily-news
git init -b main
git add .
git commit -m "feat: initial daily news site"

# Create an empty repo on GitHub named "daily-news" (public, required for
# free GitHub Pages on a personal account), then:
git remote add origin git@github.com:<your-user>/daily-news.git
git push -u origin main
```

### 2. Enable GitHub Pages

Repo -> **Settings -> Pages**:
- Source: **Deploy from a branch**
- Branch: **`main`** / folder: **`/site`**
- Save

Pages will take about a minute to publish. The URL will be
`https://<your-user>.github.io/daily-news/`.

### 3. Give Actions permission to commit

Repo -> **Settings -> Actions -> General -> Workflow permissions**:
- Select **Read and write permissions**
- Save

Without this, the cron can fetch news but cannot push the updated
`data.json` back to the repo.

### 4. Seed the first run

Repo -> **Actions -> Refresh news -> Run workflow -> Run workflow**.

That commits a fresh `site/data.json`, which triggers Pages to redeploy.
After that the hourly cron takes over.

---

## Refresh cadence

Set in `.github/workflows/refresh-news.yml`:

```yaml
schedule:
  - cron: "0 * * * *"   # every hour, on the hour (UTC)
```

Notes:
- GitHub's scheduled workflows can be delayed 5 to 15 minutes under load.
- To go more frequent (minimum 5 min), change to e.g. `"*/15 * * * *"`.
- GitHub auto-disables scheduled workflows in repos with **60 days of no
  activity**. The bot's own commits count as activity, so this stays alive
  on its own.

---

## Tuning what shows up

All of these live at the top of [`scripts/fetch_news.py`](scripts/fetch_news.py):

- `TOP_PER_SOURCE = 5` : how many stories to keep per source
- `SUMMARY_TARGET_WORDS = 100` : the "about a hundred words" target
- `SUMMARY_HARD_MAX_WORDS = 120` : never exceed this
- `MAX_AGE_DAYS = 7` : drop anything older than a week (protects against
  stale-cache regressions like the Sep 2026 WSJ RSS outage)
- `FEEDS = [...]` : the list of feeds to pull. Add or remove entries here.

To add a third source (e.g. FT) via Bing News, add:

```python
FeedSpec("FT", "Top",
    "https://www.bing.com/news/search?q=site%3Aft.com&format=rss&count=25&freshness=day"),
```

---

## Running locally

```bash
cd ~/Desktop/daily-news
python3 -m venv .venv
.venv/bin/pip install -r scripts/requirements.txt
.venv/bin/python scripts/fetch_news.py

python3 -m http.server -d site 8000
# open http://localhost:8000
```

---

## File map

| Path | What it does |
| --- | --- |
| `scripts/fetch_news.py` | Fetches feeds, scrapes articles, composes summaries |
| `scripts/requirements.txt` | `feedparser`, `python-dateutil`, `requests`, `beautifulsoup4`, `lxml` |
| `.github/workflows/refresh-news.yml` | Hourly cron + auto-commit |
| `site/index.html` | Page shell |
| `site/styles.css` | Dark, magazine-style theme |
| `site/app.js` | Renders `data.json`, filters, client-side auto-poll |
| `site/data.json` | Generated payload (do not hand-edit) |
