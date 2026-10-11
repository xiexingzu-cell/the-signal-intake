# THE SIGNAL V0.3.2 — GitHub Actions overseas metadata collector

- **Repository must be PRIVATE**. No website, no subscriber data, no password/API keys, no root SSH keys.
- `feeds.json`: experimental overseas publisher RSS endpoints, metadata only. Endpoints are **candidates**, not proof they work.
- `collector.py`: reused local V0.3.1 standard-library RSS/Atom parser and link validation.
- `collect_remote.py`: outputs `snapshots/latest.json` + `snapshots/latest.report.json` after a nonempty collection.
- `.github/workflows/collect-overseas.yml`: manual dispatch + **06:37 and 07:17 Beijing time** scheduled collection. GitHub may delay/miss runs, so Tencent remains the publication clock.
- `permissions: contents: write`: only the workflow can commit the updated metadata, subject to repository/organization policy.
- Personal use only; no full article text, republished imagery, or AI-generated content.

## Install this folder as the ROOT of a new private GitHub repository

In PowerShell (adjust username/repository):

```powershell
Expand-Archive "$env:USERPROFILE\Downloads\the-signal-v032-github-bridge.zip" -DestinationPath "$env:USERPROFILE\Downloads" -Force
cd "$env:USERPROFILE\Downloads\github-bridge"
git init -b main
git add .
git commit -m "Initialize THE SIGNAL overseas news collector"
git remote add origin https://github.com/YOUR_USERNAME/the-signal-intake.git
git push -u origin main
```

In GitHub UI: **Actions > THE SIGNAL overseas news metadata > Run workflow**. Check `snapshots/latest.json` was committed and `snapshots/latest.report.json` for per-source failures. No snapshot will be committed if every RSS feed fails; fix sources rather than fabricating coverage.

Do **not** commit private SSH keys, environment variables, or any paid/paywalled news text.
