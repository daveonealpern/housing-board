# Housing Signal Board — setup

A standalone repo that feeds one sheet in your command-index hub. Twenty minutes
once, then it runs itself every morning and you never open it again.

You are creating a small robot that lives on GitHub, wakes at 6am, visits the
Federal Reserve, the SEC and Polymarket, writes down what it finds, and updates a
page. You bookmark the page, or reach it from your hub. That is the whole system.

Final layout:

    housing-board/
      index.html                     the sheet
      fetch.py                       the collector
      .github/workflows/
        update-housing-data.yml      the schedule
      data.json                      appears on its own after the first run

---

## Step 1 — Make the repo

Go to **github.com/new**.

- Repository name: `housing-board` (this exact name matters, see Step 5)
- Set it to **Public**. Scheduled robots are free on public repos and draw from a
  minute allowance on private ones. Everything it collects is public data anyway.
- Tick **Add a README file**
- Press **Create repository**

---

## Step 2 — Add the three files

Press **Add file → Create new file** once per file, typing each filename exactly.
GitHub turns slashes into folders as you type, which is what you want.

**File 1** — filename `index.html`
Paste the contents of `housing-index.html`. Commit.

**File 2** — filename `fetch.py`
Paste the contents of `fetch.py`. Commit.

**File 3** — filename `.github/workflows/update-housing-data.yml`
Paste the contents of `update-housing-data.yml`. Commit.

---

## Step 3 — Give the collector your keys

**Settings → Secrets and variables → Actions → New repository secret.** Twice.

| Name | Value |
|---|---|
| `FRED_API_KEY` | your FRED key |
| `SEC_USER_AGENT` | `David Alpern your@email.com` |

The second is not optional theatre. The SEC blocks anonymous programs and
requires a contact line. It is never shown publicly.

**Regenerate your FRED key first** at fred.stlouisfed.org, since the old one was
pasted into a chat. Put the fresh one straight into this box rather than sending
it anywhere.

---

## Step 4 — Turn the page on

**Settings → Pages.** Source: **Deploy from a branch**. Branch `main`, folder
`/ (root)`. Save.

Your sheet lives at:

    https://YOUR-USERNAME.github.io/housing-board/

First publish takes two or three minutes.

---

## Step 5 — Run it once

**Actions** tab → enable workflows if asked → **Update housing data** in the left
sidebar → **Run workflow** → the green **Run workflow** button.

About a minute. Green tick means it worked and `data.json` will have appeared.
Reload the sheet and the numbers are there.

Red X: click into the run, read the last few lines, send them over. It names the
problem plainly.

---

## Step 6 — Link it from your hub

In the `command-index` repo, edit `index.html`. Find the PARCELS list, and after
the `clc-ldm` block (1.06) add a comma and paste this in:

```js
    ,{
      index: "1.07",
      book: "I",
      title: "Housing Signal Board",
      description: "Leading indicators for land and homebuilding, ordered by how far ahead each one sees. Permits, starts, rates, cost, distress, California series, and public builder filings.",
      tag: "Auto-updating, no inputs",
      url: "https://YOUR-USERNAME.github.io/housing-board/"
    }
```

Replace `YOUR-USERNAME` with your GitHub username. Commit. The hub rebuilds around
it and the card behaves exactly like every other sheet.

The sheet's own "Sheet Index" back link points at `../command-index/`, which
resolves correctly as long as both repos are named as written here. That is the
only reason the repo name matters.

---

## That is the end of the work

- It updates every morning on its own
- The sheet shows when it last ran and what publishes next, read from FRED's
  actual release calendar rather than estimated
- If a data series gets renamed at the Fed, the collector searches for the
  replacement, keeps going, and posts a note on the sheet saying what it swapped
- If a source breaks entirely, a brass-bordered notice appears at the top rather
  than the sheet quietly showing stale numbers

GitHub disables scheduled workflows on repos with no commits for sixty days.
This one commits `data.json` whenever the data moves, so it keeps itself alive.
If the sheet ever goes stale, press Run workflow once.

---

## What still needs a human, four times a year

Builders disclose lots owned and lots optioned in a sentence in the middle of the
10-Q, not in tagged data. The collector fetches every filing the day it lands and
pulls the balance sheet inventory figures, but it cannot reliably read that
sentence.

The builder table shows each company's filing date, so you will see when a quarter
has landed. Message me **"run the builder numbers"** and I will read them and hand
back lot counts, specs per community, and cancellation rates.

If you would rather not have even that step, there is a version where the collector
calls Claude to read each filing itself. One more key, a few cents a quarter.
