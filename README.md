# MalyPrincBB

This repository archives the daily page from `https://malyprinc.mikme.eu/`.

It keeps two variants for every captured day:

- `original/` keeps the raw HTML exactly as downloaded.
- `original/source.txt` keeps the full HTML source as plain text.
- `offline/` keeps a localized copy with downloaded CSS, images, and other page assets.
- `offline/page.pdf` keeps a printable PDF generated from the offline copy.

Repository layout:

- `snapshots/YYYY-MM-DD/` stores dated captures.
- `days/01` to `days/30` store stable links grouped by puzzle day.
- `index.html` is the GitHub Pages landing page.

Automation:

- GitHub Actions runs daily around `00:10` Europe/Bratislava time.
- Because GitHub Actions cron is UTC-only, the workflow triggers at both `22:10` and `23:10` UTC and only continues when the local Bratislava hour is `00`.

Manual refresh:

```bash
python3 scripts/archive.py --date 2026-04-03
```

GitHub Pages should be enabled from the `main` branch root.
