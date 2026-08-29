import { Fragment, useEffect, useMemo, useRef, useState } from 'react';
import { GoogleMap, Marker, Polyline, Circle, InfoWindow, useJsApiLoader } from '@react-google-maps/api';
import {
  IconRoute, IconCar, IconBike, IconClock, IconCheck, IconAlert, IconPin, IconPlus, IconInbox,
  IconDownload, IconRefresh, IconArrowUp, IconArrowDown, IconGauge, IconFlag, IconSearch, IconX,
  IconUsers, IconChevron, IconLocate,
} from '../icons';
import './routeWorkspace.css';

// A JS-API key is meant to be used client-side (it's restricted by
// HTTP referrer/app in Google Cloud Console, not a secret like a backend
// auth token) - same treatment as the Sentry DSN elsewhere in this
// codebase. VITE_GOOGLE_MAPS_API_KEY can still override it without a
// code change.
//
// Reverted back to the original key after two follow-up keys each broke
// the map outright - confirmed with a real headless-browser load (not
// just an HTTP status check, which can't see this): both newer keys threw
// ApiTargetBlockedMapError on Maps JavaScript API itself (the map never
// initializes at all), because each one had only ONE of {Maps JavaScript
// API, Directions API} allowed in its own API restrictions, never both at
// once. This key is the one of the three that actually loads the map
// (mapOk, no gm_authFailure) - it just doesn't have Directions authorized
// yet (REQUEST_DENIED), which this app already degrades gracefully for
// (falls back to the overview map / "Route unavailable" text) rather than
// breaking. The real fix is enabling BOTH Maps JavaScript API and
// Directions API on THIS SAME key's API restrictions in Google Cloud
// Console - not creating another key with just one of them checked.
const GOOGLE_MAPS_API_KEY = import.meta.env.VITE_GOOGLE_MAPS_API_KEY || 'AIzaSyDodjkyPxxK0C_5m6pX0u-hAj2kHeeI-Zo';
const GOOGLE_MAPS_LIBRARIES = [];

// A stationary phone's GPS still drifts a few meters between fixes - real
// sensor noise, not movement (confirmed against a genuinely parked phone:
// the reported point never lands on the exact same coordinate twice).
// Shared by every place that decides "did the driver actually move"
// (useAnimatedPositions' glide, FleetMap's heading, LiveMapModal's panTo
// and heading) so that answer is consistent everywhere live tracking
// shows up, not a different threshold hand-picked per call site.
const GPS_NOISE_METERS = 8;

// Was: a custom dark vector style for LiveMapModal (its own map, distinct
// from FleetMap below). Replaced with satellite - see LiveMapModal's own
// options for why (same reasoning as FleetMap's satellite lock: seeing
// the real ground under a live driver is the whole point of a live-
// tracking view, and a custom `styles` array is silently ignored on
// satellite/hybrid map types anyway, so keeping this around no longer
// did anything).

// Same VITE_API_BASE_URL App.jsx's apiFetch reads (empty in local dev,
// where Vite's own proxy - see vite.config.js's '/ws' entry - forwards
// same-origin requests to the backend; the deployed backend's own origin
// in production, since prod frontend/backend are separate Render
// services with no proxy between them). Re-derived here rather than
// threaded down as a prop through RouteWorkspace/FleetMap/LiveMapModal,
// same as GOOGLE_MAPS_API_KEY above - one extra env read is cheaper than
// plumbing one more prop through every layer for a three-line derivation.
function trackingWebSocketUrl() {
  const base = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/$/, '');
  if (base) return `${base.replace(/^http/, 'ws')}/ws/tracking`;
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${window.location.host}/ws/tracking`;
}

// Live driver locations pushed the instant a driver's phone pings (see
// ws_manager.py) - the accelerant on top of this app's existing REST
// polling (fetchRouteTracking, still running unchanged as a fallback for
// a first load, a dropped socket reconnecting, or an environment that
// blocks WebSocket upgrades entirely). `onMessage` is read through a ref
// so the connection effect only needs to depend on `enabled` - it doesn't
// need to reconnect just because the caller's callback identity changed
// on a re-render (which, being a plain function defined inline in a
// component body, happens on every render).
function useTrackingSocket(enabled, onMessage) {
  const onMessageRef = useRef(onMessage);
  onMessageRef.current = onMessage;

  useEffect(() => {
    if (!enabled) return undefined;
    let cancelled = false;
    let socket = null;
    let reconnectTimer = null;
    let attempt = 0;

    const connect = () => {
      if (cancelled) return;
      socket = new WebSocket(trackingWebSocketUrl());
      socket.onopen = () => { attempt = 0; };
      socket.onmessage = (event) => {
        try {
          onMessageRef.current(JSON.parse(event.data));
        } catch {
          // Not parseable JSON - ignore rather than let one bad frame
          // take down the handler for every frame after it.
        }
      };
      socket.onclose = () => {
        if (cancelled) return;
        // A dropped connection (phone screen off doesn't affect this -
        // it's the *admin's* browser tab/network that would drop it: a
        // wifi blip, the backend redeploying, Render's free tier idling
        // back up) should reconnect on its own without hammering the
        // server - capped backoff, not immediate retry-in-a-loop.
        const delay = Math.min(10000, 1000 * 2 ** attempt);
        attempt += 1;
        reconnectTimer = window.setTimeout(connect, delay);
      };
      socket.onerror = () => { socket.close(); };
    };
    connect();

    return () => {
      cancelled = true;
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
      if (socket) { socket.onclose = null; socket.close(); }
    };
  }, [enabled]);
}

// Smoothly glides a set of {lat,lng} positions toward whatever their
// latest real fix is, instead of the marker jumping instantly to each new
// point the moment it arrives - the same trick Uber/Swiggy/Rapido-style
// live maps use, and the actual fix for tracking "looking" laggy even
// when the underlying data is fresh: a GPS ping lands every few seconds
// either way (see driver-app/src/locationTask.js), so what makes motion
// read as continuous instead of a stuck-then-teleport dot is entirely
// this animation, not how often a new fix arrives.
//
// `targets` is keyed by whatever id the caller uses (route_id for
// FleetMap's multiple dots, a fixed key for LiveMapModal's single one) ->
// {lat, lng}. Returns the same shape, but animated. Positions for keys no
// longer present in `targets` are dropped so a driver dot doesn't linger
// after live tracking is switched off or a route stops being tracked.
function useAnimatedPositions(targets) {
  const [displayed, setDisplayed] = useState({});
  const displayedRef = useRef({});
  const animsRef = useRef({}); // key -> { from, to, start }
  const rafIdRef = useRef(null);
  const GLIDE_MS = 4200; // just past the driver's ~5s ping interval, so a glide mostly finishes before the next one rather than visibly stalling at the end

  useEffect(() => {
    Object.entries(targets).forEach(([key, pos]) => {
      const hasDisplayedPosition = key in displayedRef.current;
      const from = displayedRef.current[key] || { lat: pos.lat, lng: pos.lng };
      const current = animsRef.current[key];
      // Retargeting to the exact same fix (a REST poll re-confirming what
      // the WebSocket already delivered) shouldn't restart the glide from
      // scratch - only start a new animation when the target actually
      // moved.
      if (current && current.to.lat === pos.lat && current.to.lng === pos.lng) return;
      // Below the noise floor - hold perfectly still instead of gliding
      // to a point that isn't a real movement. Only applies once this key
      // already has a real displayed position; the very first fix for a
      // key must always go through (its "from" is trivially its own
      // position either way, so the distance check would otherwise always
      // read as 0 and silently never render the dot at all).
      if (hasDisplayedPosition && haversineMeters(from, pos) < GPS_NOISE_METERS) return;
      animsRef.current[key] = { from, to: { lat: pos.lat, lng: pos.lng }, start: performance.now() };
    });
    // Pruning happens *here*, in the effect body, not inside tick() below -
    // this is the fresh `targets` for this specific effect invocation.
    // tick() runs across many animation frames without this effect
    // re-running (that's the whole point of the rafIdRef guard below), so
    // a targets check inside tick() would close over whatever `targets`
    // was at the moment tick was first scheduled and never see a later
    // invocation's value again for as long as the same loop keeps
    // running - which doesn't just miss *new* prunes, it actively deletes
    // every position on every frame once the first target arrives (empty
    // `{}` at mount is never "key in targets"). Confirmed via a real
    // instant-snap-instead-of-glide bug caused by exactly that.
    Object.keys(animsRef.current).forEach((key) => {
      if (!(key in targets)) { delete animsRef.current[key]; delete displayedRef.current[key]; }
    });

    if (rafIdRef.current != null) return; // a loop is already running and will pick up the retarget above
    const tick = () => {
      const now = performance.now();
      let stillAnimating = false;
      Object.entries(animsRef.current).forEach(([key, anim]) => {
        const noOp = anim.from.lat === anim.to.lat && anim.from.lng === anim.to.lng;
        const t = noOp ? 1 : Math.min(1, (now - anim.start) / GLIDE_MS);
        // A no-op animation (a brand-new key's first fix, animated "from"
        // itself) is already at its target on frame one - counting it as
        // still-animating just because elapsed time hasn't reached
        // GLIDE_MS yet would keep this rAF loop spinning for a full
        // 4.2s doing nothing on every fresh key, for no visible benefit.
        if (t < 1) stillAnimating = true;
        displayedRef.current[key] = {
          lat: anim.from.lat + (anim.to.lat - anim.from.lat) * t,
          lng: anim.from.lng + (anim.to.lng - anim.from.lng) * t,
        };
      });
      setDisplayed({ ...displayedRef.current });
      rafIdRef.current = stillAnimating ? requestAnimationFrame(tick) : null;
    };
    rafIdRef.current = requestAnimationFrame(tick);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [targets]);

  useEffect(() => () => {
    if (rafIdRef.current == null) return;
    cancelAnimationFrame(rafIdRef.current);
    // Resetting the ref back to null here is the actual point, not just
    // the cancel call - React 18 StrictMode (dev only) runs every
    // effect's mount -> cleanup -> mount once on initial mount to catch
    // exactly this kind of bug, and without this reset, that first
    // simulated cleanup cancels the just-scheduled frame but leaves
    // rafIdRef.current non-null - so the *next* (real) run of the effect
    // above sees "a loop is already running" and never schedules a real
    // one. The one symptom: positions stop updating entirely, forever,
    // with no error anywhere - confirmed by hand while building this.
    rafIdRef.current = null;
  }, []);

  return displayed;
}

// Same 6 hex values as .route-hue-0..5 in routeWorkspace.css (which set
// --route-color for the routes table's left-accent border) - one source
// of truth in each language, kept in sync by hand since CSS custom
// properties aren't readable from here without a DOM round-trip. Used
// by FleetMap below so a route's line on the map is the same color as
// its row in the table.
const ROUTE_HUE_COLORS = ['#3b82f6', '#10b981', '#6366f1', '#8b5cf6', '#06b6d4', '#f59e0b'];
const routeColor = (routeIdx) => ROUTE_HUE_COLORS[routeIdx % ROUTE_HUE_COLORS.length];

// A round badge-and-tail glyph for each order stop, not a plain dot -
// and not a google.maps.Symbol path either: on this Maps JS release the
// map defaults to vector (WebGL) rendering, and legacy Marker with a
// Symbol path/label icon (or even Google's own default pin) silently
// mounts with no visible glyph at all under it - confirmed by checking
// Marker.getIcon()/getPosition() after onLoad: both correct, nothing
// painted. google.maps.Map's renderingType can force classic raster
// rendering, but only at construction, and react-google-maps/api reapplies
// `options` via setOptions on every re-render, which throws for that
// property post-construction - not usable through this wrapper. An
// IMAGE-based icon (an actual <img>, not a WebGL-drawn vector path) sidesteps
// the whole issue, so the pin - including its number - is drawn once as
// an SVG data: URI instead of a Symbol+label pair.
//
// A thick white ring (not the old thin teardrop outline) plus a small
// ground shadow is what actually keeps a pin legible now that the map is
// locked to satellite imagery - a photo basemap has none of a vector
// map's flat, predictable color, so a marker needs real contrast of its
// own rather than relying on standing out against a plain background.
function pinIconUrl(color, number, { size = 36, opacity = 1 } = {}) {
  const h = Math.round(size * 1.32);
  const cx = size / 2;
  const r = size / 2 - 2;
  const cy = r + 2;
  const tailHalf = size * 0.15;
  const tailTipY = h - 2;
  const fontSize = Math.max(10, Math.round(r * 1.05));
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${h}" viewBox="0 0 ${size} ${h}">`
    + `<ellipse cx="${cx}" cy="${h - 2}" rx="${size * 0.16}" ry="${size * 0.055}" fill="#000" fill-opacity="${0.28 * opacity}"/>`
    + `<path d="M${cx - tailHalf} ${cy + r * 0.62} L${cx} ${tailTipY} L${cx + tailHalf} ${cy + r * 0.62} Z" `
    + `fill="${color}" fill-opacity="${opacity}" stroke="#fff" stroke-width="2" stroke-linejoin="round"/>`
    + `<circle cx="${cx}" cy="${cy}" r="${r}" fill="${color}" fill-opacity="${opacity}" stroke="#fff" stroke-width="3"/>`
    + `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="#000" stroke-opacity="0.22" stroke-width="1"/>`
    + `<text x="${cx}" y="${cy + 1}" font-family="Arial, Helvetica, sans-serif" font-size="${fontSize}" font-weight="800" `
    + `fill="#fff" text-anchor="middle" dominant-baseline="central">${number}</text>`
    + `</svg>`;
  return {
    url: `data:image/svg+xml;charset=UTF-8,${encodeURIComponent(svg)}`,
    scaledSize: new window.google.maps.Size(size, h),
    // Anchored at the tail's tip (not the glyph's bottom edge) so the pin
    // visually points at the exact coordinate, the way a dropped-pin
    // marker should.
    anchor: new window.google.maps.Point(size / 2, tailTipY),
  };
}

// Same image-icon approach as pinIconUrl, for a plain filled circle - the
// live driver dot and its translucent halo below. Always carries a white
// ring now (previously only when a strokeColor was explicitly passed) for
// the same satellite-contrast reason as the pins above.
function dotIconUrl(color, { size = 16, opacity = 1, strokeColor = '#fff' } = {}) {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">`
    + `<circle cx="${size / 2}" cy="${size / 2}" r="${size / 2 - 1.5}" fill="${color}" fill-opacity="${opacity}" stroke="${strokeColor}" stroke-width="2"/>`
    + `</svg>`;
  return {
    url: `data:image/svg+xml;charset=UTF-8,${encodeURIComponent(svg)}`,
    scaledSize: new window.google.maps.Size(size, size),
    anchor: new window.google.maps.Point(size / 2, size / 2),
  };
}

