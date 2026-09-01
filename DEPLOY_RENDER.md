# Deploying ROOTPLAN to Render

Render deploys from a Git repository (GitHub/GitLab/Bitbucket) it can watch
for pushes - there's no "upload a folder" path for a Blueprint deploy. This
repo (`rootplan/`) now has a `render.yaml` that defines both services, so
once it's on GitHub the rest is a few clicks.

## 1. Push this repo to GitHub

From `rootplan/` (this folder - it's the repo root, containing `backend/`
and `frontend/`):
```bash
git init
git add .
git commit -m "Prepare for Render deployment"
git branch -M main
git remote add origin <your-empty-github-repo-url>
git push -u origin main
```
`.gitignore` already excludes `backend/.env`, `backend/venv/`,
`backend/rootplan.db`, `frontend/node_modules/`, `frontend/dist/` - your
Neon credentials and Google Maps key are never committed.

## 2. Create the Blueprint on Render

1. [dashboard.render.com](https://dashboard.render.com) -> **New** -> **Blueprint**.
2. Connect the GitHub repo you just pushed.
3. Render reads `render.yaml` and proposes two services:
   `rootplan-backend` (Python web service) and `rootplan-frontend` (static site).
4. It'll prompt for the env vars marked `sync: false` - fill in:
   - **rootplan-backend**
     - `DATABASE_URL` - your Neon connection string (the same one in `backend/.env`)
     - `GOOGLE_MAPS_API_KEY` - your Google Maps key. Needs both **Geocoding
       API** and **Places API** enabled on this key's Google Cloud project
       (Google Cloud Console -> APIs & Services -> Library) - the geocoder
       falls back to Places' text search for addresses that name a specific
       apartment/building the structured Geocoding API can't place (see
       app/geocoding/google_geocoder.py's `_find_place`). Missing only
       Places API doesn't break anything - that fallback just quietly never
       fires - so this is safe to enable after the fact too.
     - `MAPBOX_ACCESS_TOKEN` - leave blank unless you use the Mapbox provider
     - `ALLOWED_ORIGINS` - leave blank (defaults to `*`) for now; you'll set this in step 4
   - **rootplan-frontend**
     - `VITE_API_BASE_URL` - leave blank for now too; also set in step 4
5. Click **Apply** to create and deploy both.

## 3. First deploy will "work" but be loosely connected

The two services don't know each other's URL yet, so the frontend will call
same-origin (wrong) and CORS is wide open (`*`). That's expected - fixed next.

## 4. Wire the two services together

Once both have deployed, Render shows each one's URL, e.g.:
- Backend: `https://rootplan-backend.onrender.com`
- Frontend: `https://rootplan-frontend.onrender.com`

Go back into each service's **Environment** tab and set:
- **rootplan-frontend** -> `VITE_API_BASE_URL` = the backend URL (no trailing slash)
- **rootplan-backend** -> `ALLOWED_ORIGINS` = the frontend URL (no trailing slash)

Saving an env var triggers a redeploy automatically. `VITE_API_BASE_URL` is
baked in at build time (it's a Vite env var), so the frontend must actually
rebuild after you set it - the auto-redeploy handles that.

## 5. Verify

Open the frontend URL, upload an Excel file, generate routes, then:
- Refresh the browser - the same data should still be there.
- In the Render dashboard, manually restart the `rootplan-backend` service -
  refresh the frontend again, data should still be there (that's the whole
  point of the Postgres/Neon migration - see `backend/POSTGRES_MIGRATION.md`).

## Notes

- `backend/Procfile` (`uvicorn app.main:app --host 0.0.0.0 --port $PORT`) is
  what `render.yaml`'s `startCommand` also does - the Procfile is just a
  fallback if you ever create the backend service by hand instead of via
  Blueprint.
- Render's free web service plan spins down after 15 minutes idle; the next
  request wakes it up (~30-60s cold start). That's a Render free-tier
  characteristic, unrelated to the database - Neon's own free tier also
  suspends idle compute and wakes on the next query, both transparent to
  the app because of `pool_pre_ping` (see `backend/app/database.py`).
- To redeploy after future code changes: `git push` - Render auto-deploys
  both services on push (`autoDeploy` defaults to on).
