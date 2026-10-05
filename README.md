# AI Status Now – live status of 200 AI tools

A one-page website that shows whether popular AI tools (ChatGPT, Claude, Gemini, Grok and ~200 more) are up or down. A robot on GitHub checks every tool about every 10 minutes and republishes the page automatically. Hosting is free (GitHub Pages).

## The only two files you normally edit

| File | What it is |
|---|---|
| `site-config.json` | Site name, your web address, contact email, Google Form link, AdSense ID and ad slots, Search Console code. Each setting has a `_help` line above it explaining it. |
| `tools.json` | The list of tools. One line per tool. |

To edit on GitHub: open the file → click the pencil icon → change the text between the quotes → **Commit changes**. The website rebuilds itself in 2–3 minutes.

## Adding a tool

Copy any existing line in `tools.json`, paste it on a new line, and change the name, website, category, pricing and description. Every line except the last needs a comma at the end.

Optional extras on a line:
- `"featured": true` – shows the tool in the quick-glance strip at the top.
- `"status_page": "https://status.example.com"` – the tool's official status page (works with the common "Statuspage" kind).
- `"status_component": "ChatGPT"` – only use the part of that status page whose name contains this word.
- `"check_url": "https://..."` – check a different address than the one visitors click.

## What the other files do (no need to touch)

- `update.py` – the robot: checks the tools and builds the website.
- `templates/` – the page design.
- `static/` – icon and social-share image.
- `data/state.json` – the last 24 hours of results (written by the robot).
- `.github/workflows/update-status.yml` – tells GitHub to run the robot every 10 minutes.
- `site/` – the finished website (built automatically, not stored in the repository).

## If something looks wrong

- **The page didn't update:** open the **Actions** tab. A red ✗ means a run failed; click it to see why. Click **Run workflow** to try again.
- **A tool always shows Down but works fine:** some sites block automated checks. Add a `"check_url"` pointing to another page of that site, or remove the tool.