// A plain dot says "the driver is here" but not which way they're
// actually moving - a real navigation-style arrow (drawn pointing due
// north/up by default) does. Rotated per-instance via a CSS `rotate()`
// transform on the rendered <img> (see FleetMap's pin rendering below),
// not baked into the SVG itself - one static icon shape reused for every
// heading, rather than generating a fresh data URI per rotation.
function arrowIconUrl(color, { size = 26, opacity = 1 } = {}) {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 24 24">`
    + `<path d="M12 1.5 L21 20.5 L12 16 L3 20.5 Z" fill="${color}" fill-opacity="${opacity}" stroke="#fff" stroke-width="2" stroke-linejoin="round"/>`
    + `</svg>`;
  return {
    url: `data:image/svg+xml;charset=UTF-8,${encodeURIComponent(svg)}`,
    scaledSize: new window.google.maps.Size(size, size),
    anchor: new window.google.maps.Point(size / 2, size / 2),
  };
}

// Direction chevrons along a route's line - one at the midpoint of each
// segment long enough to hold one legibly, rotated to point the way the
// route actually travels. Without these, two out-and-back stretches of
// the same line (a real shape when a route doubles back near the depot)
// read as one ambiguous stroke; a chevron makes the direction of travel
// obvious at a glance, the same way a real driving-directions line does.
function directionArrows(pixels) {
  const arrows = [];
  for (let i = 0; i < pixels.length - 1; i++) {
    const a = pixels[i];
    const b = pixels[i + 1];
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const len = Math.hypot(dx, dy);
    if (len < 34) continue; // too short to place a legible arrow on
    arrows.push({
      x: (a.x + b.x) / 2,
      y: (a.y + b.y) / 2,
      angle: (Math.atan2(dy, dx) * 180) / Math.PI,
    });
  }
  return arrows;
}

function capacityText(count, capacity) {
  if (!capacity) return `${count}`;
  if (count >= capacity) return `${count} / ${capacity} — FULL`;
  if (capacity - count === 1) return `${count} / ${capacity} — 1 slot remaining`;
  return `${count} / ${capacity}`;
}

// route.areas sometimes carries the manifest's own stray formatting
// (trailing commas, the same area repeated back-to-back) straight
// through - joined raw, that reads as a comma-studded wall of text
// ("Adambakkam, → Madipakkam → , Madipakkam → neelangari"). Cleaned up
// and capped at `max` entries + a "+N" count instead of dumping every
// area at full length.
function formatAreaPath(areas, max = 2) {
  if (!areas || areas.length === 0) return 'No deliveries yet';
  const cleaned = areas
    // Strip stray commas/whitespace off *either* end - the manifest has
    // had both a trailing "Adambakkam," and a leading ", Madipakkam" in
    // the wild, and only handling one side left the other showing up as
    // a bare " → , " in the joined path.
    .map((a) => (a || '').replace(/^[,\s]+|[,\s]+$/g, ''))
    .filter(Boolean)
    .filter((a, i, arr) => i === 0 || a !== arr[i - 1]);
  if (cleaned.length === 0) return 'No deliveries yet';
  if (cleaned.length <= max) return cleaned.join(' → ');
  return `${cleaned.slice(0, max).join(' → ')} +${cleaned.length - max}`;
}

function CapacityBar({ count, capacity }) {
  const pct = capacity ? Math.min(100, Math.round((count / capacity) * 100)) : 0;
  const state = !capacity ? '' : count >= capacity ? ' capacity-bar__fill--full' : (capacity - count === 1 ? ' capacity-bar__fill--warn' : '');
  return (
    <div className="capacity-bar" role="progressbar" aria-valuenow={count} aria-valuemin={0} aria-valuemax={capacity}>
      <span className={`capacity-bar__fill${state}`} style={{ width: `${pct}%` }} />
    </div>
  );
}

function matchesQuery(order, query) {
  if (!query) return true;
  const q = query.toLowerCase();
  return (
    String(order.order_id ?? '').toLowerCase().includes(q)
    || (order.customer_name || '').toLowerCase().includes(q)
    || (order.area || '').toLowerCase().includes(q)
    || (order.address || '').toLowerCase().includes(q)
  );
}

// Capacity/lateness status only - a route is either empty, has room, is at
// capacity, or has late deliveries. Driver assignment and live GPS tracking
// are a separate concept, shown in the route detail's Driver & Tracking
// card (DriverTrackingCard below) rather than folded into this badge.
function routeStatus(route, capacity) {
  const count = route.orders.length;
  if (count === 0) return 'empty';
  if (route.late_deliveries && route.late_deliveries.length > 0) return 'delayed';
  if (count >= capacity) return 'full';
  return 'open';
}

const STATUS_LABEL = { empty: 'No deliveries', delayed: 'Delayed', full: 'Full', open: 'Open' };

function StatusBadge({ status }) {
  return <span className={`route-status-badge route-status-badge--${status}`}>{STATUS_LABEL[status]}</span>;
}

// The shared identity block for the Unassigned Orders list: Customer ->
// AREA -> full address, in that visual weight, then order id / a
// prominent map pin / secondary info last.
function DeliveryIdentityBlock({ order }) {
  const mapLink = order.map_link || '';
  return (
    <div className="delivery-identity">
      <span className="delivery-identity__customer">{order.customer_name || 'Unnamed customer'}</span>
      {order.area && <span className="delivery-identity__area">{order.area}</span>}
      <span className="delivery-identity__address">{order.address || 'No address on file'}</span>
      <span className="delivery-identity__meta">
        <span>Order #{order.order_id}</span>
        {order.lat != null && order.lng != null ? (
          <span className="delivery-identity__located"><IconCheck width={11} height={11} />Located</span>
        ) : (
          <span className="delivery-identity__unlocated"><IconAlert width={11} height={11} />Needs geocoding</span>
        )}
        {mapLink && (
          <a className="pin-badge" href={mapLink} target="_blank" rel="noopener noreferrer" onClick={(e) => e.stopPropagation()}>
            <IconPin width={13} height={13} />
            View pin
          </a>
        )}
      </span>
    </div>
  );
}

// --------------------------------------------------------------------------
// Routes List — filter bar, wide route table.
// --------------------------------------------------------------------------

function RoutesFilterBar({
  search, onSearchChange, vehicleFilter, onVehicleFilterChange, statusFilter, onStatusFilterChange,
  locationFilter, onLocationFilterChange, locations, onClear, hasActiveFilters,
}) {
  return (
    <div className="routes-filter-bar">
      <div className="search-field routes-filter-bar__search">
        <IconSearch width={14} height={14} />
        <input
          type="text"
          placeholder="Search route, area, order ID, customer…"
          value={search}
          onChange={(e) => onSearchChange(e.target.value)}
        />
      </div>
      <select className="select-compact" value={vehicleFilter} onChange={(e) => onVehicleFilterChange(e.target.value)}>
        <option value="all">All vehicles</option>
        <option value="car">Car</option>
        <option value="bike">Bike</option>
      </select>
      <select className="select-compact" value={statusFilter} onChange={(e) => onStatusFilterChange(e.target.value)}>
        <option value="all">All statuses</option>
        <option value="open">Open</option>
        <option value="full">Full</option>
        <option value="delayed">Delayed</option>
        <option value="empty">No deliveries</option>
      </select>
      {locations.length > 0 && (
        <select className="select-compact" value={locationFilter} onChange={(e) => onLocationFilterChange(e.target.value)}>
          <option value="all">All locations</option>
          {locations.map((loc) => <option key={loc} value={loc}>{loc}</option>)}
        </select>
      )}
      {hasActiveFilters && (
        <button type="button" className="btn btn--ghost routes-filter-bar__clear" onClick={onClear}>
          <IconX width={13} height={13} />
          Clear Filters
        </button>
      )}
    </div>
  );
}

function RowMenu({ route, onDownload, onDeleteRoute, isDeletingRoute }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const onDocClick = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener('mousedown', onDocClick);
    return () => document.removeEventListener('mousedown', onDocClick);
  }, [open]);

  return (
    <div className="row-menu" ref={ref} onClick={(e) => e.stopPropagation()}>
      <button type="button" className="btn btn--ghost row-menu__trigger" onClick={() => setOpen((v) => !v)}>
        More
      </button>
      {open && (
        <div className="row-menu__panel">
          <button type="button" onClick={() => { onDownload(route); setOpen(false); }}>
            <IconDownload width={13} height={13} />
            Download sheet
          </button>
          <button
            type="button"
            className="row-menu__danger"
            disabled={isDeletingRoute === route.route_name}
            onClick={() => {
              setOpen(false);
              if (window.confirm(`Delete ${route.route_name}? Its ${route.orders.length} deliver${route.orders.length === 1 ? 'y' : 'ies'} will move to Unassigned Orders - nothing is deleted.`)) {
                onDeleteRoute(route);
              }
            }}
          >
            {isDeletingRoute === route.route_name ? <span className="spinner" /> : <IconX width={13} height={13} />}
            {isDeletingRoute === route.route_name ? 'Deleting…' : 'Delete Route'}
          </button>
        </div>
      )}
    </div>
  );
}

