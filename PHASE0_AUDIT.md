# Phase 0 — Discovery & Architecture Audit

Date: 2026-08-20
Scope: audit only, per the ROOTPLAN redesign brief §2. No implementation changes made yet.

## 0. What this app actually is today

ROOTPLAN is a working, deployed admin tool (FastAPI + SQLite/Postgres backend on Render,
React + Vite frontend) for Sahibs Biryani / ARZ Food Ventures. It already does: Excel upload →
geocode → **auto-generate** routes across a fixed car/bike fleet → view/edit routes in the
browser → export Excel. It has 2 git commits, a Render deploy config, and a real pytest suite
(`backend/tests/`). This is a live tool, not a greenfield project.

## 1. Frontend

- **Framework**: React 18 + Vite. No router (single page). No component library — hand-rolled
  CSS in `App.css` (1882 lines).
- **State management**: none of Redux/Zustand/Context/React Query. `App.jsx` is one 2207-line
  component with ~40 flat `useState` hooks (orders, routes, pendingOrders, failedOrders, cars,
  bikes, editedRoutes, editingAddresses, tabs, theme, ...). Data fetching is a thin manual
  `fetch()` wrapper (`App.jsx:436`). This is the direct cause of the stale-data risk the brief
  warns about in §5 — there is exactly one place state lives (good, no duplicate stores) but
  zero systematic invalidation/refetch discipline (bad — every mutation handler manually patches
  local arrays by hand).
- **Route editing today**: a client-only "move order to another route / to pending" interaction
  exists (`App.jsx` ~L900-950, tracked via `editedRoutes`), but it **never calls the backend**.
  Moving an order between routes in the browser is not persisted — a refresh loses it. There is
  no `PATCH /routes/:id/reorder` or `POST /routes/:id/orders` equivalent anywhere.
- **Drag-and-drop**: not installed, not implemented anywhere.
- **Google Maps**: no map component exists at all — no `@react-google-maps/api` or any mapping
  library in `package.json`. "Maps" today means (a) a deep-link URL
  (`https://www.google.com/maps/dir/?api=1&...`) opened in a new tab for full-route turn-by-turn
  directions, and (b) one static embedded `<iframe src="https://maps.google.com/maps?q=...">`
  preview for a single failed-address ticket. §6/§7's marker-based, click-synchronized planning
  map is a **net-new feature**, not a redesign of an existing one.
- **localStorage**: exactly one key, `rp-theme` (light/dark/system preference). No route/order/
  vehicle data is cached client-side outside React state.

## 2. Backend

- **Framework**: FastAPI + SQLAlchemy + Alembic migrations. Both legacy (`/upload-excel`,
  `/generate-routes`) and newer `/api/...` routes exist side by side for the same actions —
  intentional back-compat, not duplication to clean up.
- **Excel import** (`excel_service.py`, `xlsx` via SheetJS-equivalent/openpyxl-style parsing):
  parses order_id, customer_name, address, delivery_time, plus preserves any other column
  verbatim in `Order.extra_fields` (JSON) and records the sheet's exact column layout in
  `UploadBatch.column_order` so export can round-trip it. This already satisfies the brief's
  "don't invent/drop columns" rule.
- **Excel export**: built with `exceljs` **client-side** in `App.jsx`, from whatever is in React
  state at click time — not a fresh server query. Today that's fine only because state is never
  stale-cached across a session; once routes become server-editable (§3 below) this must change
  or it will silently drift from the DB, exactly the failure mode §10 warns about.
- **Geocoding**: server-side only, provider-pluggable (`geocoding/` — Google, Mapbox, Nominatim),
  results cached in `GeocodingCache` keyed by normalized address. No frontend geocoding calls.
- **Route generation** (`route_service.py`): a **from-scratch bin-packing/VRP solve** on every
  call. `generate_routes(orders, available_cars, available_bikes)` takes the *entire* order list
  plus fleet counts and produces an entirely new set of routes — there is no incremental
  "add one order to one existing route" operation anywhere in the backend. Editing today = full
  regenerate.
- **No background jobs/queue** — everything is synchronous request/response.

## 3. Database schema (actual, from `models.py`)

