# Deploying the static demo

The demo build (`npm run build`, output in `dist/`) is static: no backend, key, Docker or database. It
uses relative asset paths and hash routes (`#/`, `#/app/...`), so it can be served from any path.

- **Vercel:** `frontend/vercel.json` sets the install, build and output commands. Import the repository
  with `frontend` as the root directory.
- **GitHub Pages:** `github-pages.yml` here is a ready workflow, kept out of `.github/workflows/` so it
  never runs by itself. Copy it there and choose "GitHub Actions" as the Pages source to enable it.
  `public/.nojekyll` stops Pages from filtering the build output.

Nothing in this repository deploys automatically.