function RouteRow({ route, routeIdx, capacityFor, onOpen, onDownload, onDeleteRoute, isDeletingRoute, selected, onToggleSelect }) {
  const capacity = capacityFor(route.vehicle_type);
  const count = route.orders.length;
  const status = routeStatus(route, capacity);
  const subtitle = formatAreaPath(route.areas, 2);

  const isDeleting = isDeletingRoute === route.route_name;

  return (
    <div
      className={`route-row route-hue-${routeIdx % 6}${selected ? ' route-row--selected' : ''}${isDeleting ? ' route-row--busy' : ''}`}
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => { if (e.key === 'Enter') onOpen(); }}
    >
      {isDeleting && (
        <div className="busy-overlay">
          <span className="spinner" />
        </div>
      )}
      <div className="route-row__cell route-row__cell--select" onClick={(e) => e.stopPropagation()}>
        <input
          type="checkbox"
          checked={selected}
          onChange={() => onToggleSelect(route.route_name)}
          aria-label={`Select ${route.route_name}`}
        />
      </div>
      <div className="route-row__cell route-row__cell--route">
        <span className="route-row__name">{route.route_name}</span>
        <span className="route-row__subtitle">{subtitle}</span>
      </div>
      <div className="route-row__cell route-row__cell--vehicle">
        <span className={`vehicle-pill vehicle-pill--${route.vehicle_type === 'car' ? 'car' : 'bike'}`}>
          {route.vehicle_type === 'car' ? <IconCar width={12} height={12} /> : <IconBike width={12} height={12} />}
          {route.vehicle_type === 'car' ? 'Car' : 'Bike'}
        </span>
      </div>
      <div className="route-row__cell route-row__cell--stops">
        <span className="mono-num">{count}</span>
        <span className="route-row__muted"> stops</span>
      </div>
      <div className="route-row__cell route-row__cell--progress">
        {/* "12 / 6" alone doesn't say what the numbers mean - spelling it
            out as stops-of-capacity reads correctly at a glance instead
            of needing the column header for context. */}
        <span className="route-row__progress-text">{count} of {capacity} stops</span>
        <CapacityBar count={count} capacity={capacity} />
      </div>
      <div className="route-row__cell route-row__cell--distance mono-num">
        {route.route_distance_km != null ? `${route.route_distance_km} km` : '—'}
      </div>
      <div className="route-row__cell route-row__cell--eta mono-num">
        {route.estimated_finish_time || '—'}
      </div>
      <div className="route-row__cell route-row__cell--status">
        <StatusBadge status={status} />
      </div>
      <div className="route-row__cell route-row__cell--actions">
        <button type="button" className="btn btn--outline route-row__view" onClick={(e) => { e.stopPropagation(); onOpen(); }}>
          View
        </button>
        <RowMenu route={route} onDownload={onDownload} onDeleteRoute={onDeleteRoute} isDeletingRoute={isDeletingRoute} />
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// Fleet map — every visible route on one Google Map at once (Map/Split
// view). Distinct from LiveMapModal above: that one is a single route's
// live driver position, polled every 3s; this one is the static planned
// shape of every route in `routes`, fetched once per route set from the
// same real backend endpoint (GET /api/routes/{id}/route-path) LiveMapModal
// already uses - no new endpoint, no simulated geometry.
// --------------------------------------------------------------------------
function FleetMap({
  routes, fetchRoutePlannedPath, fetchRouteTracking, requestedLiveTracking, selectedRouteName, onSelectRoute, baseColorIndex = 0,
  // The fleet-wide Map/Split view only polls tracking for routes whose
  // *cached* route_run_status/driver_id already say "in progress with a
  // driver" (trackableRoutes below) - deliberately, to stay cheap however
  // many routes are on the board. But that cache is exactly the `routes`
  // prop as it was last fetched from the backend, which never refreshes on
  // its own - a driver starting their route from their own app updates the
  // real backend the instant it happens, with nothing pushing that change
  // into this already-open admin tab. RouteDetail's embedded single-route
  // map passes this to skip that (possibly stale) gate entirely and just
  // poll the one route it's already scoped to - which is exactly why the
  // "Live tracking" switch could look like it does nothing there: the gate
  // was silently blocking the poll, not the poll itself failing.
  alwaysTrackable = false,
}) {
  const { isLoaded, loadError } = useJsApiLoader({
    googleMapsApiKey: GOOGLE_MAPS_API_KEY,
    libraries: GOOGLE_MAPS_LIBRARIES,
  });
  const mapRef = useRef(null);
  const [paths, setPaths] = useState({}); // route_id -> [{lat,lng}, ...]
  const [pathsLoading, setPathsLoading] = useState(false);
  const [pathsRefreshNonce, setPathsRefreshNonce] = useState(0);
  const [liveTracking, setLiveTracking] = useState(false);
  const [driverPositions, setDriverPositions] = useState({}); // route_id -> {lat,lng,recordedAt}
  const routeIds = routes.map((r) => r.route_id).join(',');
  // Unlike routeIds above, this also changes when a route's *stop
  // composition or sequence* changes - reordering within a route, or an
  // admin moving a stop from one route to another (both routes keep the
  // same route_id the whole time, so routeIds alone never notices). That
  // used to mean the drawn line - fetched once per route_id and cached in
  // `paths` - went stale the moment an admin edited a route: pins would
  // move (they're computed fresh from route.orders every render) but the
  // line connecting them would keep tracing the route's *old* shape.
  const routeSequenceKey = routes
    .map((r) => `${r.route_id}:${(r.orders || []).map((o) => o.order_id).join(',')}`)
    .join('|');

  // Pins are plain positioned <img> elements over the map, not
  // google.maps.Marker - on this Maps JS release the map defaults to
  // vector (WebGL) rendering, under which Marker (icon, label, even
  // Google's own default pin) mounts with a correct position/icon
  // (confirmed via Marker.onLoad/getIcon()) but paints nothing at all.
  // Polyline turned out unreliable here too (rendered in some sessions,
  // silently not in others) - route lines are drawn as plain SVG below
  // for the same reason, not google.maps.Polyline. LiveMapModal above
  // still uses both Marker and Polyline for a single route's live view;
  // it hasn't shown the same symptom in testing, but it's the same
  // underlying API and hasn't been proven immune either.
  // Position is a straight linear map of lat/lng onto the visible
  // viewport's current north-east/south-west corners (map.getBounds())
  // and the container's own pixel size - not Mercator-exact at very
  // wide (whole-country) zooms, but well within a pixel or two of exact
  // at the city-block-to-city-wide zooms this map actually runs at.
  // Recomputed on the map's own 'idle' (view has finished changing,
  // including after fitBounds) and 'bounds_changed' events - a more
  // reliable "the view is settled, read it now" signal here than
  // OverlayView.draw() turned out to be (draw() fired, but on a
  // stale/earlier view - the first version of this code read
  // fromLatLngToDivPixel() through it and consistently placed most
  // pins outside the visible canvas).
  const [viewport, setViewport] = useState(null); // {ne:{lat,lng}, sw:{lat,lng}, w, h}
  const projectPoint = (lat, lng) => {
    if (!viewport) return null;
    const { ne, sw, w, h } = viewport;
    if (ne.lng === sw.lng || ne.lat === sw.lat) return null;
    return {
      x: ((lng - sw.lng) / (ne.lng - sw.lng)) * w,
      y: ((ne.lat - lat) / (ne.lat - sw.lat)) * h,
    };
  };

  // "Live Tracking" in the sidebar nav requests this on (see
  // requestedView/requestedLiveTracking in RouteWorkspace) - a one-way
  // trigger, not a controlled prop, so the toggle below still switches
  // freely afterward.
  useEffect(() => {
    if (requestedLiveTracking) setLiveTracking(true);
  }, [requestedLiveTracking]);

  // Refetched whenever the route set changes OR any route's own stop
  // composition/sequence changes (routeSequenceKey - see above), and also
  // on demand via the toolbar's Refresh button (pathsRefreshNonce) for the
  // rare edit this key doesn't catch (e.g. the backend's road-snapped
  // shape between two stops shifting without the stop list itself
  // changing) and as a plain manual "I want to be sure" control.
  useEffect(() => {
    let cancelled = false;
    setPathsLoading(true);
    Promise.all(routes.map((route) => (
      fetchRoutePlannedPath(route.route_id)
        .then((data) => {
          if (cancelled) return;
          setPaths((prev) => ({ ...prev, [route.route_id]: data.path || [] }));
        })
        .catch(() => {})
    ))).finally(() => { if (!cancelled) setPathsLoading(false); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routeSequenceKey, pathsRefreshNonce]);

  // Which routes actually have someone to track right now - a driver
  // assigned AND the route genuinely started, not just planned. Real
  // signal already on every route (route_run_status/driver_id), not
  // inferred - except when alwaysTrackable overrides this (see its
  // comment above): the single route RouteDetail already scoped this map
  // to is trackable by definition, whatever this possibly-stale field
  // currently says.
  const trackableRoutes = useMemo(
    () => (alwaysTrackable ? routes : routes.filter((r) => r.route_run_status === 'in_progress' && r.driver_id != null)),
    [routes, alwaysTrackable]
  );
  const trackableIds = trackableRoutes.map((r) => r.route_id).join(',');

  // A fix from either channel below only replaces what's already stored
  // when it's actually newer - the REST poll and the WebSocket push (just
  // below) race by design (the push is the fast path, the poll is the
  // resilient fallback - see useTrackingSocket's comment), so without
  // this a slightly-delayed poll response could land after a fresher push
  // already rendered and yank the dot back to an older position.
  const applyLocationFix = (routeId, lat, lng, recordedAt, heading) => {
    setDriverPositions((prev) => {
      const existing = prev[routeId];
      if (existing?.recordedAt && recordedAt && existing.recordedAt >= recordedAt) return prev;
      // Below the GPS noise floor (see GPS_NOISE_METERS), the vehicle
      // isn't really moving - so the heading shouldn't drift either, the
      // same reasoning useAnimatedPositions applies to position. Without
      // this, a parked driver's arrow could still wobble a degree or two
      // between noisy-but-technically-different headings on each fix.
      const isNoise = existing && haversineMeters(existing, { lat, lng }) < GPS_NOISE_METERS;
      // GPS heading is only meaningful while actually moving - most phones
      // report null/undefined for it the instant the vehicle stops, which
      // would otherwise snap the arrow back to "pointing north" every time
      // a driver pauses at a light or a stop. Keep showing the last real
      // heading instead of a fabricated one.
      const resolvedHeading = !isNoise && heading != null && !Number.isNaN(heading) ? heading : (existing?.heading ?? 0);
      return { ...prev, [routeId]: { lat, lng, recordedAt, heading: resolvedHeading } };
    });
  };

  // Polls every in-progress route's real tracking endpoint - the same
  // GET /api/routes/{id}/tracking LiveMapModal already uses for one
  // route at a time - while the live layer is switched on. A fan-out
  // over however many routes are actually running right now, not every
  // route on the board, so this stays cheap regardless of fleet size.
  // Kept running at its original cadence even with the WebSocket push
  // below doing most of the real work now - this is what still updates
  // the dot on a first load (before any new ping has fired yet), and what
  // keeps tracking working at all in an environment that blocks WebSocket
  // upgrades outright.
  useEffect(() => {
    if (!liveTracking || trackableRoutes.length === 0) return undefined;
    let cancelled = false;
    const poll = () => {
      trackableRoutes.forEach((route) => {
        fetchRouteTracking(route.route_id)
          .then((data) => {
            if (cancelled) return;
            const last = data?.last_location;
            if (!last) {
              setDriverPositions((prev) => {
                if (!(route.route_id in prev)) return prev;
                const next = { ...prev };
                delete next[route.route_id];
                return next;
              });
              return;
            }
            applyLocationFix(route.route_id, last.lat, last.lng, last.recorded_at, last.heading);
          })
          .catch(() => {});
      });
    };
    poll();
    const interval = setInterval(poll, 5000);
    return () => { cancelled = true; clearInterval(interval); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [liveTracking, trackableIds]);

  // The fast path - see useTrackingSocket's own comment for the full
  // picture. Every connected admin tab gets every location ping the
  // instant a driver's phone sends it, without waiting out the 5s poll
  // above.
  useTrackingSocket(liveTracking, (msg) => {
    if (msg?.type !== 'location' || msg.route_id == null) return;
    if (!trackableRoutes.some((r) => r.route_id === msg.route_id)) return;
    applyLocationFix(msg.route_id, msg.lat, msg.lng, msg.recorded_at, msg.heading);
  });

  // Clear stale dots the instant tracking is switched off, rather than
  // leaving the last-known positions frozen on the map.
  useEffect(() => { if (!liveTracking) setDriverPositions({}); }, [liveTracking]);

  // Smoothed, continuously-gliding version of driverPositions for
  // rendering - see useAnimatedPositions' own comment for why this is
  // what actually fixes "looks laggy/jumpy" (both channels above still
  // deliver periodic fixes, not a continuous stream; this is what turns
  // those into visually continuous motion).
  const animatedDriverPositions = useAnimatedPositions(driverPositions);

  const allStops = useMemo(
    () => routes.flatMap((route) => (route.orders || []).filter((o) => o.lat != null && o.lng != null).map((o) => ({ lat: o.lat, lng: o.lng }))),
    [routes]
  );

  const fitToRoutes = () => {
    const map = mapRef.current;
    if (!map || !window.google || allStops.length === 0) return;
    const bounds = new window.google.maps.LatLngBounds();
    allStops.forEach((s) => bounds.extend(s));
    map.fitBounds(bounds, 48);
  };

  // Auto-fit once the map and the first batch of stops are both ready -
  // after that, "Fit to routes" (below) is the explicit re-trigger so a
  // dispatcher's own zoom/pan while inspecting a route isn't fought on
  // every unrelated re-render.
  const fittedRef = useRef(false);
  useEffect(() => { fittedRef.current = false; }, [routeIds]);
  useEffect(() => {
    if (fittedRef.current || !mapRef.current || allStops.length === 0) return;
    fittedRef.current = true;
    fitToRoutes();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allStops.length, isLoaded]);

  if (loadError) return <div className="empty-state fleet-map__empty">Could not load Google Maps.</div>;
  if (!isLoaded) return <div className="empty-state fleet-map__empty">Loading map…</div>;
  if (allStops.length === 0) return <div className="empty-state fleet-map__empty"><IconPin width={22} height={22} />No geocoded stops to show on the map yet.</div>;

  return (
    <div className="fleet-map">
      <GoogleMap
        mapContainerClassName="fleet-map__canvas"
        center={allStops[0]}
        zoom={12}
        onLoad={(map) => {
          mapRef.current = map;
          fitToRoutes();
          const updateViewport = () => {
            const bounds = map.getBounds();
            const div = map.getDiv();
            if (!bounds || !div) return;
            const ne = bounds.getNorthEast();
            const sw = bounds.getSouthWest();
            setViewport({
              ne: { lat: ne.lat(), lng: ne.lng() },
              sw: { lat: sw.lat(), lng: sw.lng() },
              w: div.offsetWidth,
              h: div.offsetHeight,
            });
          };
          map.addListener('idle', updateViewport);
          map.addListener('bounds_changed', updateViewport);
          // The route detail drawer this map can sit inside slides in via
          // a CSS transform (.route-drawer-slide-in, ~200ms) - if onLoad
          // fires while that's still running, fitBounds computes against
          // the container's mid-transition size and lands on the wrong
          // zoom/center. A resize nudge + re-fit once the animation has
          // definitely settled corrects it (the 'idle' after this re-fit
          // is what actually lands viewport on the right numbers);
          // harmless where there's no animation (the fleet-wide Map/Split
          // view) too.
          window.setTimeout(() => {
            if (mapRef.current !== map) return;
            window.google.maps.event.trigger(map, 'resize');
            fitToRoutes();
          }, 350);
        }}
        options={{
          zoomControl: true, streetViewControl: false,
          // Locked to satellite, no switcher - imagery is what makes the
          // big numbered pins and route lines actually mean something
          // (an admin can see the real driveway/building a pin sits on,
          // not just an abstract road diagram), and mapTypeId is a plain
          // option react-google-maps/api can safely reapply on every
          // re-render (unlike renderingType above), so there's no risk of
          // it silently reverting.
          mapTypeControl: false,
          mapTypeId: 'satellite',
          fullscreenControl: false, gestureHandling: 'greedy',
          backgroundColor: '#0b0f14',
        }}
      >
      </GoogleMap>

      {/* Route lines - drawn as plain SVG, not google.maps.Polyline, for
          the same reason pins are plain <img>s (see the comment above
          projectPoint): this Maps JS release's vector rendering doesn't
          reliably paint classic overlays. Same projectPoint pixel math as
          the pins below, so a route's line and its stops are always
          exactly aligned - no separate coordinate system to drift out of
          sync. Each route draws as three passes - a white halo stroke
          underneath, the colored line on top of it, then direction
          chevrons - because a satellite basemap has none of a flat vector
          map's predictable color to read a thin colored line against; the
          halo is what actually makes the line visible over open ground,
          water, or a busy rooftop. */}
      <svg className="fleet-map__lines">
        {routes.map((route, idx) => {
          const color = routeColor(baseColorIndex + idx);
          const selected = selectedRouteName === route.route_name;
          const dimmed = selectedRouteName != null && !selected;
          const path = paths[route.route_id];
          const stops = (route.orders || []).filter((o) => o.lat != null && o.lng != null);
          const linePoints = path && path.length > 1
            ? path
            : stops.length > 1 ? stops.map((o) => ({ lat: o.lat, lng: o.lng })) : null;
          if (!linePoints) return null;
          const pixels = linePoints.map((p) => projectPoint(p.lat, p.lng)).filter(Boolean);
          if (pixels.length < 2) return null;
          const pointsAttr = pixels.map((p) => `${p.x},${p.y}`).join(' ');
          const opacity = dimmed ? 0.3 : 0.95;
          return (
            <g key={route.route_name}>
              <polyline
                points={pointsAttr}
                fill="none"
                stroke="#fff"
                strokeWidth={selected ? 8 : 6.5}
                strokeOpacity={dimmed ? 0.3 : 0.75}
                strokeLinecap="round"
                strokeLinejoin="round"
                pointerEvents="none"
              />
              <polyline
                points={pointsAttr}
                fill="none"
                stroke={color}
                strokeWidth={selected ? 4.5 : 3.25}
                strokeOpacity={opacity}
                strokeLinecap="round"
                strokeLinejoin="round"
                style={{ cursor: 'pointer', pointerEvents: 'stroke' }}
                onClick={() => onSelectRoute(route.route_name)}
              />
              {directionArrows(pixels).map((arrow, arrowIdx) => (
                <polygon
                  key={arrowIdx}
                  points="-5,-3.5 4.5,0 -5,3.5"
                  fill={color}
                  stroke="#fff"
                  strokeWidth={1}
                  strokeLinejoin="round"
                  opacity={opacity}
                  transform={`translate(${arrow.x} ${arrow.y}) rotate(${arrow.angle})`}
                  pointerEvents="none"
                />
              ))}
            </g>
          );
        })}
      </svg>

      {/* `viewport` state (set by the idle/bounds_changed listeners in
          onLoad above) is what makes this block recompute after every
          pan/zoom/resize/fitBounds - projectPoint() reads it fresh on
          every render. */}
      <div className="fleet-map__pins">
        {routes.flatMap((route, idx) => {
          const color = routeColor(baseColorIndex + idx);
          const selected = selectedRouteName === route.route_name;
          const dimmed = selectedRouteName != null && !selected;
          const stops = (route.orders || []).filter((o) => o.lat != null && o.lng != null);
          const pins = stops.map((o, stopIdx) => {
            const pos = projectPoint(o.lat, o.lng);
            if (!pos) return null;
            const icon = pinIconUrl(color, stopIdx + 1, { size: selected ? 44 : 36, opacity: dimmed ? 0.4 : 1 });
            return (
              <img
                key={o.order_id}
                src={icon.url}
                width={icon.scaledSize.width}
                height={icon.scaledSize.height}
                alt=""
                className="fleet-map__pin"
                style={{ left: pos.x, top: pos.y, zIndex: selected ? 4 : 2 }}
                onClick={() => onSelectRoute(route.route_name)}
                title={`${route.route_name} · stop ${stopIdx + 1}: ${o.customer_name || 'Stop'}`}
              />
            );
          });
          const driverPos = liveTracking && driverPositions[route.route_id];
          if (driverPos) {
            // Falls back to the raw fix for the one frame or two before
            // useAnimatedPositions' own rAF loop has run yet - never
            // actually visible, just avoids a same-frame "fix arrived but
            // nothing rendered" gap.
            const animated = animatedDriverPositions[route.route_id] || driverPos;
            const p = projectPoint(animated.lat, animated.lng);
            if (p) {
              const halo = dotIconUrl(color, { size: 40, opacity: 0.18 });
              const arrow = arrowIconUrl(color, { size: 26 });
              pins.push(
                <img key={`${route.route_name}-driver-halo`} src={halo.url} width={40} height={40} alt="" className="fleet-map__pin fleet-map__pin--dot" style={{ left: p.x, top: p.y, zIndex: 5, pointerEvents: 'none' }} />,
                // Rotated per the driver's real GPS heading (see
                // applyLocationFix - not animated the way position is,
                // just snaps to each new reading) so this actually shows
                // which way they're moving, not just where they are. The
                // rotation has to be set inline, alongside the same
                // translate(-50%,-50%) centering .fleet-map__pin--dot
                // normally supplies via CSS - an inline `transform`
                // replaces the class's entirely rather than combining
                // with it.
                <img
                  key={`${route.route_name}-driver`}
                  src={arrow.url}
                  width={26}
                  height={26}
                  alt=""
                  className="fleet-map__pin fleet-map__pin--dot"
                  style={{ left: p.x, top: p.y, zIndex: 6, transform: `translate(-50%, -50%) rotate(${driverPos.heading || 0}deg)` }}
                  title={`${route.route_name} · driver`}
                />
              );
            }
          }
          return pins;
        })}
      </div>

      <div className="fleet-map__toolbar">
        <button type="button" className="fleet-map__toolbar-btn" onClick={fitToRoutes} title="Fit to routes">
          <IconGauge width={15} height={15} />
        </button>
        {/* Manual re-fetch of every route's line, for the one thing
            routeSequenceKey above can't catch on its own (the backend's
            road-snapped shape shifting without the stop list itself
            changing) and as a plain "make sure this is current" control
            after any edit - reordering a stop, adding one, or moving one
            from another route (see routeSequenceKey's comment for what
            already updates automatically). */}
        <button
          type="button"
          className="fleet-map__toolbar-btn"
          onClick={() => setPathsRefreshNonce((n) => n + 1)}
          disabled={pathsLoading}
          title="Refresh route lines"
        >
          {pathsLoading ? <span className="spinner" /> : <IconRefresh width={15} height={15} />}
        </button>
      </div>

      <div className="fleet-map__live-toggle">
        <span>Live tracking</span>
        <button
          type="button"
          className={`switch${liveTracking ? ' switch--on' : ''}`}
          onClick={() => setLiveTracking((v) => !v)}
          role="switch"
          aria-checked={liveTracking}
        >
          <span className="switch__knob" />
        </button>
      </div>

      <div className="fleet-map__legend">
        {routes.map((route, idx) => (
          <button
            type="button"
            key={route.route_name}
            className={`fleet-map__legend-item${selectedRouteName === route.route_name ? ' fleet-map__legend-item--active' : ''}`}
            onClick={() => onSelectRoute(route.route_name)}
          >
            <span className="fleet-map__legend-swatch" style={{ background: routeColor(baseColorIndex + idx) }} />
            {route.route_name}
          </button>
        ))}
      </div>

      {/* alwaysTrackable makes trackableRoutes.length === 0 meaningless
          here (it's never empty - see its definition above), so the
          single-route embed instead checks whether a position actually
          came back yet. */}
      {liveTracking && (alwaysTrackable ? Object.keys(driverPositions).length === 0 : trackableRoutes.length === 0) && (
        <div className="fleet-map__live-empty">
          <IconLocate width={15} height={15} />
          {alwaysTrackable
            ? 'No live location yet for this route\'s driver — it appears as soon as their app reports a position.'
            : 'No drivers on the road right now — a route shows up here once its driver starts it.'}
        </div>
      )}
    </div>
  );
}

// Compact left-hand list for the Split view - route id, vehicle, stop
// count, status, and the same color swatch as its line on FleetMap.
// Not the full RoutesTable (8 columns doesn't fit a half-width panel);
// clicking a row highlights that route on the map instead of opening it.
function SplitRouteList({ routes, capacityFor, selectedRouteName, onSelectRoute }) {
  return (
    <div className="split-route-list">
      {routes.map((route, idx) => {
        const capacity = capacityFor(route.vehicle_type);
        const status = routeStatus(route, capacity);
        const selected = selectedRouteName === route.route_name;
        return (
          <button
            type="button"
            key={route.route_name}
            className={`split-route-row${selected ? ' split-route-row--selected' : ''}`}
            onClick={() => onSelectRoute(selected ? null : route.route_name)}
          >
            <span className="split-route-row__swatch" style={{ background: routeColor(idx) }} />
            <span className="split-route-row__body">
              <span className="split-route-row__name">{route.route_name}</span>
              <span className="split-route-row__meta">
                {route.vehicle_type === 'car' ? <IconCar width={11} height={11} /> : <IconBike width={11} height={11} />}
                {route.orders.length} of {capacity} stops
              </span>
            </span>
            <StatusBadge status={status} />
          </button>
        );
      })}
    </div>
  );
}

function RoutesTable({
  routes, capacityFor, onOpen, onDownload, onDeleteRoute, isDeletingRoute, hasActiveFilters, onClearFilters,
  selectedRouteNames, onToggleSelect, onToggleSelectAll,
}) {
  if (routes.length === 0) {
    return (
      <div className="empty-state">
        <IconSearch width={22} height={22} />
        No routes match your current filters.
        {hasActiveFilters && (
          <button type="button" className="btn btn--ghost" onClick={onClearFilters}>Clear Filters</button>
        )}
      </div>
    );
  }

  const allSelected = routes.length > 0 && routes.every((r) => selectedRouteNames.includes(r.route_name));

  return (
    <div className="routes-table">
      <div className="routes-table__head">
        <div className="route-row__cell route-row__cell--select">
          <input
            type="checkbox"
            checked={allSelected}
            onChange={() => onToggleSelectAll(routes.map((r) => r.route_name))}
            aria-label="Select all routes"
          />
        </div>
        <div className="route-row__cell route-row__cell--route">Route</div>
        <div className="route-row__cell route-row__cell--vehicle">Vehicle</div>
        <div className="route-row__cell route-row__cell--stops">Stops</div>
        <div className="route-row__cell route-row__cell--progress">Progress</div>
        <div className="route-row__cell route-row__cell--distance">Distance</div>
        <div className="route-row__cell route-row__cell--eta">Finish ETA</div>
        <div className="route-row__cell route-row__cell--status">Status</div>
        <div className="route-row__cell route-row__cell--actions">Actions</div>
      </div>
      <div className="routes-table__body">
        {routes.map((route, idx) => (
          <RouteRow
            key={route.route_name}
            route={route}
            routeIdx={idx}
            capacityFor={capacityFor}
            onOpen={() => onOpen(route.route_name)}
            onDownload={onDownload}
            onDeleteRoute={onDeleteRoute}
            isDeletingRoute={isDeletingRoute}
            selected={selectedRouteNames.includes(route.route_name)}
            onToggleSelect={onToggleSelect}
          />
        ))}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// Route Detail — header, overview strip, spacious stop list.
// --------------------------------------------------------------------------

function RouteOverviewStrip({ route, capacity, maxCapacity }) {
  const count = route.orders.length;
  const hasFlexRoom = maxCapacity > capacity;
  const stats = [
    { label: 'Vehicle type', value: route.vehicle_type === 'car' ? 'Car' : 'Bike' },
    { label: 'Addresses', value: `${count} of ${maxCapacity}` },
    ...(hasFlexRoom ? [
      { label: 'Base capacity', value: capacity },
      { label: 'Additional addresses', value: Math.max(count - capacity, 0) },
    ] : []),
    { label: 'Areas', value: route.areas && route.areas.length ? route.areas.length : '—' },
    { label: 'Distance', value: route.route_distance_km != null ? `${route.route_distance_km} km` : '—' },
    { label: 'Travel time', value: route.route_time_minutes != null ? `${route.route_time_minutes} min` : '—' },
    { label: 'Avg / stop', value: route.average_stop_time != null ? `${route.average_stop_time} min` : '—' },
    { label: 'Finish ETA', value: route.estimated_finish_time || '—' },
  ];
  return (
    <div className="route-overview">
      {stats.map((stat) => (
        <div key={stat.label} className="route-overview__item">
          <span className="route-overview__label">{stat.label}</span>
          <span className="route-overview__value mono-num">{stat.value}</span>
        </div>
      ))}
    </div>
  );
}

// "Add Address from Another Route" - the modal for pulling stops directly
// from a different route once this one is at (or past) its base capacity.
// Capped at the destination's max capacity (10 for a car; a bike has no
// flex room and never gets this button in the first place).
function AddAddressModal({ destinationRoute, routes, capacityFor, maxCapacityFor, onConfirm, onClose, isSubmitting }) {
  const eligibleSourceRoutes = routes.filter((r) => r.route_name !== destinationRoute.route_name && r.orders.length > 0);
  const [sourceRouteName, setSourceRouteName] = useState(eligibleSourceRoutes[0]?.route_name || '');
  const [selectedIds, setSelectedIds] = useState([]);

  const sourceRoute = eligibleSourceRoutes.find((r) => r.route_name === sourceRouteName) || null;
  const maxCapacity = maxCapacityFor(destinationRoute.vehicle_type);
  const destinationCount = destinationRoute.orders.length;
  const remainingSlots = Math.max(maxCapacity - destinationCount, 0);
  const overSelected = selectedIds.length > remainingSlots;

  const toggleId = (orderId) => {
    const id = String(orderId);
    setSelectedIds((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  };

  const changeSource = (name) => { setSourceRouteName(name); setSelectedIds([]); };

  const handleConfirm = async () => {
    if (!sourceRoute || selectedIds.length === 0 || overSelected) return;
    const ok = await onConfirm(sourceRoute.route_id, selectedIds);
    if (ok) onClose();
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal modal--wide" onClick={(e) => e.stopPropagation()}>
        <div className="modal__header">
          <h3>Add Address to Route</h3>
          <button type="button" className="modal__close" onClick={onClose}><IconX width={16} height={16} /></button>
        </div>

        <div className="modal__meta">
          <span>{destinationRoute.route_name}</span>
          <span>Vehicle: {destinationRoute.vehicle_type === 'car' ? 'Car' : 'Bike'}</span>
          <span>Current addresses: <strong className="mono-num">{destinationCount} / {maxCapacity}</strong></span>
        </div>

        <div className="modal__body">
          <label className="modal__field-label" htmlFor="add-address-source">Select Source Route</label>
          {eligibleSourceRoutes.length === 0 ? (
            <div className="empty-state">No other routes have addresses available to move.</div>
          ) : (
            <>
              <select
                id="add-address-source"
                className="select-compact modal__source-select"
                value={sourceRouteName}
                onChange={(e) => changeSource(e.target.value)}
              >
                {eligibleSourceRoutes.map((r) => (
                  <option key={r.route_name} value={r.route_name}>
                    {r.route_name} ({r.orders.length}/{capacityFor(r.vehicle_type)} · {r.vehicle_type === 'car' ? 'Car' : 'Bike'})
                  </option>
                ))}
              </select>

              <div className="modal__address-list">
                {sourceRoute?.orders.map((order) => (
                  <label key={order.order_id} className="address-pick-row">
                    <input
                      type="checkbox"
                      checked={selectedIds.includes(String(order.order_id))}
                      onChange={() => toggleId(order.order_id)}
                    />
                    <div className="address-pick-row__body">
                      <div className="address-pick-row__top">
                        <span className="address-pick-row__order-id">Order #{order.order_id}</span>
                        <span className="address-pick-row__customer">{order.customer_name}</span>
                        {order.is_late && <span className="stop-status stop-status--late">🔴 Late</span>}
                      </div>
                      {order.area && <span className="route-stop__area">{order.area}</span>}
                      <span className="address-pick-row__address">{order.address}</span>
                      <span className="address-pick-row__meta">
                        Current route: {sourceRoute.route_name} · Slot {order.delivery_time}
                      </span>
                    </div>
                  </label>
                ))}
              </div>
            </>
          )}
        </div>

        {eligibleSourceRoutes.length > 0 && (
          <div className="modal__summary">
            <span>Selected: <strong className="mono-num">{selectedIds.length} address{selectedIds.length === 1 ? '' : 'es'}</strong></span>
            <span>Destination Route: <strong className="mono-num">{destinationCount + selectedIds.length} / {maxCapacity}</strong></span>
          </div>
        )}
        {overSelected && (
          <div className="modal__warning">
            <IconAlert width={13} height={13} />
            Only {remainingSlots} address slot{remainingSlots === 1 ? '' : 's'} {remainingSlots === 1 ? 'is' : 'are'} available. Deselect {selectedIds.length - remainingSlots} to continue.
          </div>
        )}

        <div className="modal__footer">
          <button type="button" className="btn btn--ghost" onClick={onClose}>Cancel</button>
          <button
            type="button"
            className="btn btn--primary"
            disabled={!sourceRoute || selectedIds.length === 0 || overSelected || isSubmitting}
            onClick={handleConfirm}
          >
            {isSubmitting ? 'Adding…' : `Add ${selectedIds.length || ''} Address${selectedIds.length === 1 ? '' : 'es'}`.trim()}
          </button>
        </div>
      </div>
    </div>
  );
}

const TRACKING_STATUS_LABEL = {
  not_started: { emoji: '⚪', text: 'Not Started' },
  live: { emoji: '🟢', text: 'Live' },
  delayed: { emoji: '🟠', text: 'Delayed' },
  offline: { emoji: '🔴', text: 'Offline' },
  completed: { emoji: '⚫', text: 'Completed' },
};

function TrackingStatusPill({ status }) {
  const info = TRACKING_STATUS_LABEL[status] || TRACKING_STATUS_LABEL.not_started;
  return <span className={`tracking-status-pill tracking-status-pill--${status}`}>{info.emoji} {info.text}</span>;
}

// Pick a driver for this route - separate from the "conflict" flow the
// backend can hand back (the same driver already running a different
// route), which this surfaces as a plain confirm rather than a second
// modal, since it's a rare, single yes/no decision.
function AssignDriverModal({ route, drivers, onAssign, onClose }) {
  const activeDrivers = drivers.filter((d) => d.status === 'active');
  const [driverId, setDriverId] = useState(activeDrivers[0]?.id ? String(activeDrivers[0].id) : '');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState(null);

  const doAssign = async (force) => {
    if (!driverId) return;
    setIsSubmitting(true);
    setError(null);
    try {
      const result = await onAssign(Number(driverId), force);
      if (result.conflict) {
        if (window.confirm(`${result.message}`)) {
          await doAssign(true);
          return;
        }
        setIsSubmitting(false);
        return;
      }
      onClose();
    } catch (err) {
      setError(err.message || 'Could not assign driver.');
      setIsSubmitting(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal__header">
          <h3>Assign Driver — {route.route_name}</h3>
          <button type="button" className="modal__close" onClick={onClose}><IconX width={16} height={16} /></button>
        </div>
        <div className="modal__body">
          {activeDrivers.length === 0 ? (
            <div className="empty-state">No active drivers yet - add one from the Drivers page first.</div>
          ) : (
            <>
              <label className="modal__field-label" htmlFor="assign-driver-select">Driver</label>
              <select
                id="assign-driver-select"
                className="select-compact modal__source-select"
                value={driverId}
                onChange={(e) => setDriverId(e.target.value)}
              >
                {activeDrivers.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name} ({d.driver_code}){d.assigned_route_name ? ` — currently on ${d.assigned_route_name}` : ''}
                  </option>
                ))}
              </select>
            </>
          )}
        </div>
        {error && <div className="modal__warning"><IconAlert width={13} height={13} />{error}</div>}
        <div className="modal__footer">
          <button type="button" className="btn btn--ghost" onClick={onClose}>Cancel</button>
          <button type="button" className="btn btn--primary" disabled={!driverId || isSubmitting} onClick={() => doAssign(false)}>
            {isSubmitting ? 'Assigning…' : 'Assign Driver'}
          </button>
        </div>
      </div>
    </div>
  );
}

function timeAgo(iso) {
  const seconds = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  return `${minutes}m ago`;
}

// Straight-line distance in meters between two {lat, lng} points - used
// only to tell "the driver actually moved" apart from GPS noise (a
// stationary phone's reported fix still drifts a few meters between
// pings), not for anything that needs real accuracy.
function haversineMeters(a, b) {
  const R = 6371000;
  const toRad = (deg) => (deg * Math.PI) / 180;
  const dLat = toRad(b.lat - a.lat);
  const dLng = toRad(b.lng - a.lng);
  const s = Math.sin(dLat / 2) ** 2
    + Math.cos(toRad(a.lat)) * Math.cos(toRad(b.lat)) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(s));
}

// The actual "watch them move" view - "Track Driver" used to just open a
// static Google Maps pin frozen at whatever the last location happened to
// be the moment you clicked it, with no way to see it update. This is a
// real embedded Google Map (JS API - switched back from the earlier
// Leaflet/OSM version now that a Maps key is in use) that re-polls
// tracking on its own faster timer and pans to the driver's marker every
// time it actually moves, plus the breadcrumb trail already available
// from the tracking endpoint's `path`.
function LiveMapModal({ route, driver, initialTracking, fetchRouteTracking, fetchRoutePlannedPath, onClose }) {
  const [tracking, setTracking] = useState(initialTracking);
  const [showInfo, setShowInfo] = useState(false);
  const [plannedPath, setPlannedPath] = useState([]);
  // Off the moment an admin drags the map to look at something else - a
  // map that keeps yanking your view back to the driver every 3s poll
  // isn't inspectable. The recenter button (bottom-right, same spot/glyph
  // Google Maps itself uses) turns it back on.
  const [autoFollow, setAutoFollow] = useState(true);
  const mapRef = useRef(null);
  const lastPannedRef = useRef(null);
  // Captured once, on the first render that has a real fix - GoogleMap's
  // own `center` prop is only ever read as the *initial* view; all
  // repositioning after that goes through the imperative panTo effect
  // below. Passing the live `position` object as `center` on every render
  // (the previous version did) fights that effect - react-google-maps
  // calls the plain, non-animated map.setCenter() on every prop change,
  // racing the smooth panTo() and reading as a shaky, "unstable" map.
  const initialCenterRef = useRef(null);
  const { isLoaded, loadError } = useJsApiLoader({
    googleMapsApiKey: GOOGLE_MAPS_API_KEY,
    libraries: GOOGLE_MAPS_LIBRARIES,
  });

  useEffect(() => {
    let cancelled = false;
    // 3s - this view is the one you're actually watching move, so it gets
    // the tightest poll in the app (the card behind it polls slower). Kept
    // running unchanged alongside the WebSocket push below (see its own
    // comment) as the resilient fallback for a first load or an
    // environment that blocks WebSocket upgrades.
    const poll = () => fetchRouteTracking(route.route_id).then((data) => { if (!cancelled) setTracking(data); }).catch(() => {});
    poll();
    const interval = setInterval(poll, 3000);
    return () => { cancelled = true; clearInterval(interval); };
  }, [route.route_id, fetchRouteTracking]);

  // The fast path - this modal is the one view someone opens specifically
  // to watch a driver move, so it's always subscribed while open (no
  // separate on/off toggle to gate it behind, unlike FleetMap's
  // liveTracking switch). Only the raw fix is kept here; merged against
  // the REST poll's own last_location below, whichever is newer wins.
  const [wsFix, setWsFix] = useState(null); // {lat, lng, recorded_at, heading, accuracy}
  // The breadcrumb line (`path` below) used to come *only* from the REST
  // poll's tracking.path - a real bug, not just a rough edge: the arrow
  // moves the instant a WebSocket push lands, but the line connecting to
  // it only caught up on the next 3s poll, so the line visibly lagged
  // behind the already-moved arrow instead of the two updating together.
  // This mirrors every new WS fix into the line immediately; pruned back
  // down once the REST poll's own path has genuinely caught up to it (see
  // the effect below), so the REST poll stays the eventual source of
  // truth and nothing gets double-drawn.
  const [wsPathPoints, setWsPathPoints] = useState([]); // [{lat, lng, recorded_at}]
  useTrackingSocket(true, (msg) => {
    if (msg?.type !== 'location' || msg.route_id !== route.route_id) return;
    setWsFix((prev) => {
      if (prev?.recorded_at && msg.recorded_at && prev.recorded_at >= msg.recorded_at) return prev;
      return { lat: msg.lat, lng: msg.lng, recorded_at: msg.recorded_at, heading: msg.heading, accuracy: msg.accuracy };
    });
    setWsPathPoints((prev) => {
      if (prev.length && prev[prev.length - 1].recorded_at >= msg.recorded_at) return prev;
      return [...prev, { lat: msg.lat, lng: msg.lng, recorded_at: msg.recorded_at }].slice(-200);
    });
  });
  // GPS heading is only meaningful while actually moving - most phones
  // report null/undefined the instant the vehicle stops, which would
  // otherwise snap the arrow back to "pointing north" every time the
  // driver pauses. Remembers the last real heading instead. Separate from
  // lastPannedRef below (which only updates while autoFollow is on) -
  // this needs to track the last real position regardless of whether the
  // admin has dragged the map away, or the noise comparison below would
  // silently break the moment autoFollow turns off.
  const lastHeadingRef = useRef(0);
  const lastHeadingSourcePositionRef = useRef(null);

  // The actual road route the driver is meant to be following - depot ->
  // every stop in delivery order -> last stop - so you can see whether
  // they're on it, not just a bare dot with no path to judge it against.
  // Fetched once per route open (stop order doesn't change while a route
  // is in progress) from the backend's own OSRM-backed endpoint, not
  // Google's client-side DirectionsService - that legacy Directions API
  // isn't enabled for this project's Maps key (only the JavaScript API
  // is), while OSRM is the same routing engine already proven reliable
  // elsewhere in this codebase (route_distance_time).
  useEffect(() => {
    let cancelled = false;
    fetchRoutePlannedPath(route.route_id)
      .then((data) => { if (!cancelled) setPlannedPath(data.path || []); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [route.route_id, fetchRoutePlannedPath]);

  // Whichever fix is actually newer wins - the REST poll and the
  // WebSocket push race by design (same reasoning as FleetMap's
  // applyLocationFix), so a slightly-delayed poll response can never
  // regress the dot backward after a fresher push already rendered.
  const restLast = tracking?.last_location;
  const last = !restLast ? wsFix : !wsFix || restLast.recorded_at >= wsFix.recorded_at ? restLast : wsFix;
  const position = last ? { lat: last.lat, lng: last.lng } : null;
  const restPath = tracking?.path || [];
  // The REST path plus any WS-pushed points newer than it - see
  // wsPathPoints' own comment for why the line needs this to keep pace
  // with the arrow instead of lagging a poll cycle behind it.
  const restPathLastRecordedAt = restPath.length ? restPath[restPath.length - 1].recorded_at : null;
  const path = [
    ...restPath,
    ...wsPathPoints.filter((p) => !restPathLastRecordedAt || p.recorded_at > restPathLastRecordedAt),
  ].map((p) => ({ lat: p.lat, lng: p.lng }));

  // Once the REST poll's own path has caught up past a WS point, drop it
  // from wsPathPoints - keeps the array from growing unbounded and keeps
  // the REST poll as the eventual single source of truth for the line.
  useEffect(() => {
    if (!restPathLastRecordedAt) return;
    setWsPathPoints((prev) => {
      const next = prev.filter((p) => p.recorded_at > restPathLastRecordedAt);
      return next.length === prev.length ? prev : next;
    });
  }, [restPathLastRecordedAt]);
  // Below the GPS noise floor, the vehicle isn't really moving, so the
  // heading shouldn't drift either (same reasoning as FleetMap's
  // applyLocationFix) - otherwise a parked driver's arrow could still
  // wobble a degree or two between noisy-but-technically-different
  // headings on each fix.
  const isHeadingNoise = position && lastHeadingSourcePositionRef.current
    && haversineMeters(lastHeadingSourcePositionRef.current, position) < GPS_NOISE_METERS;
  if (position && !isHeadingNoise) lastHeadingSourcePositionRef.current = position;
  if (!isHeadingNoise && last?.heading != null && !Number.isNaN(last.heading)) lastHeadingRef.current = last.heading;
  const heading = lastHeadingRef.current;
  // Real GPS accuracy (meters, from the phone's own location provider) -
  // rendered as a circle around the arrow below rather than pretending
  // the dot marks an exact point. A phone's GPS is commonly 5-20m off
  // (worse near/inside a building, which is exactly what a warehouse
  // stop looks like) - showing that honestly is the fix for "the arrow
  // looks slightly off from where I actually am": it isn't a rendering
  // bug, it's real sensor uncertainty, and a plain dot with no accuracy
  // indicator hides that instead of explaining it.
  const accuracy = last?.accuracy != null && !Number.isNaN(last.accuracy) ? last.accuracy : null;

  if (position && initialCenterRef.current === null) {
    initialCenterRef.current = position;
  }

  // Smoothly-glided version of `position`, for the marker's rendered
  // position only - panTo/autoFollow below still key off the raw
  // `position`, unchanged, so camera movement stays driven by real GPS
  // fixes and the existing "only pan for genuine movement" threshold,
  // not by every intermediate animation frame.
  const animatedTargets = useMemo(() => (position ? { driver: position } : {}), [position?.lat, position?.lng]);
  const displayedPosition = useAnimatedPositions(animatedTargets).driver || position;

  // panTo on every real position change is what makes this read as *live*
  // movement - but GPS noise moves the reported point a few meters even
  // while the driver is standing still, and panning the map for that
  // reads as a shaky, "unstable" map rather than a calm live one. Only
  // pans for a move big enough to actually be movement, and only while
  // the admin hasn't dragged the map away to look at something else (see
  // autoFollow above).
  useEffect(() => {
    if (!position || !mapRef.current || !autoFollow) return;
    const prev = lastPannedRef.current;
    if (prev && haversineMeters(prev, position) < 8) return;
    lastPannedRef.current = position;
    mapRef.current.panTo(position);
  }, [position?.lat, position?.lng, autoFollow]);

  const handleRecenter = () => {
    setAutoFollow(true);
    if (position && mapRef.current) {
      lastPannedRef.current = position;
      mapRef.current.panTo(position);
    }
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal modal--wide live-map-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal__header">
          <h3>Live Location — {driver.name}</h3>
          <button type="button" className="modal__close" onClick={onClose}><IconX width={16} height={16} /></button>
        </div>
        <div className="modal__meta">
          <span>{route.route_name}</span>
          <TrackingStatusPill status={tracking?.tracking_status || 'not_started'} />
          {last && <span>Updated {timeAgo(last.recorded_at)}</span>}
        </div>
        {loadError ? (
          <div className="empty-state live-map-modal__empty">Could not load Google Maps.</div>
        ) : !isLoaded ? (
          <div className="empty-state live-map-modal__empty">Loading map…</div>
        ) : position ? (
          <div className="live-map-modal__map-wrap">
            <GoogleMap
              mapContainerClassName="live-map-modal__map"
              center={initialCenterRef.current}
              zoom={16}
              onLoad={(map) => { mapRef.current = map; }}
              onDragStart={() => setAutoFollow(false)}
              options={{
                // Satellite, locked (no switcher) - same reasoning as
                // FleetMap's own map: seeing the real ground under a live
                // driver is the actual point of a live-tracking view, and
                // a custom `styles` array (the old DARK_MAP_STYLE) is
                // silently ignored on satellite/hybrid map types anyway.
                mapTypeId: 'satellite',
                disableDefaultUI: true, zoomControl: true,
                gestureHandling: 'greedy', backgroundColor: '#0b0f14',
              }}
            >
              {plannedPath.length > 1 && (
                <Polyline path={plannedPath} options={{ strokeColor: '#f5a623', strokeWeight: 4, strokeOpacity: 0.55, zIndex: 1 }} />
              )}
              {path.length > 1 && (
                <Polyline path={path} options={{ strokeColor: '#6f9bff', strokeWeight: 3, strokeOpacity: 0.9, zIndex: 2 }} />
              )}
              {(route.orders || []).filter((o) => o.lat != null && o.lng != null).map((o, idx) => (
                <Marker
                  key={o.order_id}
                  position={{ lat: o.lat, lng: o.lng }}
                  zIndex={1}
                  icon={{
                    path: window.google.maps.SymbolPath.CIRCLE,
                    scale: 6,
                    fillColor: o.is_delivered ? '#4fc98a' : '#63636b',
                    fillOpacity: 1,
                    // White, not near-black - a satellite basemap has no
                    // predictable color to read a dark outline against;
                    // white is what actually keeps this visible over
                    // rooftops, roads, and open ground alike (same
                    // reasoning as FleetMap's own pins/lines).
                    strokeColor: '#fff',
                    strokeWeight: 1.5,
                  }}
                  title={`${idx + 1}. ${o.customer_name || 'Stop'}`}
                />
              ))}
              {/* The translucent halo behind the arrow is Google Maps'
                  own "your location" glyph - reads as live/GPS at a glance
                  instead of just another plain pin. Both markers render at
                  displayedPosition (the smoothly-animated one), not the
                  raw target - see useAnimatedPositions' comment for why
                  that's what actually makes this read as continuous
                  motion rather than a stuck-then-teleport dot. The
                  foreground marker is a directional arrow (`rotation` -
                  degrees clockwise from north, exactly what the driver
                  app's GPS heading already reports), not a plain dot, so
                  this shows which way the driver is actually moving, not
                  just where they are - `heading` already falls back to
                  the last real reading while stationary (see its own
                  computation above), so the arrow doesn't snap to
                  "north" every time the driver pauses. */}
              {/* The real GPS accuracy radius, drawn to true map scale
                  (unlike the fixed-pixel halo below) - see `accuracy`'s
                  own comment for why this is the honest answer to "the
                  arrow looks slightly off from my real position": that's
                  real sensor uncertainty, not a bug, and this shows
                  exactly how much. Skipped entirely when the phone hasn't
                  reported an accuracy figure at all, rather than drawing
                  a fabricated circle. */}
              {accuracy != null && (
                <Circle
                  center={displayedPosition}
                  radius={accuracy}
                  options={{
                    fillColor: '#2457d6', fillOpacity: 0.12,
                    strokeColor: '#2457d6', strokeOpacity: 0.4, strokeWeight: 1,
                    clickable: false, zIndex: 1,
                  }}
                />
              )}
              <Marker
                position={displayedPosition}
                zIndex={2}
                clickable={false}
                icon={{
                  path: window.google.maps.SymbolPath.CIRCLE,
                  scale: 20,
                  fillColor: '#2457d6',
                  fillOpacity: 0.18,
                  strokeWeight: 0,
                }}
              />
              <Marker
                position={displayedPosition}
                zIndex={3}
                onClick={() => setShowInfo((v) => !v)}
                icon={{
                  path: window.google.maps.SymbolPath.FORWARD_CLOSED_ARROW,
                  rotation: heading,
                  scale: 5.5,
                  fillColor: '#2457d6',
                  fillOpacity: 1,
                  strokeColor: '#fff',
                  strokeWeight: 2,
                }}
              >
                {showInfo && (
                  <InfoWindow onCloseClick={() => setShowInfo(false)}>
                    <span>{driver.name} · {timeAgo(last.recorded_at)}</span>
                  </InfoWindow>
                )}
              </Marker>
            </GoogleMap>
            {!autoFollow && (
              <button type="button" className="live-map-modal__recenter" onClick={handleRecenter} title="Recenter on driver">
                <IconLocate width={18} height={18} />
              </button>
            )}
          </div>
        ) : (
          <div className="empty-state live-map-modal__empty">No location reported yet - the map appears as soon as the first ping lands.</div>
        )}
        <div className="modal__footer">
          <button type="button" className="btn btn--ghost" onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}

// Live status + assignment for the route currently open in detail view.
// Polls GET /api/routes/{id}/tracking on its own timer while this card is
// mounted (i.e. only while an admin actually has this route's detail view
// open) rather than pushing tracking state up into the whole workspace.
function DriverTrackingCard({ route, drivers, onAssignDriver, onUnassignDriver, fetchRouteTracking, fetchRoutePlannedPath }) {
  const [tracking, setTracking] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState(null);
  const [showAssignModal, setShowAssignModal] = useState(false);
  const [showLiveMap, setShowLiveMap] = useState(false);
  const [isUnassigning, setIsUnassigning] = useState(false);

  const load = async () => {
    try {
      const data = await fetchRouteTracking(route.route_id);
      setTracking(data);
      setError(null);
    } catch (err) {
      setError(err.message || 'Could not load driver tracking.');
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    setIsLoading(true);
    load();
    const interval = setInterval(load, 5000); // tightened again - was 15s, then 8s
    return () => clearInterval(interval);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [route.route_id]);

  const handleUnassign = async () => {
    if (!window.confirm(`Remove ${tracking.driver.name} from ${route.route_name}?`)) return;
    setIsUnassigning(true);
    try {
      await onUnassignDriver(route.route_id);
      await load();
    } finally {
      setIsUnassigning(false);
    }
  };

  return (
    <div className={`driver-card${tracking?.driver ? ` driver-card--${tracking.tracking_status}` : ''}`}>
      <div className="driver-card__head">
        <h3><IconUsers width={14} height={14} /> Driver & Live Tracking</h3>
        <button type="button" className="modal__close driver-card__refresh" title="Refresh" onClick={load}>
          <IconRefresh width={14} height={14} />
        </button>
      </div>

      {isLoading ? (
        <div className="driver-card__loading">Loading…</div>
      ) : error ? (
        <div className="empty-state">{error}</div>
      ) : !tracking.driver ? (
        <div className="driver-card__empty">
          <span>No driver assigned to this route yet.</span>
          <button type="button" className="btn btn--outline" onClick={() => setShowAssignModal(true)}>
            <IconPlus width={14} height={14} /> Assign Driver
          </button>
        </div>
      ) : (
        <>
          <div className="driver-card__row">
            <div className="driver-card__identity">
              <span className="driver-card__name">{tracking.driver.name}</span>
              <span className="driver-card__meta mono-num">{tracking.driver.driver_code}{tracking.driver.vehicle_number ? ` · ${tracking.driver.vehicle_number}` : ''}{tracking.driver.mobile ? ` · ${tracking.driver.mobile}` : ''}</span>
            </div>
            <TrackingStatusPill status={tracking.tracking_status} />
          </div>
          {tracking.last_location && (
            <div className="driver-card__meta">
              Last seen {new Date(tracking.last_location.recorded_at).toLocaleTimeString()}
              {/* Built from the driver's own recorded GPS trail, not the
                  planned route distance route_service estimated at
                  generation time - a real, live odometer, and the actual
                  answer to "how much km has he travelled" rather than
                  just "how much was planned". */}
              {tracking.distance_travelled_km != null && ` · ${tracking.distance_travelled_km} km travelled`}
            </div>
          )}
          {tracking.delivery_legs?.some((leg) => leg.delivered) && (
            <div className="driver-card__legs">
              <span className="driver-card__legs-label">Delivery times</span>
              {tracking.delivery_legs.filter((leg) => leg.delivered).map((leg) => {
                const order = route.orders.find((o) => String(o.order_id) === String(leg.order_id));
                return (
                  <div key={leg.order_id} className="driver-card__leg-row">
                    <span className="driver-card__leg-name">{order?.customer_name || `Order #${leg.order_id}`}</span>
                    <span className="driver-card__leg-stats mono-num">
                      {leg.time_minutes != null ? `${leg.time_minutes} min` : '—'} · {leg.distance_km != null ? `${leg.distance_km} km` : '—'}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
          {route.orders.length > 0 && (
            <div className="driver-card__progress">
              <div className="driver-card__progress-label">
                <span>{route.delivered_count || 0} / {route.orders.length} delivered</span>
              </div>
              <div className="capacity-bar">
                <span
                  className={`capacity-bar__fill${(route.delivered_count || 0) >= route.orders.length ? ' capacity-bar__fill--full' : ''}`}
                  style={{ width: `${Math.round(((route.delivered_count || 0) / route.orders.length) * 100)}%` }}
                />
              </div>
            </div>
          )}
          <div className="driver-card__actions">
            {tracking.last_location ? (
              <button type="button" className="btn btn--secondary" onClick={() => setShowLiveMap(true)}>
                <IconPin width={14} height={14} /> Track Driver
              </button>
            ) : (
              <button type="button" className="btn btn--secondary" disabled title="No location reported yet">
                <IconPin width={14} height={14} /> Track Driver
              </button>
            )}
            <button type="button" className="btn btn--outline" onClick={() => setShowAssignModal(true)}>Change Driver</button>
            <button type="button" className="btn btn--danger-ghost" disabled={isUnassigning} onClick={handleUnassign}>
              {isUnassigning ? 'Removing…' : 'Unassign'}
            </button>
          </div>
        </>
      )}

      {showAssignModal && (
        <AssignDriverModal
          route={route}
          drivers={drivers}
          onClose={() => setShowAssignModal(false)}
          onAssign={async (driverId, force) => {
            const result = await onAssignDriver(route.route_id, driverId, force);
            if (!result.conflict) await load();
            return result;
          }}
        />
      )}

      {showLiveMap && tracking?.driver && (
        <LiveMapModal
          route={route}
          driver={tracking.driver}
          initialTracking={tracking}
          fetchRouteTracking={fetchRouteTracking}
          fetchRoutePlannedPath={fetchRoutePlannedPath}
          onClose={() => setShowLiveMap(false)}
        />
      )}
    </div>
  );
}

// Driver & Tracking's own page - DriverTrackingCard used to sit embedded
// inside RouteDetail, below the Route Map card and above the stop list;
// it's lifted out here into its own slide-in drawer (same pattern
// RouteDetail itself already uses over the routes list - see
// route-drawer/route-drawer-backdrop) so it reads as a real separate
// destination reached via its own "Driver & Tracking" button, not one
// more card competing for space on the route's main page. Everything it
// shows/does is unchanged - just DriverTrackingCard given room of its
// own.
function DriverPage({ route, drivers, onAssignDriver, onUnassignDriver, fetchRouteTracking, fetchRoutePlannedPath, onBack }) {
  return (
    <div className="route-detail">
      <div className="route-detail__header">
        <button type="button" className="route-detail__back" onClick={onBack}>
          <IconChevron width={14} height={14} className="route-detail__back-icon" />
          {route.route_name}
        </button>
        <div className="route-detail__title-block">
          <div className="route-detail__title-row">
            <h2 className="route-detail__title">Driver & Tracking</h2>
          </div>
          <span className="route-detail__subtitle">
            {route.route_name} · {route.vehicle_type === 'car' ? 'Car' : 'Bike'} · {route.orders.length} stop{route.orders.length === 1 ? '' : 's'}
          </span>
        </div>
      </div>
      <DriverTrackingCard
        route={route}
        drivers={drivers}
        onAssignDriver={onAssignDriver}
        onUnassignDriver={onUnassignDriver}
        fetchRouteTracking={fetchRouteTracking}
        fetchRoutePlannedPath={fetchRoutePlannedPath}
      />
    </div>
  );
}

function RouteDetail({
  route, routeIdx, routes, capacityFor, pendingOrders,
  onBack, onToggleVehicle, isChangingVehicle, onDeleteRoute, isDeletingRoute, onDownload,
  onReassignOrder, onAssignOrders, onReorderRoute,
  fetchRouteTracking, fetchRoutePlannedPath, onOpenDriverPage,
  selectedOrderId, onSelectOrder,
  maxCapacityFor, onMoveOrders, isMovingAddresses,
}) {
  const capacity = capacityFor(route.vehicle_type);
  const maxCapacity = maxCapacityFor(route.vehicle_type);
  const count = route.orders.length;
  const isFull = count >= capacity;
  const isAtMaxCapacity = count >= maxCapacity;
  const canFlexAddresses = maxCapacity > capacity;
  const isEdited = route.status === 'manually_edited';
  const status = routeStatus(route, capacity);
  const nodeRefs = useRef({});
  const [showAddAddressModal, setShowAddAddressModal] = useState(false);

  useEffect(() => {
    if (selectedOrderId && nodeRefs.current[selectedOrderId]) {
      nodeRefs.current[selectedOrderId].scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }, [selectedOrderId]);

  const dragRef = useRef(null);
  const handleDragStart = (orderId) => { dragRef.current = String(orderId); };
  const handleDrop = (targetOrderId) => {
    const draggedId = dragRef.current;
    dragRef.current = null;
    if (!draggedId || draggedId === String(targetOrderId)) return;
    const ids = route.orders.map((o) => String(o.order_id));
    const fromIdx = ids.indexOf(draggedId);
    const toIdx = ids.indexOf(String(targetOrderId));
    if (fromIdx === -1 || toIdx === -1) return;
    const reordered = [...ids];
    const [moved] = reordered.splice(fromIdx, 1);
    reordered.splice(toIdx, 0, moved);
    onReorderRoute(route, reordered);
  };
  const moveStop = (orderId, direction) => {
    const ids = route.orders.map((o) => String(o.order_id));
    const idx = ids.indexOf(String(orderId));
    const targetIdx = direction === 'up' ? idx - 1 : idx + 1;
    if (idx === -1 || targetIdx < 0 || targetIdx >= ids.length) return;
    const reordered = [...ids];
    [reordered[idx], reordered[targetIdx]] = [reordered[targetIdx], reordered[idx]];
    onReorderRoute(route, reordered);
  };

  // Removing a stop back to Unassigned is a real, slightly-destructive
  // action - confirm it, and say plainly where the order is going, same
  // language as deleting a whole route.
  const handleMoveTo = (orderId, target) => {
    if (target === 'pending') {
      if (window.confirm('Remove this order from the route?\n\nIt will move to Unassigned Orders and can be assigned to another route later.')) {
        onReassignOrder(orderId, route.route_name, target);
      }
      return;
    }
    onReassignOrder(orderId, route.route_name, target);
  };

  return (
    <div className={`route-detail route-hue-${routeIdx % 6}`}>
      <div className="route-detail__header">
        <button type="button" className="route-detail__back" onClick={onBack}>
          <IconChevron width={14} height={14} className="route-detail__back-icon" />
          Routes
        </button>
        <div className="route-detail__title-block">
          <div className="route-detail__title-row">
            <h2 className="route-detail__title">{route.route_name}</h2>
            <StatusBadge status={status} />
            {route.is_auto_created && <span className="tag tag--auto"><IconPlus width={11} height={11} />Auto-added</span>}
            {isEdited && <span className="tag tag--edited"><IconAlert width={11} height={11} />Manually edited</span>}
          </div>
          <span className="route-detail__subtitle">
            {formatAreaPath(route.areas, 5)}
          </span>
        </div>

        <div className="route-detail__actions">
          <button
            type="button"
            className="btn btn--secondary"
            disabled={isChangingVehicle === route.route_name}
            title="Switch this route's vehicle type"
            onClick={() => onToggleVehicle(route)}
          >
            {route.vehicle_type === 'car' ? <IconCar width={14} height={14} /> : <IconBike width={14} height={14} />}
            Switch to {route.vehicle_type === 'car' ? 'Bike' : 'Car'}
            <IconRefresh width={11} height={11} />
          </button>
          <select
            className="select-compact"
            value=""
            disabled={isFull || pendingOrders.length === 0}
            title={isFull ? 'This route is full' : 'Add an unassigned order to this route'}
            onChange={(e) => { const orderId = e.target.value; if (orderId) onAssignOrders([orderId], route.route_name); e.target.value = ''; }}
          >
            <option value="">
              {isFull ? 'Route full' : pendingOrders.length === 0 ? 'No unassigned orders' : '+ Add Delivery'}
            </option>
            {pendingOrders.map((order) => (
              <option key={order.order_id} value={order.order_id}>
                #{order.order_id} — {order.customer_name || 'Unnamed'}
              </option>
            ))}
          </select>
          {canFlexAddresses && isFull && (
            <button
              type="button"
              className="btn btn--outline"
              disabled={isAtMaxCapacity}
              title={isAtMaxCapacity ? `This route has reached the maximum capacity of ${maxCapacity} addresses.` : 'Move addresses here from another route'}
              onClick={() => setShowAddAddressModal(true)}
            >
              <IconPlus width={14} height={14} />
              {isAtMaxCapacity ? 'Maximum capacity reached' : 'Add Address from Another Route'}
            </button>
          )}
          <button type="button" className="btn btn--secondary" onClick={() => onDownload(route)}>
            <IconDownload width={14} height={14} />
            Download sheet
          </button>
          <button type="button" className="btn btn--secondary" onClick={() => onOpenDriverPage(route)}>
            <IconUsers width={14} height={14} />
            Driver & Tracking
          </button>
          <button
            type="button"
            className="btn btn--danger-ghost"
            disabled={isDeletingRoute === route.route_name}
            onClick={() => {
              if (window.confirm(`Delete ${route.route_name}? Its ${count} deliver${count === 1 ? 'y' : 'ies'} will move to Unassigned Orders - nothing is deleted.`)) {
                onDeleteRoute(route);
              }
            }}
          >
            {isDeletingRoute === route.route_name ? <span className="spinner" /> : <IconX width={14} height={14} />}
            {isDeletingRoute === route.route_name ? 'Deleting…' : 'Delete Route'}
          </button>
        </div>
      </div>

      <RouteOverviewStrip route={route} capacity={capacity} maxCapacity={maxCapacity} />

      <div className="route-stop-list">
        {count === 0 ? (
          <div className="empty-state">
            <IconInbox width={22} height={22} />
            This route currently has no delivery stops.
          </div>
        ) : (
          <>
            <div className="route-stop-list__terminal">
              <span className="route-stop-list__terminal-dot"><IconInbox width={12} height={12} /></span>
              Warehouse
            </div>

            {route.orders.map((order, stopIdx) => (
              <div
                key={order.order_id}
                className={`route-stop${String(order.order_id) === String(selectedOrderId) ? ' route-stop--selected' : ''}${order.is_delivered ? ' route-stop--delivered' : ''}`}
                ref={(el) => { nodeRefs.current[order.order_id] = el; }}
                draggable
                title="Drag to reorder this delivery within the route"
                onClick={() => onSelectOrder?.(order.order_id)}
                onDragStart={() => handleDragStart(order.order_id)}
                onDragOver={(e) => e.preventDefault()}
                onDrop={() => handleDrop(order.order_id)}
              >
                <div className="route-stop__rail">
                  <span className={`route-stop__seq${order.is_late ? ' route-stop__seq--late' : ''}`}>{String(stopIdx + 1).padStart(2, '0')}</span>
                  {stopIdx < route.orders.length - 1 && <span className="route-stop__connector" />}
                </div>
                <div className="route-stop__body">
                  <div className="route-stop__top">
                    <span className="route-stop__order-id">Order #{order.order_id}</span>
                    <span className="route-stop__customer">{order.customer_name}</span>
                    <span className={`stop-status${order.is_late ? ' stop-status--late' : ''}`}>
                      {order.is_late ? '🔴 Late' : '🟢 On time'}
                    </span>
                    {order.is_delivered && <span className="stop-status stop-status--delivered">✅ Delivered</span>}
                  </div>
                  {order.area && <span className="route-stop__area">{order.area}</span>}
                  <p className="route-stop__address">{order.address || 'No address on file'}</p>
                  <div className="route-stop__meta">
                    <span>Slot {order.delivery_time}</span>
                    {order.eta && <span>ETA {order.eta}</span>}
                    {order.map_link && (
                      <a className="pin-badge" href={order.map_link} target="_blank" rel="noopener noreferrer" onClick={(e) => e.stopPropagation()}>
                        <IconPin width={12} height={12} />
                        View pin
                      </a>
                    )}
                  </div>
                  <div className="route-stop__actions" onClick={(e) => e.stopPropagation()}>
                    <span className="stop-reorder">
                      <button
                        type="button"
                        className="stop-reorder__btn"
                        disabled={stopIdx === 0}
                        title="Move earlier in the delivery sequence"
                        onClick={() => moveStop(order.order_id, 'up')}
                      >
                        <IconArrowUp width={12} height={12} />
                      </button>
                      <button
                        type="button"
                        className="stop-reorder__btn"
                        disabled={stopIdx === route.orders.length - 1}
                        title="Move later in the delivery sequence"
                        onClick={() => moveStop(order.order_id, 'down')}
                      >
                        <IconArrowDown width={12} height={12} />
                      </button>
                    </span>
                    <select
                      className="stop-move"
                      value=""
                      title="Move this stop to another route"
                      onChange={(e) => { const target = e.target.value; if (target) handleMoveTo(order.order_id, target); e.target.value = ''; }}
                    >
                      <option value="">Move to…</option>
                      {routes.filter((r) => r.route_name !== route.route_name).map((r) => {
                        const full = r.orders.length >= capacityFor(r.vehicle_type);
                        return (
                          <option key={r.route_name} value={r.route_name} disabled={full}>
                            {r.route_name} ({r.orders.length}/{capacityFor(r.vehicle_type)}){full ? ' — full' : ''}
                          </option>
                        );
                      })}
                      <option value="pending">Remove → Unassigned</option>
                    </select>
                  </div>
                </div>
              </div>
            ))}

            <div className="route-stop-list__terminal route-stop-list__terminal--end">
              <span className="route-stop-list__terminal-dot route-stop-list__terminal-dot--end"><IconFlag width={12} height={12} /></span>
              Finish{route.estimated_finish_time ? ` · ${route.estimated_finish_time}` : ''}
            </div>
          </>
        )}
      </div>

      {/* The in-app map every route now has, not just routes with an
          active driver - seeing this route's stops no longer requires
          waiting on live tracking to start. Below the stop list (not
          above it) so an admin reads the actual delivery order first,
          then sees it laid out geographically. Reuses FleetMap scoped to
          just this one route, so the same big pins/route line/live layer
          as the fleet-wide Map view show up here too - alwaysTrackable
          because this map is already scoped to one specific route, so it
          shouldn't second-guess that against a route_run_status/driver_id
          snapshot that can go stale the moment a driver starts their
          route from their own app (see FleetMap's alwaysTrackable
          comment). */}
      <div className="driver-card route-map-card">
        <div className="driver-card__head">
          <h3><IconPin width={14} height={14} /> Route Map</h3>
        </div>
        <FleetMap
          routes={[route]}
          fetchRoutePlannedPath={fetchRoutePlannedPath}
          fetchRouteTracking={fetchRouteTracking}
          selectedRouteName={route.route_name}
          onSelectRoute={() => {}}
          baseColorIndex={routeIdx}
          alwaysTrackable
        />
      </div>

      {showAddAddressModal && (
        <AddAddressModal
          destinationRoute={route}
          routes={routes}
          capacityFor={capacityFor}
          maxCapacityFor={maxCapacityFor}
          isSubmitting={isMovingAddresses}
          onClose={() => setShowAddAddressModal(false)}
          onConfirm={(sourceRouteId, orderIds) => onMoveOrders(sourceRouteId, route.route_id, orderIds)}
        />
      )}
    </div>
  );
}

// --------------------------------------------------------------------------
// Unassigned Orders panel (unchanged workflow, out of scope for this pass).
// --------------------------------------------------------------------------

function UnassignedPanel({ orders, routes, pendingOrders, capacityFor, isRouteFull, onAssignOrders, onCreateRoute, isCreatingRoute }) {
  const [search, setSearch] = useState('');
  const [selected, setSelected] = useState([]);
  const [bulkTarget, setBulkTarget] = useState('');

  const filtered = useMemo(() => orders.filter((o) => matchesQuery(o, search)), [orders, search]);

  useEffect(() => { setSelected((prev) => prev.filter((id) => filtered.some((o) => String(o.order_id) === id))); }, [filtered]);

  const toggleSelected = (orderId) => {
    const id = String(orderId);
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  };

  return (
    <div className="unassigned-panel">
      <div className="unassigned-panel__toolbar">
        <div className="search-field">
          <IconSearch width={14} height={14} />
          <input
            type="text"
            placeholder="Search customer, order ID, area, address…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <select
          className="stop-move add-route-select"
          value=""
          disabled={isCreatingRoute}
          onChange={(e) => { const t = e.target.value; if (t) onCreateRoute(t); e.target.value = ''; }}
        >
          <option value="">{isCreatingRoute ? 'Creating…' : '+ Add Route'}</option>
          <option value="bike">Bike</option>
          <option value="car">Car</option>
        </select>
      </div>

      {selected.length > 0 && (
        <div className="bulk-assign-bar">
          <span>{selected.length} selected</span>
          <select className="select-compact" value={bulkTarget} onChange={(e) => setBulkTarget(e.target.value)}>
            <option value="">Assign selected to…</option>
            {routes.map((r) => {
              const full = isRouteFull(r);
              const willFit = r.orders.length + selected.length <= capacityFor(r.vehicle_type);
              return (
                <option key={r.route_name} value={r.route_name} disabled={full || !willFit}>
                  {r.route_name} ({r.orders.length}/{capacityFor(r.vehicle_type)}){!willFit ? ` — only ${capacityFor(r.vehicle_type) - r.orders.length} space(s)` : ''}
                </option>
              );
            })}
          </select>
          <button
            type="button"
            className="btn btn--primary"
            disabled={!bulkTarget}
            onClick={() => { onAssignOrders(selected, bulkTarget); setSelected([]); setBulkTarget(''); }}
          >
            Assign Selected
          </button>
          <button type="button" className="btn btn--ghost" onClick={() => setSelected([])}>Clear</button>
        </div>
      )}

      <div className="unassigned-panel__list">
        {pendingOrders.length === 0 ? (
          <div className="empty-state empty-state--ok">
            <IconCheck width={22} height={22} />
            All orders are currently assigned.
          </div>
        ) : filtered.length === 0 ? (
          <div className="empty-state"><IconSearch width={22} height={22} />No orders match "{search}"</div>
        ) : (
          filtered.map((order) => (
            <div key={order.order_id} className="unassigned-row">
              <input
                type="checkbox"
                checked={selected.includes(String(order.order_id))}
                onChange={() => toggleSelected(order.order_id)}
              />
              <DeliveryIdentityBlock order={order} />
              <div className="unassigned-row__meta">
                <span className={`tag ${order.status === 'unassigned' ? 'tag--edited' : 'tag--auto'}`}>
                  {order.status === 'unassigned' ? 'Removed from route' : 'Never routed'}
                </span>
                {order.previous_route_name && (
                  <span className="unassigned-row__prev">Previously: {order.previous_route_name} ({order.previous_vehicle_type})</span>
                )}
              </div>
              <select
                className="stop-move"
                value=""
                title="Assign this order to a route"
                onChange={(e) => { const t = e.target.value; if (t) onAssignOrders([order.order_id], t); }}
              >
                <option value="">Assign to…</option>
                {routes.map((r) => {
                  const full = isRouteFull(r);
                  return (
                    <option key={r.route_name} value={r.route_name} disabled={full}>
                      {r.route_name} ({r.orders.length}/{capacityFor(r.vehicle_type)}){full ? ' — FULL' : ''}
                    </option>
                  );
                })}
              </select>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// Top-level workspace: Routes (list ↔ detail) / Unassigned Orders tabs.
// --------------------------------------------------------------------------

export default function RouteWorkspace({
  routes, pendingOrders, isProcessing, capacityFor, maxCapacityFor, isRouteFull,
  isCreatingRoute, isChangingVehicle, isDeletingRoute, isMovingAddresses,
  onCreateRoute, onToggleVehicle, onDeleteRoute, onReassignOrder, onReorderRoute,
  onAssignOrders, onDownloadRoute, onMoveOrders,
  requestedTab, requestedView, requestedLiveTracking,
  drivers, onAssignDriver, onUnassignDriver, fetchRouteTracking, fetchRoutePlannedPath,
  onViewChange,
}) {
  const [tab, setTab] = useState('routes');
  // requestedTab is a one-way "command" from the sidebar nav (clicking
  // "Unassigned Orders" should actually switch this tab, not just scroll
  // near it) - a change in this prop jumps the tab; the tab buttons below
  // still update `tab` directly for an immediate click response.
  useEffect(() => {
    if (requestedTab === 'routes' || requestedTab === 'unassigned') setTab(requestedTab);
  }, [requestedTab]);
  const [view, setView] = useState('list'); // 'list' | 'detail'
  // A route's detail now opens as a slide-in drawer over this page (see
  // the render below) rather than replacing it outright, so App.jsx's
  // chrome above it (KPI row, Generate panel, session tabs) no longer
  // needs to hide when one is open - the drawer's own backdrop already
  // dims everything behind it. onViewChange is kept (App.jsx still wires
  // it to routeDetailOpen) so nothing upstream needs to change; it just
  // never has reason to report true anymore.
  useEffect(() => { onViewChange?.(false); }, [onViewChange]);
  const [selectedRouteName, setSelectedRouteName] = useState(null);
  const [selectedOrderId, setSelectedOrderId] = useState(null);
  const [routeSearch, setRouteSearch] = useState('');
  const [vehicleFilter, setVehicleFilter] = useState('all');
  const [statusFilter, setStatusFilter] = useState('all');
  const [locationFilter, setLocationFilter] = useState('all');

  // Bulk selection on the Routes table - same shape as UnassignedPanel's
  // `selected` above (array of ids, here route_name), just for
  // Download/Delete instead of Assign.
  const [selectedRouteNames, setSelectedRouteNames] = useState([]);
  const [isBulkActing, setIsBulkActing] = useState(false);

  // List | Map | Split, for the Routes tab only - independent of
  // `selectedRouteName`/`view` above (which is specifically "a route's
  // full detail page is open"). Highlighting a route on the map/in the
  // split list doesn't open its detail page, so this gets its own state.
  const [viewMode, setViewMode] = useState('list');
  const [mapSelectedRouteName, setMapSelectedRouteName] = useState(null);
  // Same one-way command shape as requestedTab above - "Live Tracking" in
  // the sidebar jumps the Routes tab into Map view. requestedLiveTracking
  // (passed straight through to FleetMap below) additionally flips its
  // live layer on.
  useEffect(() => {
    if (requestedView === 'map') setViewMode('map');
  }, [requestedView]);

  const allLocations = useMemo(() => {
    const seen = new Set();
    const ordered = [];
    routes.forEach((r) => (r.areas || []).forEach((a) => {
      if (!seen.has(a.toLowerCase())) { seen.add(a.toLowerCase()); ordered.push(a); }
    }));
    return ordered.sort((a, b) => a.localeCompare(b));
  }, [routes]);

  useEffect(() => {
    if (routes.length === 0) { setSelectedRouteName(null); setView('list'); return; }
    if (selectedRouteName && !routes.some((r) => r.route_name === selectedRouteName)) {
      setSelectedRouteName(null);
      setView('list');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routes]);

  const selectedRoute = routes.find((r) => r.route_name === selectedRouteName) || null;
  const selectedRouteIdx = selectedRoute ? routes.findIndex((r) => r.route_name === selectedRoute.route_name) : 0;

  // Driver & Tracking as its own page (DriverPage above), reached from
  // RouteDetail's "Driver & Tracking" button - stacks its own drawer over
  // the route detail drawer, same shape as selectedRouteName/view above.
  const [driverPageRouteName, setDriverPageRouteName] = useState(null);
  useEffect(() => {
    if (driverPageRouteName && !routes.some((r) => r.route_name === driverPageRouteName)) {
      setDriverPageRouteName(null);
    }
  }, [routes, driverPageRouteName]);
  const driverPageRoute = routes.find((r) => r.route_name === driverPageRouteName) || null;

  const hasActiveFilters = Boolean(routeSearch.trim()) || vehicleFilter !== 'all' || statusFilter !== 'all' || locationFilter !== 'all';
  const clearFilters = () => { setRouteSearch(''); setVehicleFilter('all'); setStatusFilter('all'); setLocationFilter('all'); };

  const filteredRoutes = useMemo(() => {
    let result = routes;
    if (vehicleFilter !== 'all') result = result.filter((r) => r.vehicle_type === vehicleFilter);
    if (statusFilter !== 'all') result = result.filter((r) => routeStatus(r, capacityFor(r.vehicle_type)) === statusFilter);
    if (locationFilter !== 'all') result = result.filter((r) => (r.areas || []).some((a) => a.toLowerCase() === locationFilter.toLowerCase()));
    if (routeSearch.trim()) {
      const q = routeSearch.toLowerCase();
      result = result.filter((r) => (
        r.route_name.toLowerCase().includes(q)
        || (r.areas || []).some((a) => a.toLowerCase().includes(q))
        || r.orders.some((o) => matchesQuery(o, q))
      ));
    }
    return result;
  }, [routes, routeSearch, vehicleFilter, statusFilter, locationFilter, capacityFor]);

  // Same pattern as UnassignedPanel's selected-filter effect above: drop
  // anything selected that's no longer in view (deleted, or filtered out).
  useEffect(() => {
    setSelectedRouteNames((prev) => prev.filter((name) => filteredRoutes.some((r) => r.route_name === name)));
  }, [filteredRoutes]);

  const toggleRouteSelected = (routeName) => {
    setSelectedRouteNames((prev) => (prev.includes(routeName) ? prev.filter((n) => n !== routeName) : [...prev, routeName]));
  };
  const toggleSelectAllRoutes = (visibleNames) => {
    setSelectedRouteNames((prev) => (
      visibleNames.every((n) => prev.includes(n))
        ? prev.filter((n) => !visibleNames.includes(n))
        : [...new Set([...prev, ...visibleNames])]
    ));
  };
  const selectedRoutesList = routes.filter((r) => selectedRouteNames.includes(r.route_name));

  // Downloads are synchronous client-side workbook builds (no shared
  // component state to race), so these can fire back-to-back. Deletes
  // hit the API and update shared `routes`/`isDeletingRoute` state one
  // route at a time - same as clicking each row's Delete individually,
  // just queued instead of concurrent, and behind one confirm instead of
  // one per route.
  const bulkDownloadSelected = () => {
    selectedRoutesList.forEach((route) => onDownloadRoute(route));
  };
  const bulkDeleteSelected = async () => {
    const names = selectedRoutesList.map((r) => r.route_name).join(', ');
    if (!window.confirm(`Delete ${selectedRoutesList.length} route${selectedRoutesList.length === 1 ? '' : 's'} (${names})? Their deliveries will move to Unassigned Orders - nothing is deleted.`)) return;
    setIsBulkActing(true);
    try {
      for (const route of selectedRoutesList) {
        // eslint-disable-next-line no-await-in-loop
        await onDeleteRoute(route);
      }
      setSelectedRouteNames([]);
    } finally {
      setIsBulkActing(false);
    }
  };

  const openRoute = (routeName) => { setSelectedRouteName(routeName); setSelectedOrderId(null); setView('detail'); };
  const backToList = () => setView('list');

  return (
    <div className="board board--workspace" id="route-workspace">
      <div className="workspace-tabs">
        <button type="button" className={`workspace-tab${tab === 'routes' ? ' workspace-tab--active' : ''}`} onClick={() => setTab('routes')}>
          <IconRoute width={14} height={14} />
          Routes
          <span className="workspace-tab__count mono-num">{routes.length}</span>
        </button>
        <button
          type="button"
          id="unassigned-board"
          className={`workspace-tab${tab === 'unassigned' ? ' workspace-tab--active' : ''}`}
          onClick={() => setTab('unassigned')}
        >
          <IconInbox width={14} height={14} />
          Unassigned Orders
          <span className="workspace-tab__count workspace-tab__count--warn mono-num">{pendingOrders.length}</span>
        </button>
      </div>

      {tab === 'unassigned' ? (
        <UnassignedPanel
          orders={pendingOrders}
          pendingOrders={pendingOrders}
          routes={routes}
          capacityFor={capacityFor}
          isRouteFull={isRouteFull}
          onAssignOrders={onAssignOrders}
          onCreateRoute={onCreateRoute}
          isCreatingRoute={isCreatingRoute}
        />
      ) : routes.length === 0 ? (
        <div className="empty-state">
          <IconInbox width={22} height={22} />
          {isProcessing ? 'Building routes…' : 'Create your first delivery route to start organizing today\'s orders.'}
          <select
            className="stop-move add-route-select"
            value=""
            disabled={isCreatingRoute}
            onChange={(e) => { const t = e.target.value; if (t) onCreateRoute(t); e.target.value = ''; }}
          >
            <option value="">{isCreatingRoute ? 'Creating…' : 'Create Route'}</option>
            <option value="bike">Bike</option>
            <option value="car">Car</option>
          </select>
        </div>
      ) : (
        <>
        <div className="routes-page">
          <div className="routes-page__header">
            <div>
              <h2 className="routes-page__title">Routes</h2>
              <p className="routes-page__subtitle">Manage delivery routes, vehicles and stops</p>
            </div>
            <div className="routes-page__header-actions">
              <div className="view-toggle" role="group" aria-label="Routes view">
                <button type="button" className={`view-toggle__btn${viewMode === 'list' ? ' view-toggle__btn--active' : ''}`} onClick={() => setViewMode('list')}>List</button>
                <button type="button" className={`view-toggle__btn${viewMode === 'map' ? ' view-toggle__btn--active' : ''}`} onClick={() => setViewMode('map')}>Map</button>
                <button type="button" className={`view-toggle__btn${viewMode === 'split' ? ' view-toggle__btn--active' : ''}`} onClick={() => setViewMode('split')}>Split</button>
              </div>
              <select
                className="stop-move add-route-select"
                value=""
                disabled={isCreatingRoute}
                onChange={(e) => { const t = e.target.value; if (t) onCreateRoute(t); e.target.value = ''; }}
              >
                <option value="">{isCreatingRoute ? 'Creating…' : 'Create Route'}</option>
                <option value="bike">Bike</option>
                <option value="car">Car</option>
              </select>
            </div>
          </div>

          {viewMode === 'list' && (
            <RoutesFilterBar
              search={routeSearch}
              onSearchChange={setRouteSearch}
              vehicleFilter={vehicleFilter}
              onVehicleFilterChange={setVehicleFilter}
              statusFilter={statusFilter}
              onStatusFilterChange={setStatusFilter}
              locationFilter={locationFilter}
              onLocationFilterChange={setLocationFilter}
              locations={allLocations}
              onClear={clearFilters}
              hasActiveFilters={hasActiveFilters}
            />
          )}

          {viewMode === 'list' && selectedRouteNames.length > 0 && (
            <div className="bulk-assign-bar">
              <span>{selectedRouteNames.length} selected</span>
              <button type="button" className="btn btn--secondary" onClick={bulkDownloadSelected}>
                <IconDownload width={13} height={13} />
                Download selected
              </button>
              <button type="button" className="btn btn--danger" disabled={isBulkActing} onClick={bulkDeleteSelected}>
                {isBulkActing ? <span className="spinner" /> : <IconX width={13} height={13} />}
                {isBulkActing ? 'Deleting…' : 'Delete selected'}
              </button>
              <button type="button" className="btn btn--ghost" onClick={() => setSelectedRouteNames([])}>Clear</button>
            </div>
          )}

          {viewMode === 'map' && (
            <FleetMap
              routes={filteredRoutes}
              fetchRoutePlannedPath={fetchRoutePlannedPath}
              fetchRouteTracking={fetchRouteTracking}
              requestedLiveTracking={requestedLiveTracking}
              selectedRouteName={mapSelectedRouteName}
              onSelectRoute={(name) => setMapSelectedRouteName((prev) => (prev === name ? null : name))}
            />
          )}

          {viewMode === 'split' && (
            <div className="routes-split">
              <SplitRouteList
                routes={filteredRoutes}
                capacityFor={capacityFor}
                selectedRouteName={mapSelectedRouteName}
                onSelectRoute={setMapSelectedRouteName}
              />
              <FleetMap
                routes={filteredRoutes}
                fetchRoutePlannedPath={fetchRoutePlannedPath}
                fetchRouteTracking={fetchRouteTracking}
                requestedLiveTracking={requestedLiveTracking}
                selectedRouteName={mapSelectedRouteName}
                onSelectRoute={(name) => setMapSelectedRouteName((prev) => (prev === name ? null : name))}
              />
            </div>
          )}

          {viewMode === 'list' && (
          <div className="routes-table-wrap">
            <RoutesTable
              routes={filteredRoutes}
              capacityFor={capacityFor}
              onOpen={openRoute}
              onDownload={onDownloadRoute}
              onDeleteRoute={onDeleteRoute}
              isDeletingRoute={isDeletingRoute}
              hasActiveFilters={hasActiveFilters}
              onClearFilters={clearFilters}
              selectedRouteNames={selectedRouteNames}
              onToggleSelect={toggleRouteSelected}
              onToggleSelectAll={toggleSelectAllRoutes}
            />
            {isBulkActing && (
              <div className="busy-overlay">
                <span className="spinner" />
              </div>
            )}
          </div>
          )}
        </div>

        {/* Route detail as a slide-in drawer over the routes list, not a
            full-page swap - internal layout: overview strip, stop list,
            route map (driver & tracking is its own separate page now,
            reached via a button here - see DriverPage above). Renders
            inside a wide panel instead of replacing this page outright,
            so the KPI row/Generate panel above (App.jsx) and the routes
            list behind it stay in place, dimmed by the backdrop. Wide
            rather than a narrow ~420px drawer: RouteOverviewStrip's
            multi-column stats and the embedded map assume real width and
            would break cramped into one. */}
        {view === 'detail' && selectedRoute && (
          <>
            <div className="route-drawer-backdrop" onClick={backToList} />
            <div className="route-drawer">
              <RouteDetail
                route={selectedRoute}
                routeIdx={selectedRouteIdx}
                routes={routes}
                capacityFor={capacityFor}
                pendingOrders={pendingOrders}
                onBack={backToList}
                onToggleVehicle={onToggleVehicle}
                isChangingVehicle={isChangingVehicle}
                onDeleteRoute={onDeleteRoute}
                isDeletingRoute={isDeletingRoute}
                onDownload={onDownloadRoute}
                onReassignOrder={onReassignOrder}
                onAssignOrders={onAssignOrders}
                onReorderRoute={onReorderRoute}
                maxCapacityFor={maxCapacityFor}
                onMoveOrders={onMoveOrders}
                isMovingAddresses={isMovingAddresses}
                selectedOrderId={selectedOrderId}
                onSelectOrder={setSelectedOrderId}
                fetchRouteTracking={fetchRouteTracking}
                fetchRoutePlannedPath={fetchRoutePlannedPath}
                onOpenDriverPage={(r) => setDriverPageRouteName(r.route_name)}
              />
            </div>
          </>
        )}

        {/* Driver & Tracking's own page - stacks over the route detail
            drawer above (rendered after it, so it paints on top at the
            same z-index), reached via that drawer's "Driver & Tracking"
            button. Its own backdrop closes just this page, back to
            whichever route detail was open underneath. */}
        {driverPageRoute && (
          <>
            <div className="route-drawer-backdrop" onClick={() => setDriverPageRouteName(null)} />
            <div className="route-drawer">
              <DriverPage
                route={driverPageRoute}
                drivers={drivers}
                onAssignDriver={onAssignDriver}
                onUnassignDriver={onUnassignDriver}
                fetchRouteTracking={fetchRouteTracking}
                fetchRoutePlannedPath={fetchRoutePlannedPath}
                onBack={() => setDriverPageRouteName(null)}
              />
            </div>
          </>
        )}
        </>
      )}
    </div>
  );
}