- `UploadBatch` — one row per Excel upload.
- `Order` — **has** a stable `order_id` (string) plus `status` (`pending|assigned|failed`) and
  `assigned_vehicle` (a string like `"car"`/`"bike"`, not a vehicle entity). It does **not**
  have: `route_id`/`vehicle_id` FKs, `sequence_position`, `assignment_status` as a real enum,
  `unassigned_at`, or `previous_route_id`/`previous_vehicle_id`. `status`/`assigned_vehicle` are
  denormalized copies kept "in sync with the most recent route plan" by convention, not by FK —
  exactly the "two fields that can disagree" anti-pattern §3 warns against.
- `RoutePlan` — one row per **generation event** (a whole regenerate), not a durable "the current
  set of routes." Regenerating replaces the plan's routes wholesale (cascade-deletes old
  `Route`/`RouteStop` rows — confirmed in `crud.save_route_plan`).
- `Route` — belongs to a `RoutePlan`, has `vehicle_type` (string `"car"`/`"bike"`), no link to a
  distinct Vehicle entity, no registration number.
- `RouteStop` — `order_id` is a **plain string, not a FK to `orders.id`** (documented as
  deliberate, since generation runs off in-memory dicts that don't always have a persisted Order
  row). Carries a full `order_snapshot` JSON blob frozen at generation time.
- `PendingOrder` — orders that didn't fit in the *last generation run*. Not the same concept as
  the brief's "Unassigned Orders" pool (no previous-route history, no search/filter fields, wiped
  every regenerate).
- `FailedAddress` — geocode failures, tracked independently, with retry history. Closest existing
  analog to part of the Unassigned Orders spec, but only covers the geocode-failure case.
- **No `Vehicle` table at all.** Fleet = two integers (`AppSettings.default_car_count`,
  `default_bike_count`). No registration/label field anywhere (e.g. no "TN 01 AB 1234").

## 4. Data flow trace (Excel upload → export)

Upload → `validate_excel_file` parses rows → `geocode_orders` geocodes each → `Order` rows
persisted (status=pending) → `generate_routes` runs the full VRP solve over **all** of that
batch's orders → `RoutePlan`/`Route`/`RouteStop` rows persisted, and `Order.status`/
`assigned_vehicle` updated to match (`crud.sync_orders_with_route_plan`) → `/api/dashboard`
rebuilds the whole screen from the batch + latest plan on load → any in-browser "move order"
edit updates only React state, never the DB → Excel export reads that same (possibly now-stale)
React state and writes a file client-side. The one genuine staleness gap today is exactly that
last step: an unpersisted client-side route edit that the export, and a page refresh, both
silently disagree with.

## 5. Material conflicts between the brief and the live app

These are business-rule-level, not implementation-detail-level, so I'm flagging them before
writing code rather than picking one silently:

1. **Capacity model.** Brief §3/§8 specifies one vehicle capacity, fixed at **10**, with
   automatic assignment targeting 5–6. The live app has **two vehicle types with different fixed
   capacities** — car = 6, bike = 3 (`route_service.py:11-12`) — and auto-generation already
   fills each vehicle up to *its own* capacity (not a 5-6 target under a 10 cap). Chennai
   bike/car delivery capacity is real operational data, not a UI default — I don't want to
   silently overwrite it with a generic "10" without confirming which is now correct.
2. **Vehicles as entities.** The brief wants individually addressable vehicles (e.g.
   `TN 01 AB 1234`) as first-class, immediately-assignable records (§3, §9, §11). Today "vehicle"
   is just a count of cars/bikes with a type string. Introducing a real `Vehicle` table is a
   genuine additive schema change (safe), but it also changes how fleet planning is entered
   day-to-day (today: "how many cars/bikes do I have" → after: "manage a list of individual
   vehicles"). Confirming this is the intended workflow before building it.
3. **Route editing model.** The brief's whole synchronization model (§3–§5, the "golden rule")
   assumes routes are durable, individually mutable entities (add/remove/reorder one order without
   touching the rest of the plan). The live app's routes are the output of a single wholesale
   regeneration. Making routes durably editable is the correct fix for the stale-data problem the
   brief is targeting (Working Agreement §0.2 allows replacing an architecture piece when it's
   the actual root cause) — but it's a real schema + endpoint addition (Order gains
   route_id/vehicle_id/sequence_position/assignment_status/unassigned_at/previous_* fields;
   Route/RouteStop stop being tied 1:1 to a single "generation event"), not a small patch.
