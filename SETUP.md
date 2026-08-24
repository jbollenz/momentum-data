# Setup — GitHub website only, no tools needed

Follow these steps in your browser. Total time: about 15 minutes.

## 1. Create the repository

1. Go to [github.com](https://github.com) and sign in (create a free
   account first if you don't have one).
2. Click the **+** in the top-right corner → **New repository**.
3. Repository name: `momentum-data`. Visibility: **Public** (the dashboard
   reads the files over public raw URLs). Tick **Add a README file**.
4. Click **Create repository**.

## 2. Create the two files inside `.github` (must be typed, not dragged)

GitHub's upload box cannot take folders that start with a dot, so these two
are created by hand. Repeat the same three steps for each:

1. In your new repository, click **Add file → Create new file**.
2. In the filename box, type the path exactly (typing the `/` creates the
   folders as you go), paste the matching file's contents from this project,
   then click **Commit changes...** → **Commit changes**.

| filename to type | paste the contents of |
|---|---|
| `.github/workflows/collect.yml` | `collect.yml` |
| `.github/scripts/should_collect.py` | `should_collect.py` |

Both are needed: the workflow calls the script to decide whether a second
daily attempt has anything left to do.

## 3. Upload everything else

1. Go back to the repository front page (click the repository name).
2. Click **Add file → Upload files**.
3. Drag in these items from this project's folder:
   - `collector.py`
   - `universe.yaml`
   - `requirements.txt`
   - `README.md` (this replaces the placeholder README — that's fine)
   - `SETUP.md`
   - the whole `data` folder (it contains one CSV file; dragging the
     folder keeps it as a folder)
   - optionally the whole `tests` folder (unit tests; not used by the
     Action, handy if you ever change `collector.py`)
4. Click **Commit changes**.

## 4. Run it once

1. Open the **Actions** tab. If GitHub shows a button about enabling
   workflows, click it.
2. In the left sidebar, click **Collect market data**.
3. Click **Run workflow** (right side) → green **Run workflow** button.
4. A run appears in the list. Click it to watch; it should take about
   3–8 minutes and end with a green check mark.

## 5. Check the result

A successful first run looks like this:

- The repository front page shows a new commit named something like
  `data: 2026-08-24 22:24 UTC`.
- The `data/` folder now contains eight JSON files, all updated a few
  minutes ago: `meta.json`, `etf_daily.json`, `etf_weekly.json`,
  `stocks_daily.json`, `derived.json`, `breadth.json`, `market.json` and
  `fred.json` (about 6 MB in total).
- Open `data/meta.json` and check three things:
  - `"run_ok": true`;
  - `freshness.US.lag_sessions` and `freshness.EU.lag_sessions` are `0`;
  - `counts` shows a few hundred `ok` entries and `failed_tickers` is empty
    or nearly empty. A handful of failed tickers is normal (symbols change
    over time) — dozens are not.
  `market_sources` may show a failure for one of the three sentiment
  sources; that is best-effort data and never fails the run.

From now on the collection runs automatically on weekday evenings
(22:20 UTC), with a second attempt at 01:20 UTC that skips itself when the
first one already worked. Nothing else to do.

## If something goes wrong

- **The run is red:** open the run in the Actions tab and read the log of
  the "Run collector" step — every problem is printed there in plain
  words. A fully red run usually means Yahoo Finance was temporarily
  unreachable; just press **Run workflow** again later. The run still
  committed `data/meta.json` with `"run_ok": false`, so the dashboard shows
  the failure instead of pretending the old data is current.
- **The run is green but some data looks missing:** open `data/meta.json`
  and look at `failed_tickers`, `market_sources` and `notes`.
- **A run finished in seconds without collecting anything:** that is the
  second daily attempt skipping itself because the first one already
  produced fresh data — the "Decide whether to collect" step log says so.
- **GitHub emails that the scheduled workflow was disabled** (can happen
  after ~60 days without repository activity): open **Actions → Collect
  market data → Enable workflow**.
