# SQLite -> PostgreSQL (Neon) migration

## What changed

- `app/database.py` now normalizes a `postgres://` URL to `postgresql://`
  and adds `pool_pre_ping`/`pool_recycle` for Postgres connections (Neon can
  drop idle connections; SQLite behavior is untouched).
- `requirements.txt` (new) pins the app's actual dependencies, including
  `psycopg2-binary` as the Postgres driver.
- `.env.example` / `.env` document the `DATABASE_URL` variable.
- `Procfile` (new) starts the app with `--port $PORT` for Render.
- `scripts/migrate_sqlite_to_postgres.py` (new) does a one-time copy of an
  existing local `rootplan.db`'s data into Postgres.

Nothing else changed: models, `crud.py`, routes, Alembic migrations, and the
frontend were already database-agnostic (`DATABASE_URL`-driven, normalized
schema, dashboard rebuilt from the DB on load) before this change - see
`app/database.py`'s original comment and `scripts/migrate_legacy_data.py`
for the earlier migration that got the app to this point.

## Steps

1. **Create a Neon project** and copy its pooled connection string (Neon
   dashboard -> Connection Details). It looks like:
   `postgresql://user:password@ep-xxxx.neon.tech/dbname?sslmode=require`

2. **Set `DATABASE_URL`** to that string - locally in `backend/.env`, and on
   Render as an environment variable on the backend service.

3. **Create the schema on Postgres:**
   ```bash
   cd backend
   venv\Scripts\python -m alembic upgrade head
   ```

4. **(Optional) Import existing local data.** If `backend/rootplan.db`
   already has real orders/routes/history you want to keep:
   ```bash
   venv\Scripts\python scripts\migrate_sqlite_to_postgres.py
   ```
   Safe to re-run - it skips rows that already exist on the Postgres side by
   id. The local `rootplan.db` file is left untouched (read-only source).

5. **Run the backend** exactly as before - `uvicorn app.main:app --reload`
   locally. On Render, the `Procfile` handles the start command
   (`--host 0.0.0.0 --port $PORT`), so no code change is needed per deploy.

6. **Verify persistence:** upload an Excel file, generate routes, refresh
   the browser, and restart the backend - the dashboard should reload the
   same data every time, from Postgres.