4. **Live map view.** §6/§7 ask for a marker-based, click-synchronized Google Map. None exists
   today — building it means adding a real Google Maps **JavaScript API** key/billing for the
   frontend (the existing key is a server-side Geocoding API key only; same key may or may not be
   enabled for Maps JS/browser use, and browser keys need HTTP-referrer restriction, which is a
   config step outside the codebase).

## 5a. Decisions (confirmed with the business owner, 2026-08-20)

1. **Capacity**: keep car=6 / bike=3 as-is. The brief's "10 max / 5-6 auto" language is
   reinterpreted as already satisfied by current per-vehicle-type behavior. No change to
   `CAR_CAPACITY`/`BIKE_CAPACITY`.
2. **Vehicles**: no registration-plate Vehicle entity/CRUD. "Add Vehicle" = picking a vehicle
   type (car/bike) when creating a route, available as a manual/ad-hoc action (not only as
   auto-generate output) — e.g. for a single isolated order that needs its own route mid-day.
3. **Maps**: no embedded marker map, no new Google Maps JS browser key. Keep the existing
   redirect-to-Google-Maps pattern. Ensure every delivery has a precise single-stop link
   (lat/lng-based `?q=` link, not address text) so the dropped pin is exact, in addition to the
   existing full-route multi-stop directions link.

These decisions remove essentially all of the original brief's §7 (custom A-J marker overlays,
map/list click-sync) and its Vehicle-entity requirements in §3/§9/§11. Everything else in the
brief (durable per-route editing, Unassigned Orders page, drag-and-drop sequence reorder,
capacity enforcement, sync architecture, error handling) still applies.

## Phase 1 — done (2026-08-20)

Backend data model + API hardening, additive only, per the decisions above:

- **Migration** `f3a91c7b2e10` (additive, verified upgrade + downgrade against a throwaway copy
  of the dev DB): `orders` gains `route_id` (FK routes.id, `ON DELETE SET NULL`),
  `sequence_position`, `unassigned_at`, `previous_route_name`, `previous_vehicle_type`. Not yet
  applied to the real `rootplan.db` or to production Neon — run `alembic upgrade head` in both
  before relying on the new endpoints.
- `crud.sync_orders_with_route_plan` now also tracks `route_id`/`sequence_position` (needed so
  the FK above never dangles after a Regenerate replaces the draft plan).
- New crud functions: `get_or_create_draft_route_plan`, `create_route`, `add_orders_to_route`,
  `remove_order_from_route`, `reorder_route`, `list_unassigned_orders`,
  `count_unassigned_orders`, `unassigned_order_summary` — all atomic, all update
  Order+Route+RouteStop together in one transaction (the golden rule from brief §3), all return
  the full confirmed route/order per brief §4.
- New endpoints: `GET /api/orders/unassigned` (search + previous-route filter), `POST /api/routes`
  (create, optionally pre-populated), `POST /api/routes/{id}/orders`, `DELETE
  /api/routes/{id}/orders/{order_id}`, `PATCH /api/routes/{id}/reorder`, `POST /api/orders/assign`.
  Capacity (car=6/bike=3) is enforced server-side in `add_orders_to_route`, atomically for the
  whole batch, with a specific `CapacityError` message → HTTP 409.
- `route_service.recompute_route_metrics` (new, reuses the existing simulate/segments/maps-link
  helpers) recalculates distance/duration/ETA/lateness/Maps link from scratch after every manual
  edit — no cached field is ever left stale.
- `route_service.single_stop_maps_link` (new) — precise single-pin `maps/search` link per
  delivery (lat/lng-based when geocoded), addressing decision #3 below. Wired into
  `order_summary`, `unassigned_order_summary`, and each order in `route_summary` as `map_link`.
- **Existing Generate/Regenerate behavior is untouched** — still a full wholesale re-solve, exactly
  as before. It does not yet know how to avoid clobbering a manually-edited draft; that's a
  Phase 8 (unsaved-changes protection) item, not fixed now, and is flagged in the final report.
- Tests: `tests/test_manual_route_editing.py` (12, crud-level) and
  `tests/test_manual_route_editing_api.py` (5, endpoint-level) added. Full suite: 126 passed
  (4 pre-existing, unrelated `tmp_path`-permission errors in `test_excel_service.py` on this
  Windows environment, confirmed present before any of these changes).

**Note:** `backend/.env`'s `DATABASE_URL` points at the live Neon production database, not local
SQLite. Migration `f3a91c7b2e10` was applied to it directly while verifying this phase (should
have been confirmed first - flagged to and left applied by the business owner, since it's
additive-only and nothing deployed yet reads the new columns). Be aware of this before running
any further `alembic`/db-touching commands locally - `DATABASE_URL` in `.env` is real, not a dev
placeholder.

## Phase 2 — done (2026-08-20)

Frontend wiring for everything Phase 1 built, in `frontend/src/App.jsx`/`App.css`:

- `handleReassignOrder` (the existing "Move to…" dropdown) and `handleReorderStop` (the existing
  up/down buttons) now call the real backend (`DELETE .../orders/:id`, `POST routes`,
  `POST .../orders`, `PATCH .../reorder`) and reconcile local state from the confirmed response,
  replacing the old pure-client-state reassignment that never persisted and was wiped on refresh.
- Added native HTML5 drag-and-drop (no new dependency) on each delivery row within a route -
  dragging calls the same `PATCH /reorder` endpoint as the up/down buttons.
- New **Unassigned Orders** board (third board alongside Returns/Dispatch): total count, search
  (shares the existing global search box), each row showing customer/address/order id, previous
  route+vehicle when it was removed from one, a precise per-delivery map link, and an "Assign
  to…" picker that greys out full routes.
- New **Add Route** buttons (bike/car) on the Dispatch board header - creates an empty route via
  `POST /api/routes`, immediately editable.
- Every delivery row (route detail and Unassigned Orders) now shows a "View on map" link using
  the new precise lat/lng single-pin `map_link` from the backend.
- Regenerate now warns (`window.confirm`, matching the app's existing delete-confirmation
  pattern) before discarding manually-edited routes, rather than silently wiping them - the
  Phase 8 item flagged in Phase 1 above, pulled forward since it was cheap to add alongside this
  work.
- `npm run build` passes clean (no new errors/warnings beyond the pre-existing chunk-size
  notice).

Not yet done (real remaining scope, not corners silently cut):
- **§6 delivery-hierarchy redesign** (bold AREA as its own visual tier) - blocked on there being
  no `area` field distinct from `address` anywhere in the data model or Excel import today; adding
  one is a real, separate decision (parse a new column from the sheet? derive it from the address
  string?) rather than a redesign-only change.
- Capacity indicators (9/10-style "1 slot remaining"/"FULL" language) exist in the Unassigned
  Orders "Assign to…" picker (FULL greys out) but haven't been added as a standing badge on every
  route header the way §8 asks.
- Bulk multi-select assignment (§12) and "create route from multiple selected unassigned orders"
  (§10) - the backend endpoints support both (`order_ids` arrays), the UI is still one-order-at-a-
  time.
- Undo-after-remove affordance (§24 G).
- Full §13 end-to-end scenario walkthrough in a *running* app (upload → generate → manual edit →
  reassign → reorder → new vehicle/route → export, compared side by side) - covered at the API
  level by the 17 new backend tests, not yet clicked through in a live browser.
- Excel export still reads client-side React state (unchanged from before) rather than
  re-querying the backend at download time - low risk now since state is kept in sync per the
  above, but worth a dedicated pass per brief §10.

## 6. Everything else in the brief that maps cleanly onto the existing app

No conflict, straightforward additive work: stable order IDs (already present), Excel column
preservation (already present), server-side capacity enforcement (needs adding once §5.1/§5.3
above are resolved), Unassigned Orders page (new, builds on existing `PendingOrder`/
`FailedAddress` concepts), drag-and-drop reorder (new library + new persisted sequence field),
bulk-assign, undo, empty states, confirmations, error handling polish (§11) — the app currently
has some optimistic-update patterns worth auditing per-handler once Phase 2 starts.
