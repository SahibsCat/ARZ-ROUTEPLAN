import { Fragment, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { GoogleMap, Marker, useJsApiLoader } from '@react-google-maps/api';
import {
  IconRoute, IconCar, IconBike, IconClock, IconCheck, IconAlert, IconPin, IconPlus, IconInbox,
  IconDownload, IconRefresh, IconArrowUp, IconArrowDown, IconGauge, IconFlag, IconSearch, IconX,
  IconUsers, IconChevron, IconLocate,
} from '../icons';
// Lucide - the same operational-pages icon language introduced in App.jsx
// (see its own import comment for why); this file is where the Routes and
// Unassigned Orders workspace itself actually lives.
import {
  Route as LIconRoute, PackageSearch, MapPinPlus, RotateCw, Trash2, UserPlus,
  Map as LIconMap, Columns2, List as LIconList, Download as LIconDownload,
  MoreHorizontal, CarFront as LIconCarFront, Bike as LIconBike,
  Phone, Maximize2, Minimize2, Moon, Satellite, ExternalLink,
} from 'lucide-react';
import './routeWorkspace.css';

// A JS-API key is meant to be used client-side (it's restricted by
// HTTP referrer/app in Google Cloud Console, not a secret like a backend
// auth token) - same treatment as the Sentry DSN elsewhere in this
// codebase. VITE_GOOGLE_MAPS_API_KEY can still override it without a
// code change.
//
// This key's "API restrictions" needs BOTH Maps JavaScript API and
// Directions API checked (not split across separate keys, which doesn't
// work - the JS loader binds one key to the whole google.maps namespace,
// DirectionsService included, so both APIs have to live on the one key
// that loads the map). The previous hardcoded key here expired (same as
// the backend's Geocoding key, around the same time - likely a project-
// wide policy); this one was confirmed directly via the Geocoding,
// Places, and Directions REST APIs (all three returned real results, no
// REQUEST_DENIED/expired errors) - not yet confirmed with a real
// browser load of the JS map itself, so if the map still doesn't render,
// check for a gm_authFailure/ApiNotActivatedMapError in the browser
// console next (Maps JavaScript API specifically may need enabling
// separately even though the REST APIs above all work on this key).
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
// Two consecutive GPS fixes further apart than this are a jump (stale
// warehouse fix, GPS switched off and on), not a road the driver drove.
const TRAIL_JUMP_METERS = 400;

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
  const animsRef = useRef({}); // key -> { from, to, start, duration }
  const lastRetargetAtRef = useRef({}); // key -> performance.now() of the previous retarget
  const rafIdRef = useRef(null);
  // A fixed glide length was the actual cause of tracking still reading as
  // "stuck, then teleports" even though this whole hook exists to prevent
  // exactly that: pings don't arrive on a steady beat (distanceInterval
  // makes a moving driver's pings land every 1-3s, a stationary driver's
  // every 15s - see locationTask.js - and any real network hiccup widens
  // a single gap further). Gliding every retarget over the same ~4s
  // regardless of how far apart in time the two fixes actually were means
  // a short gap glides at the right speed but a long one gets covered in
  // the same 4s - i.e. it visibly speeds up/teleports right after a
  // stall. Sizing each glide's duration to the real gap since this key's
  // last retarget (clamped to a sane range) keeps the marker's apparent
  // speed roughly consistent with how the driver is actually moving,
  // instead of a one-size-fits-all duration.
  const MIN_GLIDE_MS = 600;
  const MAX_GLIDE_MS = 9000;
  // Each glide is stretched a little past the measured gap so the marker
  // is still moving when the next fix lands - it then retargets from
  // wherever it currently is, which reads as one continuous drive instead
  // of glide-stop-glide.
  const GLIDE_OVERLAP = 1.2;
  const DEFAULT_GLIDE_MS = 4200; // used only for a key's very first real retarget, with no prior gap to measure

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
      const now = performance.now();
      const lastRetargetAt = lastRetargetAtRef.current[key];
      const gap = lastRetargetAt != null ? now - lastRetargetAt : DEFAULT_GLIDE_MS;
      const duration = Math.min(MAX_GLIDE_MS, Math.max(MIN_GLIDE_MS, gap * GLIDE_OVERLAP));
      lastRetargetAtRef.current[key] = now;
      animsRef.current[key] = { from, to: { lat: pos.lat, lng: pos.lng }, start: now, duration };
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
      if (!(key in targets)) {
        delete animsRef.current[key];
        delete displayedRef.current[key];
        delete lastRetargetAtRef.current[key];
      }
    });

    if (rafIdRef.current != null) return; // a loop is already running and will pick up the retarget above
    const tick = () => {
      const now = performance.now();
      let stillAnimating = false;
      Object.entries(animsRef.current).forEach(([key, anim]) => {
        const noOp = anim.from.lat === anim.to.lat && anim.from.lng === anim.to.lng;
        const t = noOp ? 1 : Math.min(1, (now - anim.start) / anim.duration);
        // A no-op animation (a brand-new key's first fix, animated "from"
        // itself) is already at its target on frame one - counting it as
        // still-animating just because elapsed time hasn't reached this
        // glide's duration yet would keep this rAF loop spinning for
        // several seconds doing nothing on every fresh key, for no
        // visible benefit.
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

// The live driver marker itself - a bike or car emoji (matching the
// route's own vehicle_type, exactly what the admin already picks when
// creating/switching a route - see VehicleBadge elsewhere in this file)
// inside the route's own colored, white-ringed badge, same visual
// language as every other marker here. Replaces the old plain rotated
// arrow: a generic triangle doesn't say *what* the driver is actually
// out on, which matters on a fleet map mixing bikes and cars - the
// emoji answers that at a glance, the same way Swiggy/Zomato-style live
// maps use a vehicle glyph rather than an arrow. Not rotated by heading
// (an emoji glyph rotated sideways/upside-down reads as broken, not
// "turning") - which way the driver is moving is still visible from the
// breadcrumb trail/line itself.
// The warehouse marker - every route starts and ends here.
const WAREHOUSE_FALLBACK = { name: 'Warehouse', lat: 12.989953044885272, lng: 80.21804157624011 };
function warehouseIconUrl(size = 40) {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 40 40">`
    + `<rect x="3" y="3" width="34" height="34" rx="10" fill="#0f172a" stroke="#fbbf24" stroke-width="3"/>`
    + `<path d="M11 28V17l9-5 9 5v11z" fill="#fbbf24"/>`
    + `<rect x="16" y="21" width="8" height="7" rx="1" fill="#0f172a"/>`
    + `</svg>`;
  return { url: `data:image/svg+xml;charset=UTF-8,${encodeURIComponent(svg)}`, size };
}

function vehicleIconUrl(vehicleType, color, { size = 28, opacity = 1, heading = null } = {}) {
  // A navigation-style vehicle marker: a white disc with the route's
  // colour ring, a drawn scooter (with delivery box) or car glyph, and a
  // small arrow on the rim pointing the way the driver is heading (from
  // the phone's GPS heading; hidden when no heading is known).
  const c = size / 2;
  const r = size * 0.36;
  const g = (r * 1.4) / 24; // glyph scale: 24-unit icon inside the disc
  const gx = c - 12 * g;
  const gy = c - 12 * g;
  const glyph = vehicleType === 'car'
    ? `<path d="M3.5 15.5v-3.2l2.3-4.6c.3-.6.9-.9 1.5-.9h9.4c.6 0 1.2.3 1.5.9l2.3 4.6v3.2c0 .6-.4 1-1 1h-15c-.6 0-1-.4-1-1z" fill="${color}"/>`
      + `<path d="M7.6 8.6h8.8l1.6 3.4H6z" fill="#fff" fill-opacity="0.85"/>`
      + `<circle cx="7.5" cy="16.8" r="2.1" fill="#111827"/><circle cx="16.5" cy="16.8" r="2.1" fill="#111827"/>`
    : `<rect x="2" y="6.5" width="7.5" height="6.5" rx="1.3" fill="${color}"/>`
      + `<path d="M5.5 14.2h8.2l3-6.2h2.6" fill="none" stroke="${color}" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>`
      + `<path d="M17.4 8l1-2.6h2.4" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`
      + `<circle cx="6" cy="17.2" r="2.6" fill="none" stroke="#111827" stroke-width="2.1"/>`
      + `<circle cx="18" cy="17.2" r="2.6" fill="none" stroke="#111827" stroke-width="2.1"/>`;
  const arrow = heading != null && !Number.isNaN(heading)
    ? `<g transform="rotate(${heading} ${c} ${c})"><path d="M${c} ${c - r - size * 0.13} L${c + size * 0.12} ${c - r + size * 0.01} L${c - size * 0.12} ${c - r + size * 0.01} Z" fill="${color}" stroke="#fff" stroke-width="1.5" stroke-linejoin="round"/></g>`
    : '';
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" opacity="${opacity}">`
    + `<circle cx="${c}" cy="${c + size * 0.03}" r="${r + 2}" fill="#000" fill-opacity="0.3"/>`
    + arrow
    + `<circle cx="${c}" cy="${c}" r="${r}" fill="#fff" stroke="${color}" stroke-width="${Math.max(2.5, size * 0.07)}"/>`
    + `<g transform="translate(${gx} ${gy}) scale(${g})">${glyph}</g>`
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
  // A full route is the goal, not a problem - only going past base
  // capacity (possible via manual override) is flagged.
  const state = !capacity ? '' : count > capacity ? ' capacity-bar__fill--over' : count === capacity ? ' capacity-bar__fill--full' : '';
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

function RowMenu({ route, onDownload, onDeleteRoute, isDeletingRoute, drivers, onAssignDriver }) {
  const [open, setOpen] = useState(false);
  const [showAssignModal, setShowAssignModal] = useState(false);
  // Where to draw the panel, in viewport coordinates - computed fresh each
  // time the menu opens (see triggerRef.getBoundingClientRect() below).
  const [panelPos, setPanelPos] = useState(null);
  const ref = useRef(null);
  const triggerRef = useRef(null);
  const panelRef = useRef(null);

  // The trigger lives inside .routes-table, which has `overflow: hidden`
  // for its rounded corners - any row whose menu would open below the
  // table's bottom edge (the last row or two, on a short list, matching
  // what was reported) got silently clipped to nothing by that overflow,
  // even though the panel itself rendered correctly. Portaling it to
  // document.body with position:fixed escapes that (and every other
  // ancestor's overflow/stacking context) the same way any real dropdown
  // library does it.
  const updatePosition = () => {
    if (!triggerRef.current) return;
    const rect = triggerRef.current.getBoundingClientRect();
    setPanelPos({ top: rect.bottom + 6, right: window.innerWidth - rect.right });
  };

  useEffect(() => {
    if (!open) return undefined;
    const onDocClick = (e) => {
      if (ref.current && ref.current.contains(e.target)) return;
      if (panelRef.current && panelRef.current.contains(e.target)) return;
      setOpen(false);
    };
    const onReposition = () => updatePosition();
    document.addEventListener('mousedown', onDocClick);
    window.addEventListener('scroll', onReposition, true);
    window.addEventListener('resize', onReposition);
    return () => {
      document.removeEventListener('mousedown', onDocClick);
      window.removeEventListener('scroll', onReposition, true);
      window.removeEventListener('resize', onReposition);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  return (
    <div className="row-menu" ref={ref} onClick={(e) => e.stopPropagation()}>
      <button
        type="button"
        ref={triggerRef}
        className="btn btn--ghost row-menu__trigger"
        aria-label={`More actions for ${route.route_name}`}
        title="More actions"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => {
          if (!open) updatePosition();
          setOpen((v) => !v);
        }}
      >
        <MoreHorizontal width={16} height={16} />
      </button>
      {open && panelPos && createPortal(
        <div className="row-menu__panel" ref={panelRef} style={{ top: panelPos.top, right: panelPos.right }}>
          <button type="button" onClick={() => { setShowAssignModal(true); setOpen(false); }}>
            <IconUsers width={13} height={13} />
            Assign Driver
          </button>
          <button type="button" onClick={() => { onDownload(route); setOpen(false); }}>
            <LIconDownload width={13} height={13} />
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
            {isDeletingRoute === route.route_name ? <span className="spinner" /> : <Trash2 width={13} height={13} />}
            {isDeletingRoute === route.route_name ? 'Deleting…' : 'Delete Route'}
          </button>
        </div>,
        document.body,
      )}
      {showAssignModal && (
        <AssignDriverModal
          route={route}
          drivers={drivers}
          onClose={() => setShowAssignModal(false)}
          onAssign={(driverId, force) => onAssignDriver(route.route_id, driverId, force)}
        />
      )}
    </div>
  );
}

function RouteRow({ route, routeIdx, capacityFor, onOpen, onDownload, onDeleteRoute, isDeletingRoute, selected, onToggleSelect, drivers, onAssignDriver }) {
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
        <span className="route-row__badge" aria-hidden="true">
          {String(route.route_name || '').replace(/\D+/g, '') || routeIdx + 1}
        </span>
        <span className="route-row__titles">
          <span className="route-row__name">{route.route_name}</span>
          <span className="route-row__subtitle">{subtitle}</span>
        </span>
      </div>
      <div className="route-row__cell route-row__cell--vehicle">
        <span className={`vehicle-pill vehicle-pill--${route.vehicle_type === 'car' ? 'car' : 'bike'}`}>
          {route.vehicle_type === 'car' ? <LIconCarFront width={12} height={12} /> : <LIconBike width={12} height={12} />}
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
        <RowMenu route={route} onDownload={onDownload} onDeleteRoute={onDeleteRoute} isDeletingRoute={isDeletingRoute} drivers={drivers} onAssignDriver={onAssignDriver} />
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
// ---------------------------------------------------------------------
// Map look - Night (default, a calm dark ops map), Streets (Google's
// standard roadmap) or Satellite (hybrid: imagery + labels). Remembered
// per browser. Styles only apply to raster roadmaps, which is what these
// maps run as (no mapId).
// ---------------------------------------------------------------------
const NIGHT_MAP_STYLE = [
  { elementType: 'geometry', stylers: [{ color: '#111827' }] },
  { elementType: 'labels.text.fill', stylers: [{ color: '#8b95a7' }] },
  { elementType: 'labels.text.stroke', stylers: [{ color: '#0b1020' }] },
  { featureType: 'administrative', elementType: 'geometry.stroke', stylers: [{ color: '#2a3346' }] },
  { featureType: 'administrative.locality', elementType: 'labels.text.fill', stylers: [{ color: '#c3cad8' }] },
  { featureType: 'poi', stylers: [{ visibility: 'off' }] },
  { featureType: 'poi.park', elementType: 'geometry', stylers: [{ visibility: 'on' }, { color: '#13261f' }] },
  { featureType: 'road', elementType: 'geometry', stylers: [{ color: '#1f2937' }] },
  { featureType: 'road', elementType: 'geometry.stroke', stylers: [{ color: '#151c28' }] },
  { featureType: 'road', elementType: 'labels.text.fill', stylers: [{ color: '#7d8799' }] },
  { featureType: 'road.arterial', elementType: 'geometry', stylers: [{ color: '#263142' }] },
  { featureType: 'road.highway', elementType: 'geometry', stylers: [{ color: '#33415a' }] },
  { featureType: 'road.highway', elementType: 'geometry.stroke', stylers: [{ color: '#1b2433' }] },
  { featureType: 'road.highway', elementType: 'labels.text.fill', stylers: [{ color: '#b4bdcc' }] },
  { featureType: 'transit', stylers: [{ visibility: 'off' }] },
  { featureType: 'water', elementType: 'geometry', stylers: [{ color: '#0a1a2f' }] },
  { featureType: 'water', elementType: 'labels.text.fill', stylers: [{ color: '#3d5a80' }] },
];
const MAP_LOOK_KEY = 'rootplan_map_look';

function useMapLook() {
  const [look, setLookState] = useState(() => {
    try { return localStorage.getItem(MAP_LOOK_KEY) || 'night'; } catch { return 'night'; }
  });
  const setLook = (value) => {
    setLookState(value);
    try { localStorage.setItem(MAP_LOOK_KEY, value); } catch { /* private mode - fine */ }
  };
  const options = look === 'satellite'
    ? { mapTypeId: 'hybrid', styles: null }
    : { mapTypeId: 'roadmap', styles: look === 'night' ? NIGHT_MAP_STYLE : null };
  return [look, setLook, options];
}

function MapLookSwitch({ look, onChange, className = '' }) {
  const items = [['night', Moon, 'Night'], ['streets', LIconMap, 'Streets'], ['satellite', Satellite, 'Satellite']];
  return (
    <div className={`map-look ${className}`} role="group" aria-label="Map style">
      {items.map(([id, Icon, label]) => (
        <button
          key={id}
          type="button"
          className={`map-look__btn${look === id ? ' map-look__btn--on' : ''}`}
          onClick={() => onChange(id)}
          title={label}
          aria-pressed={look === id}
        >
          <Icon size={14} />
          <span>{label}</span>
        </button>
      ))}
    </div>
  );
}

// Re-renders every `ms` so "12s ago" style labels keep ticking.
function useNow(ms = 1000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}

function initials(name) {
  const parts = String(name || '?').trim().split(/\s+/).filter(Boolean);
  return ((parts[0]?.[0] || '?') + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase();
}

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
  const containerRef = useRef(null);
  const [look, setLook, lookOptions] = useMapLook();
  const [isFullscreen, setIsFullscreen] = useState(false);
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

  // Routes whose every stop is delivered but not yet ended: their line is
  // just the road back to the warehouse (from the driver, or the last stop).
  const [returnPaths, setReturnPaths] = useState({}); // route_id -> [{lat,lng}]
  const returnStarts = useMemo(() => {
    const out = {};
    for (const route of routes) {
      const orders = route.orders || [];
      if (route.route_run_status !== 'in_progress' || !orders.length || orders.some((o) => !o.is_delivered)) continue;
      const last = [...orders].reverse().find((o) => o.lat != null && o.lng != null);
      const from = driverPositions[route.route_id] || (last ? { lat: last.lat, lng: last.lng } : null);
      if (!from) continue;
      const to = route.warehouse?.lat != null ? route.warehouse : WAREHOUSE_FALLBACK;
      out[route.route_id] = { from: { lat: from.lat, lng: from.lng }, to, key: returnPathKey(from, to) };
    }
    return out;
  }, [routes, driverPositions]);
  const returnStartsKey = Object.entries(returnStarts).map(([id, r]) => `${id}:${r.key}`).join('|');
  useEffect(() => {
    let cancelled = false;
    Object.entries(returnStarts).forEach(([id, r]) => {
      fetchReturnPath(r.from, r.to).then((path) => {
        if (!cancelled) setReturnPaths((prev) => ({ ...prev, [id]: { key: r.key, path: path || [r.from, r.to] } }));
      });
    });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [returnStartsKey]);

  const allStops = useMemo(
    () => routes.flatMap((route) => (route.orders || []).filter((o) => o.lat != null && o.lng != null).map((o) => ({ lat: o.lat, lng: o.lng }))),
    [routes]
  );
  const fleetStats = useMemo(() => {
    const total = routes.reduce((n, r) => n + (r.orders || []).length, 0);
    const done = routes.reduce((n, r) => n + (r.orders || []).filter((o) => o.is_delivered).length, 0);
    return { total, done, pct: total ? Math.round((done / total) * 100) : 0 };
  }, [routes]);

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

  // Real browser fullscreen (works even inside the transformed slide-in
  // drawer, where position:fixed wouldn't), re-fitting once the size lands.
  useEffect(() => {
    const onChange = () => setIsFullscreen(document.fullscreenElement === containerRef.current);
    document.addEventListener('fullscreenchange', onChange);
    return () => document.removeEventListener('fullscreenchange', onChange);
  }, []);
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !window.google) return undefined;
    const t = window.setTimeout(() => { window.google.maps.event.trigger(map, 'resize'); fitToRoutes(); }, 120);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isFullscreen]);
  const toggleFullscreen = () => {
    const el = containerRef.current;
    if (!el) return;
    if (document.fullscreenElement) document.exitFullscreen?.().catch(() => {});
    else el.requestFullscreen?.().catch(() => {});
  };

  if (loadError) return <div className="empty-state fleet-map__empty">Could not load Google Maps.</div>;
  if (!isLoaded) return <div className="empty-state fleet-map__empty">Loading map…</div>;
  if (allStops.length === 0) return <div className="empty-state fleet-map__empty"><IconPin width={22} height={22} />No geocoded stops to show on the map yet.</div>;

  return (
    <div className={`fleet-map fleet-map--${look}${isFullscreen ? ' fleet-map--fullscreen' : ''}`} ref={containerRef}>
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
          ...lookOptions,
          disableDefaultUI: true, zoomControl: true,
          zoomControlOptions: { position: window.google?.maps?.ControlPosition?.RIGHT_BOTTOM },
          gestureHandling: 'greedy', backgroundColor: '#0b0f14', clickableIcons: false,
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
        <defs>
          <filter id="fm-glow" x="-10%" y="-10%" width="120%" height="120%">
            <feGaussianBlur stdDeviation="5" />
          </filter>
        </defs>
        {routes.map((route, idx) => {
          const color = routeColor(baseColorIndex + idx);
          const selected = selectedRouteName === route.route_name;
          const dimmed = selectedRouteName != null && !selected;
          const path = paths[route.route_id];
          const stops = (route.orders || []).filter((o) => o.lat != null && o.lng != null);
          const homeStart = returnStarts[route.route_id];
          const home = homeStart ? (returnPaths[route.route_id]?.path || [homeStart.from, homeStart.to]) : null;
          const liveFrom = homeStart && animatedDriverPositions[route.route_id];
          const linePoints = home
            ? (liveFrom ? [{ lat: liveFrom.lat, lng: liveFrom.lng }, ...home.slice(1)] : home)
            : path && path.length > 1
            ? path
            : stops.length > 1 ? stops.map((o) => ({ lat: o.lat, lng: o.lng })) : null;
          if (!linePoints) return null;
          const pixels = linePoints.map((p) => projectPoint(p.lat, p.lng)).filter(Boolean);
          if (pixels.length < 2) return null;
          const pointsAttr = pixels.map((p) => `${p.x},${p.y}`).join(' ');
          const opacity = dimmed ? 0.3 : 0.95;
          const running = route.route_run_status === 'in_progress';
          const halo = look === 'night' ? '#05070f' : '#fff';
          return (
            <g key={route.route_name}>
              {!dimmed && (selected || running || look === 'night') && (
                <polyline
                  points={pointsAttr}
                  fill="none"
                  stroke={color}
                  strokeWidth={selected ? 18 : 12}
                  strokeOpacity={selected ? 0.45 : 0.28}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  filter="url(#fm-glow)"
                  pointerEvents="none"
                />
              )}
              <polyline
                points={pointsAttr}
                fill="none"
                stroke={halo}
                strokeWidth={selected ? 8 : 6.5}
                strokeOpacity={dimmed ? 0.3 : look === 'night' ? 0.9 : 0.75}
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
              {running && !dimmed && (
                <polyline
                  className="fleet-map__flow"
                  points={pointsAttr}
                  fill="none"
                  stroke="#fff"
                  strokeWidth={2}
                  strokeOpacity={0.85}
                  strokeDasharray="2 16"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  pointerEvents="none"
                />
              )}
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
            const icon = pinIconUrl(color, o.is_delivered ? '✓' : stopIdx + 1, { size: selected ? 44 : 36, opacity: dimmed ? 0.4 : o.is_delivered ? 0.7 : 1 });
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
                title={`${route.route_name} · stop ${stopIdx + 1}: ${o.customer_name || 'Stop'}${o.is_delivered ? ' · delivered' : ''}`}
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
              const vehicle = vehicleIconUrl(route.vehicle_type, color, { size: 46, heading: driverPos.heading });
              pins.push(
                <span key={`${route.route_name}-driver-pulse`} className="fleet-map__driver-pulse" style={{ left: p.x, top: p.y, '--pulse': color }} />,
                <span key={`${route.route_name}-driver-tag`} className="fleet-map__driver-tag" style={{ left: p.x, top: p.y, '--pulse': color }}>
                  {route.route_name}
                </span>,
                <img
                  key={`${route.route_name}-driver`}
                  src={vehicle.url}
                  width={46}
                  height={46}
                  alt=""
                  className="fleet-map__pin fleet-map__pin--dot"
                  style={{ left: p.x, top: p.y, zIndex: 6 }}
                  title={`${route.route_name} · driver (${route.vehicle_type === 'car' ? 'car' : 'bike'})`}
                />
              );
            }
          }
          return pins;
        })}
      </div>

      <div className="fleet-map__hud">
        {routes.length > 1 && (
          <div className="fleet-map__stats">
            <div className="fm-ring" style={{ '--pct': fleetStats.pct }} title={`${fleetStats.pct}% delivered`}>
              <span>{fleetStats.pct}%</span>
            </div>
            <div className="fm-stat">
              <span className="fm-stat__value">{fleetStats.done}<small>/{fleetStats.total}</small></span>
              <span className="fm-stat__label">Delivered</span>
            </div>
            <div className="fm-stat">
              <span className="fm-stat__value">{routes.length}</span>
              <span className="fm-stat__label">Routes</span>
            </div>
            <div className="fm-stat">
              <span className="fm-stat__value">{liveTracking ? Object.keys(driverPositions).length : trackableRoutes.length}</span>
              <span className="fm-stat__label">On road</span>
            </div>
          </div>
        )}
        <div className="fleet-map__live-toggle">
          <span className={`fleet-map__live-dot${liveTracking ? ' is-on' : ''}`} />
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
      </div>

      <MapLookSwitch look={look} onChange={setLook} className="fleet-map__look" />

      <div className="fleet-map__toolbar">
        <button type="button" className="fleet-map__toolbar-btn" onClick={fitToRoutes} title="Fit to routes">
          <IconGauge width={15} height={15} />
        </button>
        {/* Manual re-fetch of every route's line, for the backend's
            road-snapped shape shifting without the stop list changing. */}
        <button
          type="button"
          className="fleet-map__toolbar-btn"
          onClick={() => setPathsRefreshNonce((n) => n + 1)}
          disabled={pathsLoading}
          title="Refresh route lines"
        >
          {pathsLoading ? <span className="spinner" /> : <IconRefresh width={15} height={15} />}
        </button>
        <button type="button" className="fleet-map__toolbar-btn" onClick={toggleFullscreen} title={isFullscreen ? 'Exit full screen' : 'Full screen'}>
          {isFullscreen ? <Minimize2 size={15} /> : <Maximize2 size={15} />}
        </button>
      </div>

      <div className="fleet-map__legend">
        {routes.map((route, idx) => {
          const n = (route.orders || []).length;
          const done = (route.orders || []).filter((o) => o.is_delivered).length;
          const running = route.route_run_status === 'in_progress';
          return (
            <button
              type="button"
              key={route.route_name}
              className={`fleet-map__legend-item${selectedRouteName === route.route_name ? ' fleet-map__legend-item--active' : ''}`}
              onClick={() => onSelectRoute(route.route_name)}
              style={{ '--swatch': routeColor(baseColorIndex + idx) }}
            >
              <span className="fleet-map__legend-swatch" style={{ background: routeColor(baseColorIndex + idx) }} />
              <span className="fleet-map__legend-name">{route.route_name}</span>
              {running && <span className="fleet-map__legend-live" title="On the road" />}
              <span className="fleet-map__legend-count mono-num">{done}/{n}</span>
              <span className="fleet-map__legend-bar"><span style={{ width: `${n ? (done / n) * 100 : 0}%` }} /></span>
            </button>
          );
        })}
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
                {route.vehicle_type === 'car' ? <LIconCarFront width={11} height={11} /> : <LIconBike width={11} height={11} />}
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
  selectedRouteNames, onToggleSelect, onToggleSelectAll, drivers, onAssignDriver,
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
        <div className="route-row__cell route-row__cell--progress">Load</div>
        <div className="route-row__cell route-row__cell--distance">Distance</div>
        <div className="route-row__cell route-row__cell--eta">Finish ETA</div>
        <div className="route-row__cell route-row__cell--status">Status</div>
        <div className="route-row__cell route-row__cell--actions" aria-label="Actions" />
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
            drivers={drivers}
            onAssignDriver={onAssignDriver}
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
  const lateCount = route.orders.filter((o) => o.is_late).length;
  const km = route.route_distance_km;
  const mins = route.route_time_minutes;
  const hero = [
    { label: 'Distance', value: km != null ? km : '—', unit: km != null ? 'km' : null, icon: <IconGauge width={16} height={16} />, tone: 'a' },
    { label: 'Travel time', value: mins != null ? mins : '—', unit: mins != null ? 'min' : null, icon: <IconClock width={16} height={16} />, tone: 'b' },
    { label: 'Finish ETA', value: route.estimated_finish_time || '—', unit: null, icon: <IconFlag width={16} height={16} />, tone: 'c' },
  ];
  const chips = [
    { label: 'Vehicle', value: route.vehicle_type === 'car' ? 'Car' : 'Bike' },
    { label: 'Addresses', value: `${count} of ${maxCapacity}` },
    ...(hasFlexRoom ? [
      { label: 'Base capacity', value: capacity },
      { label: 'Extra addresses', value: Math.max(count - capacity, 0) },
    ] : []),
    { label: 'Areas', value: route.areas && route.areas.length ? route.areas.length : '—' },
    { label: 'Avg / stop', value: route.average_stop_time != null ? `${route.average_stop_time} min` : '—' },
    { label: 'Late stops', value: lateCount, warn: lateCount > 0 },
  ];
  return (
    <div className="rdx__overview">
      <div className="rdx__hero-stats">
        {hero.map((h) => (
          <div key={h.label} className={`rdx__stat rdx__stat--${h.tone}`}>
            <span className="rdx__stat-icon">{h.icon}</span>
            <span className="rdx__stat-label">{h.label}</span>
            <span className="rdx__stat-value mono-num">{h.value}{h.unit && <small> {h.unit}</small>}</span>
          </div>
        ))}
      </div>
      <div className="rdx__chips">
        {chips.map((c) => (
          <div key={c.label} className={`rdx__chip${c.warn ? ' is-warn' : ''}`}>
            <span>{c.label}</span>
            <b className="mono-num">{c.value}</b>
          </div>
        ))}
      </div>
    </div>
  );
}

// "Add Address from Another Route" - the modal for pulling one stop
// directly from a different route. A move that stays within the
// destination's normal capacity is unrestricted in principle, but this
// modal only ever opens once the route is already full (see RouteDetail's
// canFlexAddresses/isFull gate), so what it's actually offering here is
// always the one manual capacity override - exactly one address, never
// more (backend-enforced too, see crud.move_orders_between_routes).
// Sentinel value for the source-route <select> meaning "pull from
// Unassigned Orders" rather than from another route - kept distinct from
// any real route_name so it can never collide with one.
const UNASSIGNED_SOURCE = '__unassigned__';

function AddAddressModal({
  destinationRoute, routes, pendingOrders, capacityFor, maxCapacityFor,
  onConfirm, onConfirmFromUnassigned, onClose, isSubmitting,
}) {
  const eligibleSourceRoutes = routes.filter((r) => r.route_name !== destinationRoute.route_name && r.orders.length > 0);
  const hasUnassigned = pendingOrders.length > 0;
  const [sourceRouteName, setSourceRouteName] = useState(
    hasUnassigned ? UNASSIGNED_SOURCE : (eligibleSourceRoutes[0]?.route_name || '')
  );
  const [selectedIds, setSelectedIds] = useState([]);

  const fromUnassigned = sourceRouteName === UNASSIGNED_SOURCE;
  const sourceRoute = fromUnassigned ? null : eligibleSourceRoutes.find((r) => r.route_name === sourceRouteName) || null;
  const sourceOrders = fromUnassigned ? pendingOrders : (sourceRoute?.orders || []);
  const maxCapacity = maxCapacityFor(destinationRoute.vehicle_type);
  const baseCapacity = capacityFor(destinationRoute.vehicle_type);
  const destinationCount = destinationRoute.orders.length;
  const remainingSlots = Math.max(maxCapacity - destinationCount, 0);

  const changeSource = (name) => {
    setSourceRouteName(name);
    setSelectedIds([]);
  };

  const toggleSelection = (idStr) => {
    setSelectedIds((prev) =>
      prev.includes(idStr) ? prev.filter((i) => i !== idStr) : [...prev, idStr]
    );
  };

  const isAllSelected =
    sourceOrders.length > 0 &&
    sourceOrders.every((o) => selectedIds.includes(String(o.order_id)));

  const toggleSelectAll = () => {
    if (isAllSelected) {
      setSelectedIds([]);
    } else {
      setSelectedIds(sourceOrders.map((o) => String(o.order_id)));
    }
  };

  const handleConfirm = async () => {
    if (selectedIds.length === 0) return;
    if (fromUnassigned) {
      const ok = await onConfirmFromUnassigned(selectedIds);
      if (ok) onClose();
      return;
    }
    if (!sourceRoute) return;
    const ok = await onConfirm(sourceRoute.route_id, selectedIds);
    if (ok) onClose();
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal modal--wide" onClick={(e) => e.stopPropagation()}>
        <div className="modal__header">
          <h3>Add Delivery Points</h3>
          <button type="button" className="modal__close" onClick={onClose}><IconX width={16} height={16} /></button>
        </div>

        <div className="modal__meta">
          <span>{destinationRoute.route_name}</span>
          <span>Vehicle: {destinationRoute.vehicle_type === 'car' ? 'Car' : 'Bike'}</span>
          <span>Current addresses: <strong className="mono-num">{destinationCount} / {baseCapacity}</strong></span>
        </div>

        <div className="modal__body">
          <p className="modal__hint">
            Select one or more delivery points from Unassigned Orders or another route to add to this route.
          </p>
          <label className="modal__field-label" htmlFor="add-address-source">Select Source</label>
          {!hasUnassigned && eligibleSourceRoutes.length === 0 ? (
            <div className="empty-state">No unassigned orders or other routes have addresses available to move.</div>
          ) : (
            <>
              <select
                id="add-address-source"
                className="select-compact modal__source-select"
                value={sourceRouteName}
                onChange={(e) => changeSource(e.target.value)}
              >
                {hasUnassigned && (
                  <option value={UNASSIGNED_SOURCE}>Unassigned Orders ({pendingOrders.length} waiting)</option>
                )}
                {eligibleSourceRoutes.map((r) => (
                  <option key={r.route_name} value={r.route_name}>
                    {r.route_name} ({r.orders.length}/{capacityFor(r.vehicle_type)} · {r.vehicle_type === 'car' ? 'Car' : 'Bike'})
                  </option>
                ))}
              </select>

              {sourceOrders.length > 0 && (
                <div className="modal__select-all-bar">
                  <label className="select-all-label">
                    <input
                      type="checkbox"
                      checked={isAllSelected}
                      onChange={toggleSelectAll}
                    />
                    <span>Select All ({sourceOrders.length} available)</span>
                  </label>
                  {selectedIds.length > 0 && (
                    <button type="button" className="btn-link-compact" onClick={() => setSelectedIds([])}>
                      Clear Selection ({selectedIds.length})
                    </button>
                  )}
                </div>
              )}

              <div className="modal__address-list">
                {sourceOrders.map((order) => {
                  const idStr = String(order.order_id);
                  const isChecked = selectedIds.includes(idStr);
                  return (
                    <label key={order.order_id} className="address-pick-row">
                      <input
                        type="checkbox"
                        checked={isChecked}
                        onChange={() => toggleSelection(idStr)}
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
                          {fromUnassigned ? 'Currently: Unassigned' : `Current route: ${sourceRoute.route_name}`}
                          {order.delivery_time ? ` · Slot ${order.delivery_time}` : ''}
                        </span>
                      </div>
                    </label>
                  );
                })}
              </div>
            </>
          )}
        </div>

        {(hasUnassigned || eligibleSourceRoutes.length > 0) && (
          <div className="modal__summary">
            <span>Selected: <strong className="mono-num">{selectedIds.length > 0 ? `${selectedIds.length} address${selectedIds.length === 1 ? '' : 'es'}` : 'none yet'}</strong></span>
            <span>Destination Route after adding: <strong className="mono-num">{destinationCount + selectedIds.length}</strong></span>
          </div>
        )}

        <div className="modal__footer">
          <button type="button" className="btn btn--ghost" onClick={onClose}>Cancel</button>
          <button
            type="button"
            className="btn btn--primary"
            disabled={(!fromUnassigned && !sourceRoute) || selectedIds.length === 0 || isSubmitting}
            onClick={handleConfirm}
          >
            {isSubmitting ? 'Adding…' : `Add ${selectedIds.length > 0 ? selectedIds.length : ''} to Route`}
          </button>
        </div>
      </div>
    </div>
  );
}

// A phone-in order or a customer added after the day's Excel upload - no
// spreadsheet row behind it. The backend geocodes the typed address, then
// either slots it into an existing route already covering the same area (if
// one has room) or creates a new route with the vehicle type picked here
// (used only as that fallback - see crud.add_manual_address).
function ManualAddressModal({ onConfirm, onClose, isSubmitting }) {
  const [address, setAddress] = useState('');
  const [customerName, setCustomerName] = useState('');
  const [deliveryTime, setDeliveryTime] = useState('');
  const [vehicleType, setVehicleType] = useState('bike');
  const [error, setError] = useState(null);

  const canSubmit = address.trim().length > 0 && !isSubmitting;

  const handleConfirm = async () => {
    if (!canSubmit) return;
    setError(null);
    const result = await onConfirm({
      address: address.trim(),
      customerName: customerName.trim(),
      deliveryTime: deliveryTime.trim(),
      vehicleType,
    });
    // handleAddManualAddress returns `true` on success, or
    // `{ ok: false, error }` on failure - the real backend/geocoding
    // error, shown right here where the admin is already looking,
    // instead of a placeholder pointing at a banner elsewhere on the
    // page that isn't even visible behind this modal's backdrop.
    if (result === true) onClose();
    else setError((result && result.error) || 'Could not add this address. Please try again.');
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal__header">
          <h3>Add Address Manually</h3>
          <button type="button" className="modal__close" onClick={onClose}><IconX width={16} height={16} /></button>
        </div>

        <div className="modal__body">
          <p className="modal__hint">
            For a phone-in order or a customer added after today's upload - no spreadsheet row needed.
            If an existing route already covers this area and has room, it's added there; otherwise a
            new route is created with the vehicle type below.
          </p>

          <label className="modal__field-label" htmlFor="manual-address-input">Address *</label>
          <textarea
            id="manual-address-input"
            className="modal__input"
            rows={3}
            value={address}
            onChange={(e) => setAddress(e.target.value)}
            placeholder="e.g. 12 Kamaraj Avenue, 1st Street, Adyar, Chennai 600020"
            disabled={isSubmitting}
          />

          <label className="modal__field-label" htmlFor="manual-address-customer">Customer Name (optional)</label>
          <input
            id="manual-address-customer"
            className="modal__input"
            type="text"
            value={customerName}
            onChange={(e) => setCustomerName(e.target.value)}
            disabled={isSubmitting}
          />

          <label className="modal__field-label" htmlFor="manual-address-slot">Delivery Slot (optional)</label>
          <input
            id="manual-address-slot"
            className="modal__input"
            type="text"
            value={deliveryTime}
            onChange={(e) => setDeliveryTime(e.target.value)}
            placeholder="e.g. 2:00 PM"
            disabled={isSubmitting}
          />

          <label className="modal__field-label" htmlFor="manual-address-vehicle">
            If a new route is needed
          </label>
          <select
            id="manual-address-vehicle"
            className="select-compact"
            value={vehicleType}
            onChange={(e) => setVehicleType(e.target.value)}
            disabled={isSubmitting}
          >
            <option value="bike">Bike</option>
            <option value="car">Car</option>
          </select>
        </div>

        {error && (
          <div className="modal__warning">
            <IconAlert width={13} height={13} />
            {error}
          </div>
        )}

        <div className="modal__footer">
          <button type="button" className="btn btn--ghost" onClick={onClose}>Cancel</button>
          <button type="button" className="btn btn--primary" disabled={!canSubmit} onClick={handleConfirm}>
            {isSubmitting ? 'Adding…' : 'Add Address'}
          </button>
        </div>
      </div>
    </div>
  );
}

// "Adjust Location" - when a geocoded pin is wrong (or just needs
// confirming) and the admin knows exactly where the customer's delivery
// point actually is. A single real, draggable google.maps.Marker on its
// own focused map, not the multi-stop route map's plain <img> pins
// (those don't support drag - see the route map's own comments on why
// they're positioned <img> elements rather than Marker in the first
// place). Saving marks the order "manually verified" (crud.
// set_manual_location) - never silently overwritten by a future
// auto-geocode again. Exported (named, alongside this file's default
// export) so App.jsx's Failed Orders ticket panel can use it too.
export function EmbeddedPinAdjuster({ order, onSaveLocation, isSubmitting }) {
  const { isLoaded, loadError } = useJsApiLoader({
    googleMapsApiKey: GOOGLE_MAPS_API_KEY,
    libraries: GOOGLE_MAPS_LIBRARIES,
  });

  const hasStartingPoint = order?.lat != null && order?.lng != null;
  const hasSuggestion = order?.suggested_lat != null && order?.suggested_lng != null;

  const [position, setPosition] = useState(() => {
    if (hasStartingPoint) return { lat: order.lat, lng: order.lng };
    if (hasSuggestion) return { lat: order.suggested_lat, lng: order.suggested_lng };
    return { lat: 12.989953044885272, lng: 80.21804157624011 };
  });

  const [isMoved, setIsMoved] = useState(false);

  useEffect(() => {
    if (hasStartingPoint) setPosition({ lat: order.lat, lng: order.lng });
    else if (hasSuggestion) setPosition({ lat: order.suggested_lat, lng: order.suggested_lng });
    else setPosition({ lat: 12.989953044885272, lng: 80.21804157624011 });
    setIsMoved(false);
  }, [order?.order_id, order?.lat, order?.lng, order?.suggested_lat, order?.suggested_lng]);

  const handleDragEnd = (e) => {
    if (e?.latLng) {
      setPosition({ lat: e.latLng.lat(), lng: e.latLng.lng() });
      setIsMoved(true);
    }
  };

  const handleSave = () => {
    if (onSaveLocation) {
      onSaveLocation(position.lat, position.lng);
      setIsMoved(false);
    }
  };

  return (
    <div className="embedded-pin-adjuster">
      <div className="pin-adjuster__banner">
        <span className="pin-adjuster__title">📍 Interactive Pin Location & Map Preview</span>
        <span className="pin-adjuster__sub">Drag the marker on the map below to set the exact delivery point</span>
      </div>

      <div className="adjust-location-map embedded-map">
        {loadError ? (
          <div className="empty-state">Could not load Google Maps.</div>
        ) : !isLoaded ? (
          <div className="empty-state">Loading Google Maps…</div>
        ) : (
          <GoogleMap
            mapContainerStyle={{ width: '100%', height: '100%' }}
            center={position}
            zoom={17}
            options={{
              streetViewControl: false,
              fullscreenControl: true,
              mapTypeControl: true,
              mapTypeControlOptions: {
                mapTypeIds: ['roadmap', 'hybrid'],
                style: window.google?.maps?.MapTypeControlStyle?.HORIZONTAL_BAR,
                position: window.google?.maps?.ControlPosition?.TOP_RIGHT,
              },
            }}
          >
            <Marker
              position={position}
              draggable
              onDragEnd={handleDragEnd}
            />
          </GoogleMap>
        )}
      </div>

      <div className="pin-adjuster__foot">
        <div className="pin-coords">
          <span>Lat: <strong className="mono-num">{position.lat.toFixed(6)}</strong></span>
          <span>Lng: <strong className="mono-num">{position.lng.toFixed(6)}</strong></span>
          {isMoved && <span className="pin-moved-badge">Pin Moved</span>}
        </div>
        <button
          type="button"
          className="btn btn--primary btn--compact"
          onClick={handleSave}
          disabled={isSubmitting}
        >
          {isSubmitting ? 'Saving Pin…' : 'Save Pin Position'}
        </button>
      </div>
    </div>
  );
}

export function AdjustLocationModal({ order, onConfirm, onClose, isSubmitting }) {
  const { isLoaded, loadError } = useJsApiLoader({
    googleMapsApiKey: GOOGLE_MAPS_API_KEY,
    libraries: GOOGLE_MAPS_LIBRARIES,
  });
  const hasStartingPoint = order?.lat != null && order?.lng != null;
  const hasSuggestion = order?.suggested_lat != null && order?.suggested_lng != null;
  const [position, setPosition] = useState(
    hasStartingPoint
      ? { lat: order.lat, lng: order.lng }
      : hasSuggestion
      ? { lat: order.suggested_lat, lng: order.suggested_lng }
      : { lat: 12.989953044885272, lng: 80.21804157624011 }
  );

  const handleConfirm = async () => {
    const ok = await onConfirm(position.lat, position.lng);
    if (ok) onClose();
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal modal--wide" onClick={(e) => e.stopPropagation()}>
        <div className="modal__header">
          <h3>Adjust Location</h3>
          <button type="button" className="modal__close" onClick={onClose}><IconX width={16} height={16} /></button>
        </div>

        <div className="modal__body">
          <p className="modal__hint">
            Drag the pin to the customer's exact delivery point{order?.order_id ? ` for order #${order.order_id}` : ''}.
            This becomes the permanent, manually-verified location.
          </p>

          <div className="adjust-location-map">
            {loadError ? (
              <div className="empty-state">Could not load Google Maps.</div>
            ) : !isLoaded ? (
              <div className="empty-state">Loading map…</div>
            ) : (
              <GoogleMap
                mapContainerStyle={{ width: '100%', height: '100%' }}
                center={position}
                zoom={17}
                options={{
                  streetViewControl: false, fullscreenControl: false,
                  mapTypeControl: true,
                  mapTypeControlOptions: {
                    mapTypeIds: ['roadmap', 'hybrid'],
                    style: window.google?.maps?.MapTypeControlStyle?.HORIZONTAL_BAR,
                    position: window.google?.maps?.ControlPosition?.TOP_RIGHT,
                  },
                }}
              >
                <Marker
                  position={position}
                  draggable
                  onDragEnd={(e) => setPosition({ lat: e.latLng.lat(), lng: e.latLng.lng() })}
                />
              </GoogleMap>
            )}
          </div>

          <div className="modal__meta">
            <span>Lat: <strong className="mono-num">{position.lat.toFixed(6)}</strong></span>
            <span>Lng: <strong className="mono-num">{position.lng.toFixed(6)}</strong></span>
          </div>
        </div>

        <div className="modal__footer">
          <button type="button" className="btn btn--ghost" onClick={onClose}>Cancel</button>
          <button type="button" className="btn btn--primary" disabled={isSubmitting} onClick={handleConfirm}>
            {isSubmitting ? 'Saving…' : 'Save Location'}
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
  return <span className={`tracking-status-pill tracking-status-pill--${status}`}>{info.text}</span>;
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

// Road path from the driver (or the last stop) back to the warehouse, once
// every order is delivered. Cached per ~100 m start cell so a moving
// driver re-routes a few times, not on every ping.
const returnPathCache = new Map();
function returnPathKey(from, to) {
  return `${from.lat.toFixed(3)},${from.lng.toFixed(3)}>${to.lat.toFixed(4)},${to.lng.toFixed(4)}`;
}
function fetchReturnPath(from, to) {
  const key = returnPathKey(from, to);
  if (returnPathCache.has(key)) return returnPathCache.get(key);
  const url = `https://router.project-osrm.org/route/v1/driving/${from.lng},${from.lat};${to.lng},${to.lat}?overview=full&geometries=geojson`;
  const promise = fetch(url)
    .then((r) => (r.ok ? r.json() : null))
    .then((data) => {
      const coords = data?.routes?.[0]?.geometry?.coordinates;
      return coords && coords.length > 1 ? coords.map(([lng, lat]) => ({ lat, lng })) : null;
    })
    .catch(() => null);
  returnPathCache.set(key, promise);
  return promise;
}
function useReturnPath(active, from, to) {
  const key = active && from && to ? returnPathKey(from, to) : null;
  const [road, setRoad] = useState({ key: null, path: null });
  useEffect(() => {
    if (!key) return undefined;
    let cancelled = false;
    fetchReturnPath(from, to).then((path) => { if (!cancelled) setRoad({ key, path }); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  if (!key) return null;
  return road.key === key && road.path ? road.path : [from, to];
}

// The "watch them move" view, redesigned as a live operations console:
// a big map on the left (Night / Streets / Satellite), and a side panel
// with the driver, live speed, distance, progress, the next stop and the
// full stop timeline. Click any stop in the timeline to fly the map to it.
// Data plumbing is unchanged: a 3s REST poll plus the WebSocket push, the
// newer fix always winning; the breadcrumb line mirrors WS fixes
// immediately so it never lags behind the vehicle.
function LiveMapModal({ route, driver, initialTracking, fetchRouteTracking, fetchRoutePlannedPath, onClose }) {
  const [tracking, setTracking] = useState(initialTracking);
  const [plannedPath, setPlannedPath] = useState([]);
  // Off the moment an admin drags the map to look at something else; the
  // recenter button turns it back on.
  const [autoFollow, setAutoFollow] = useState(true);
  const [look, setLook, lookOptions] = useMapLook();
  const mapRef = useRef(null);
  const lastPannedRef = useRef(null);
  // GoogleMap's `center` prop is only the initial view - all repositioning
  // goes through panTo below, so a live object here would fight it.
  const initialCenterRef = useRef(null);
  const now = useNow(1000);
  // Same overlay technique as FleetMap: this Maps release renders as a
  // vector map where classic Marker/Polyline can paint nothing, so pins,
  // lines and the vehicle are drawn as plain SVG/<img> over the map,
  // projected from the visible bounds on every view change.
  const [viewport, setViewport] = useState(null);
  const project = (lat, lng) => {
    if (!viewport) return null;
    const { ne, sw, w, h } = viewport;
    if (ne.lng === sw.lng || ne.lat === sw.lat) return null;
    return { x: ((lng - sw.lng) / (ne.lng - sw.lng)) * w, y: ((ne.lat - lat) / (ne.lat - sw.lat)) * h };
  };
  const { isLoaded, loadError } = useJsApiLoader({
    googleMapsApiKey: GOOGLE_MAPS_API_KEY,
    libraries: GOOGLE_MAPS_LIBRARIES,
  });

  useEffect(() => {
    let cancelled = false;
    const poll = () => fetchRouteTracking(route.route_id).then((data) => { if (!cancelled) setTracking(data); }).catch(() => {});
    poll();
    const interval = setInterval(poll, 3000);
    return () => { cancelled = true; clearInterval(interval); };
  }, [route.route_id, fetchRouteTracking]);

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const [wsFix, setWsFix] = useState(null); // {lat, lng, recorded_at, heading, accuracy, speed}
  const [wsPathPoints, setWsPathPoints] = useState([]); // [{lat, lng, recorded_at}]
  useTrackingSocket(true, (msg) => {
    if (msg?.type !== 'location' || msg.route_id !== route.route_id) return;
    setWsFix((prev) => {
      if (prev?.recorded_at && msg.recorded_at && prev.recorded_at >= msg.recorded_at) return prev;
      return { lat: msg.lat, lng: msg.lng, recorded_at: msg.recorded_at, heading: msg.heading, accuracy: msg.accuracy, speed: msg.speed };
    });
    setWsPathPoints((prev) => {
      if (prev.length && prev[prev.length - 1].recorded_at >= msg.recorded_at) return prev;
      return [...prev, { lat: msg.lat, lng: msg.lng, recorded_at: msg.recorded_at }].slice(-200);
    });
  });

  // The planned road route (depot -> every stop in order), from the
  // backend's OSRM-backed endpoint.
  useEffect(() => {
    let cancelled = false;
    fetchRoutePlannedPath(route.route_id)
      .then((data) => { if (!cancelled) setPlannedPath(data.path || []); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [route.route_id, fetchRoutePlannedPath]);

  const restLast = tracking?.last_location;
  const last = !restLast ? wsFix : !wsFix || restLast.recorded_at >= wsFix.recorded_at ? restLast : wsFix;
  const position = last ? { lat: last.lat, lng: last.lng } : null;
  const restPath = tracking?.path || [];
  const restPathLastRecordedAt = restPath.length ? restPath[restPath.length - 1].recorded_at : null;
  const path = [
    ...restPath,
    ...wsPathPoints.filter((p) => !restPathLastRecordedAt || p.recorded_at > restPathLastRecordedAt),
  ].map((p) => ({ lat: p.lat, lng: p.lng }));

  useEffect(() => {
    if (!restPathLastRecordedAt) return;
    setWsPathPoints((prev) => {
      const next = prev.filter((p) => p.recorded_at > restPathLastRecordedAt);
      return next.length === prev.length ? prev : next;
    });
  }, [restPathLastRecordedAt]);

  // Real GPS accuracy (meters) - drawn as a true-scale circle so a
  // slightly-off arrow reads as honest sensor uncertainty, not a bug.
  const accuracy = last?.accuracy != null && !Number.isNaN(last.accuracy) ? last.accuracy : null;
  // Phones report m/s; null/negative while stationary.
  const speedKmh = last?.speed != null && !Number.isNaN(last.speed) && last.speed > 0 ? Math.round(last.speed * 3.6) : 0;

  if (position && initialCenterRef.current === null) {
    initialCenterRef.current = position;
  }

  const animatedTargets = useMemo(() => (position ? { driver: position } : {}), [position?.lat, position?.lng]);
  const displayedPosition = useAnimatedPositions(animatedTargets).driver || position;

  // Only pan for a move big enough to be real movement (GPS noise would
  // otherwise make the map shake), and only while following.
  // The camera follows the gliding marker (not the raw fix, which would
  // jump the map ahead of the vehicle): once the vehicle drifts out of the
  // middle of the view, glide the map back to it.
  useEffect(() => {
    if (!displayedPosition || !mapRef.current || !autoFollow || !viewport) return;
    const p = project(displayedPosition.lat, displayedPosition.lng);
    if (!p) return;
    const dx = Math.abs(p.x - viewport.w / 2);
    const dy = Math.abs(p.y - viewport.h / 2);
    if (dx > viewport.w * 0.15 || dy > viewport.h * 0.15) {
      lastPannedRef.current = displayedPosition;
      mapRef.current.panTo(displayedPosition);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [displayedPosition?.lat, displayedPosition?.lng, autoFollow]);

  const handleRecenter = () => {
    setAutoFollow(true);
    if (position && mapRef.current) {
      lastPannedRef.current = position;
      mapRef.current.panTo(position);
      if (mapRef.current.getZoom() < 15) mapRef.current.setZoom(16);
    }
  };

  const focusStop = (o) => {
    if (!mapRef.current || o.lat == null || o.lng == null) return;
    setAutoFollow(false);
    mapRef.current.panTo({ lat: o.lat, lng: o.lng });
    if (mapRef.current.getZoom() < 16) mapRef.current.setZoom(17);
  };

  const fitAll = () => {
    const map = mapRef.current;
    if (!map || !window.google) return;
    const bounds = new window.google.maps.LatLngBounds();
    (route.orders || []).forEach((o) => { if (o.lat != null && o.lng != null) bounds.extend({ lat: o.lat, lng: o.lng }); });
    if (position) bounds.extend(position);
    if (bounds.isEmpty()) return;
    setAutoFollow(false);
    map.fitBounds(bounds, 60);
  };

  const orders = route.orders || [];
  const deliveredCount = orders.filter((o) => o.is_delivered).length;
  const pct = orders.length ? Math.round((deliveredCount / orders.length) * 100) : 0;
  const nextStop = orders.find((o) => !o.is_delivered);
  const legsById = Object.fromEntries((tracking?.delivery_legs || []).map((l) => [String(l.order_id), l]));
  const status = tracking?.tracking_status || 'not_started';
  const secondsSince = last ? Math.max(0, Math.round((now - new Date(last.recorded_at).getTime()) / 1000)) : null;
  const fresh = secondsSince != null && secondsSince < 60;
  const warehouse = route.warehouse?.lat != null ? route.warehouse : WAREHOUSE_FALLBACK;
  const runStatus = tracking?.route_run_status ?? route.route_run_status;
  // Every stop delivered but the route not ended yet = driving back.
  const returning = !nextStop && orders.length > 0 && runStatus === 'in_progress';
  const warehouseKm = returning && position ? haversineMeters(position, warehouse) / 1000 : null;
  const lastStop = [...orders].reverse().find((o) => o.lat != null && o.lng != null);
  const returnFrom = position || (lastStop ? { lat: lastStop.lat, lng: lastStop.lng } : null);
  const returnPath = useReturnPath(returning, returnFrom, warehouse);
  const vehicleColor = '#3b82f6';

  return createPortal(
    <div className="lmx-backdrop" onClick={onClose}>
      <div className="lmx" onClick={(e) => e.stopPropagation()} role="dialog" aria-label={`Live location of ${driver.name}`}>
        <div className="lmx__map-area">
          {loadError ? (
            <div className="lmx__empty">Could not load Google Maps.</div>
          ) : !isLoaded ? (
            <div className="lmx__empty"><span className="spinner" /> Loading map…</div>
          ) : position ? (
            <>
              <GoogleMap
                mapContainerClassName="lmx__map"
                center={initialCenterRef.current}
                zoom={16}
                onLoad={(map) => {
                  mapRef.current = map;
                  const update = () => {
                    const bounds = map.getBounds();
                    const div = map.getDiv();
                    if (!bounds || !div) return;
                    const ne = bounds.getNorthEast();
                    const sw = bounds.getSouthWest();
                    setViewport({ ne: { lat: ne.lat(), lng: ne.lng() }, sw: { lat: sw.lat(), lng: sw.lng() }, w: div.offsetWidth, h: div.offsetHeight });
                  };
                  map.addListener('idle', update);
                  map.addListener('bounds_changed', update);
                }}
                onDragStart={() => setAutoFollow(false)}
                options={{
                  ...lookOptions,
                  disableDefaultUI: true, zoomControl: true,
                  zoomControlOptions: { position: window.google?.maps?.ControlPosition?.RIGHT_BOTTOM },
                  gestureHandling: 'greedy', backgroundColor: '#0b0f14', clickableIcons: false,
                }}
              >
              </GoogleMap>

              {(() => {
                const toPts = (pts) => pts.map((p) => project(p.lat, p.lng)).filter(Boolean).map((p) => `${p.x},${p.y}`).join(' ');
                // All delivered: only the way back to the warehouse is drawn.
                const planned = !returning && plannedPath.length > 1 ? toPts(plannedPath) : '';
                let homeLine = '';
                if (returning && returnPath) {
                  // Start the line at the gliding vehicle, then follow the road.
                  const pts = displayedPosition ? [displayedPosition, ...returnPath.slice(1)] : returnPath;
                  homeLine = toPts(pts);
                }
                // Break the driven line wherever consecutive fixes are far
                // apart (a stale fix at the warehouse, GPS off then on) -
                // otherwise that gap draws as a straight line across the map.
                // The line is drawn up to where the vehicle *is on screen*
                // (the gliding marker), not to the newest fix - otherwise
                // the line shoots ahead and the vehicle chases it.
                const drawn = displayedPosition && path.length ? [...path.slice(0, -1), displayedPosition] : path;
                const trailSegments = [];
                drawn.forEach((pt, i) => {
                  if (i === 0 || haversineMeters(drawn[i - 1], pt) > TRAIL_JUMP_METERS) trailSegments.push([pt]);
                  else trailSegments[trailSegments.length - 1].push(pt);
                });
                const trails = returning ? [] : trailSegments.filter((seg) => seg.length > 1).map(toPts).filter(Boolean);
                const me = displayedPosition ? project(displayedPosition.lat, displayedPosition.lng) : null;
                const accPx = me && accuracy != null && viewport
                  ? ((accuracy / 111320) / (viewport.ne.lat - viewport.sw.lat)) * viewport.h
                  : 0;
                return (
                  <>
                    <svg className="fleet-map__lines lmx__overlay">
                      <defs>
                        <filter id="lmx-glow" x="-10%" y="-10%" width="120%" height="120%"><feGaussianBlur stdDeviation="4" /></filter>
                      </defs>
                      {planned && (
                        <polyline points={planned} fill="none" stroke="#fbbf24" strokeWidth={3.5} strokeOpacity={0.85} strokeDasharray="2 9" strokeLinecap="round" strokeLinejoin="round" />
                      )}
                      {homeLine && (
                        <g>
                          <polyline points={homeLine} fill="none" stroke="#fbbf24" strokeWidth={14} strokeOpacity={0.35} strokeLinecap="round" strokeLinejoin="round" filter="url(#lmx-glow)" />
                          <polyline points={homeLine} fill="none" stroke="#05070f" strokeWidth={7} strokeOpacity={0.6} strokeLinecap="round" strokeLinejoin="round" />
                          <polyline points={homeLine} fill="none" stroke="#fbbf24" strokeWidth={4} strokeLinecap="round" strokeLinejoin="round" />
                          <polyline className="fleet-map__flow" points={homeLine} fill="none" stroke="#fff" strokeWidth={2} strokeOpacity={0.9} strokeDasharray="2 16" strokeLinecap="round" strokeLinejoin="round" />
                        </g>
                      )}
                      {trails.map((trail, ti) => (
                        <g key={ti}>
                          <polyline points={trail} fill="none" stroke={vehicleColor} strokeWidth={14} strokeOpacity={0.45} strokeLinecap="round" strokeLinejoin="round" filter="url(#lmx-glow)" />
                          <polyline points={trail} fill="none" stroke="#05070f" strokeWidth={7} strokeOpacity={0.6} strokeLinecap="round" strokeLinejoin="round" />
                          <polyline points={trail} fill="none" stroke="#60a5fa" strokeWidth={4} strokeLinecap="round" strokeLinejoin="round" />
                        </g>
                      ))}
                      {me && accPx > 6 && (
                        <circle cx={me.x} cy={me.y} r={accPx} fill={vehicleColor} fillOpacity={0.12} stroke={vehicleColor} strokeOpacity={0.5} strokeWidth={1} />
                      )}
                    </svg>
                    <div className="fleet-map__pins">
                      {orders.filter((o) => o.lat != null && o.lng != null).map((o) => {
                        const pos = project(o.lat, o.lng);
                        if (!pos) return null;
                        const idx = orders.indexOf(o);
                        const isNext = nextStop && String(nextStop.order_id) === String(o.order_id);
                        const color = o.is_delivered ? '#10b981' : isNext ? '#f59e0b' : '#64748b';
                        const icon = pinIconUrl(color, o.is_delivered ? '✓' : idx + 1, { size: isNext ? 42 : 34 });
                        return (
                          <img
                            key={o.order_id}
                            src={icon.url}
                            width={icon.scaledSize.width}
                            height={icon.scaledSize.height}
                            alt=""
                            className="fleet-map__pin"
                            style={{ left: pos.x, top: pos.y, zIndex: isNext ? 4 : 2 }}
                            title={`${idx + 1}. ${o.customer_name || 'Stop'}${o.is_delivered ? ' · delivered' : ''}`}
                            onClick={() => focusStop(o)}
                          />
                        );
                      })}
                      {(() => {
                        const wp = project(warehouse.lat, warehouse.lng);
                        if (!wp) return null;
                        const wi = warehouseIconUrl(returning ? 46 : 38);
                        return (
                          <img
                            src={wi.url}
                            width={wi.size}
                            height={wi.size}
                            alt=""
                            className={`fleet-map__pin fleet-map__pin--dot${returning ? ' lmx__warehouse--target' : ''}`}
                            style={{ left: wp.x, top: wp.y, zIndex: 3 }}
                            title="Warehouse: every route starts and ends here"
                            onClick={() => focusStop(warehouse)}
                          />
                        );
                      })()}
                      {me && (
                        <>
                          <span className="fleet-map__driver-pulse" style={{ left: me.x, top: me.y, '--pulse': vehicleColor }} />
                          <img
                            src={vehicleIconUrl(route.vehicle_type, vehicleColor, { size: 54, heading: last?.heading }).url}
                            width={54}
                            height={54}
                            alt=""
                            className="fleet-map__pin fleet-map__pin--dot"
                            style={{ left: me.x, top: me.y, zIndex: 6 }}
                            title={`${driver.name} · ${route.route_name}`}
                          />
                        </>
                      )}
                    </div>
                  </>
                );
              })()}

              <div className="lmx__map-top">
                <span className={`lmx__live-chip${fresh ? ' is-fresh' : ''}`}>
                  <span className="lmx__live-dot" />
                  {fresh ? 'LIVE' : 'LAST KNOWN'}
                  {secondsSince != null && <em>{secondsSince < 60 ? `${secondsSince}s ago` : timeAgo(last.recorded_at)}</em>}
                </span>
                <MapLookSwitch look={look} onChange={setLook} />
              </div>

              <div className="lmx__map-tools">
                <button type="button" className={`lmx__tool${autoFollow ? ' is-on' : ''}`} onClick={handleRecenter} title="Follow driver">
                  <IconLocate width={18} height={18} />
                </button>
                <button type="button" className="lmx__tool" onClick={fitAll} title="Show whole route">
                  <IconGauge width={18} height={18} />
                </button>
              </div>

              <div className="lmx__map-legend">
                <span><i className="lmx__key lmx__key--trail" />Driven</span>
                <span><i className="lmx__key lmx__key--plan" />Planned</span>
                <span><i className="lmx__key lmx__key--next" />Next stop</span>
                <span><i className="lmx__key lmx__key--done" />Delivered</span>
              </div>
            </>
          ) : (
            <div className="lmx__empty">
              <IconLocate width={26} height={26} />
              <strong>Waiting for the first GPS ping</strong>
              <span>The map appears as soon as {driver.name}&apos;s phone reports a location.</span>
            </div>
          )}
        </div>

        <aside className="lmx__panel">
          <div className="lmx__panel-head">
            <div className={`lmx__avatar lmx__avatar--${status}`}>
              {initials(driver.name)}
              <span className="lmx__avatar-vehicle">{route.vehicle_type === 'car' ? <IconCar width={12} height={12} /> : <IconBike width={12} height={12} />}</span>
            </div>
            <div className="lmx__who">
              <span className="lmx__name">{driver.name}</span>
              <span className="lmx__sub">{route.route_name}{driver.vehicle_number ? ` · ${driver.vehicle_number}` : ''}</span>
            </div>
            <button type="button" className="lmx__close" onClick={onClose} aria-label="Close"><IconX width={16} height={16} /></button>
          </div>

          <div className="lmx__status-row">
            <TrackingStatusPill status={status} />
            {driver.mobile && (
              <a className="lmx__call" href={`tel:${driver.mobile}`}><Phone size={14} /> Call</a>
            )}
            {last?.maps_url && (
              <a className="lmx__call lmx__call--ghost" href={last.maps_url} target="_blank" rel="noopener noreferrer"><ExternalLink size={14} /> Maps</a>
            )}
          </div>

          <div className="lmx__kpis">
            <div className="lmx__kpi">
              <span className="lmx__kpi-value">{speedKmh}<small>km/h</small></span>
              <span className="lmx__kpi-label">Speed</span>
            </div>
            <div className="lmx__kpi">
              <span className="lmx__kpi-value">{tracking?.distance_travelled_km ?? 0}<small>km</small></span>
              <span className="lmx__kpi-label">Driven</span>
            </div>
            <div className="lmx__kpi">
              <span className="lmx__kpi-value">{deliveredCount}<small>/{orders.length}</small></span>
              <span className="lmx__kpi-label">Delivered</span>
            </div>
          </div>

          <div className="lmx__progress" aria-label={`${pct}% delivered`}>
            <span style={{ width: `${pct}%` }} />
          </div>

          {nextStop ? (
            <button type="button" className="lmx__next" onClick={() => focusStop(nextStop)}>
              <span className="lmx__next-label">Heading to · stop {orders.indexOf(nextStop) + 1}</span>
              <span className="lmx__next-name">{nextStop.customer_name || `Order #${nextStop.order_id}`}</span>
              <span className="lmx__next-addr">{nextStop.address}</span>
              <span className="lmx__next-meta">
                {nextStop.delivery_time && <span><IconClock width={12} height={12} /> Slot {nextStop.delivery_time}</span>}
                {nextStop.eta && <span>ETA {nextStop.eta}</span>}
                {nextStop.is_late && <span className="lmx__late">Late</span>}
              </span>
            </button>
          ) : returning ? (
            <button type="button" className="lmx__next lmx__next--return" onClick={() => focusStop(warehouse)}>
              <span className="lmx__next-label">All {orders.length} delivered · returning</span>
              <span className="lmx__next-name">Heading back to the warehouse</span>
              <span className="lmx__next-addr">
                {warehouseKm != null ? `${warehouseKm < 1 ? `${Math.round(warehouseKm * 1000)} m` : `${warehouseKm.toFixed(1)} km`} away (straight line). ` : ''}
                The driver ends the route on reaching it. Km keep counting until then.
              </span>
            </button>
          ) : orders.length > 0 ? (
            <div className="lmx__next lmx__next--done">
              <IconCheck width={18} height={18} /> Route complete · back at the warehouse
            </div>
          ) : null}

          <div className="lmx__timeline-head">Stops</div>
          <ol className="lmx__timeline">
            {orders.map((o, idx) => {
              const isNext = nextStop && String(nextStop.order_id) === String(o.order_id);
              const leg = legsById[String(o.order_id)];
              return (
                <li key={o.order_id} className={`lmx__stop${o.is_delivered ? ' is-done' : ''}${isNext ? ' is-next' : ''}`}>
                  <button type="button" onClick={() => focusStop(o)}>
                    <span className="lmx__stop-dot">{o.is_delivered ? <IconCheck width={11} height={11} /> : idx + 1}</span>
                    <span className="lmx__stop-body">
                      <span className="lmx__stop-name">{o.customer_name || `Order #${o.order_id}`}</span>
                      <span className="lmx__stop-meta">
                        {o.is_delivered
                          ? `Delivered${o.delivered_at ? ` ${new Date(o.delivered_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}` : ''}${leg?.time_minutes != null ? ` · ${leg.time_minutes} min` : ''}${leg?.distance_km != null ? ` · ${leg.distance_km} km` : ''}`
                          : [o.area, o.delivery_time && `Slot ${o.delivery_time}`, o.eta && `ETA ${o.eta}`].filter(Boolean).join(' · ')}
                      </span>
                    </span>
                  </button>
                </li>
              );
            })}
            {orders.length > 0 && (
              <li className={`lmx__stop lmx__stop--warehouse${returning ? ' is-next' : ''}${runStatus === 'completed' ? ' is-done' : ''}`}>
                <button type="button" onClick={() => focusStop(warehouse)}>
                  <span className="lmx__stop-dot"><IconFlag width={11} height={11} /></span>
                  <span className="lmx__stop-body">
                    <span className="lmx__stop-name">Warehouse</span>
                    <span className="lmx__stop-meta">{runStatus === 'completed' ? 'Route ended here' : returning ? 'Driving back now' : 'Route ends here'}</span>
                  </span>
                </button>
              </li>
            )}
          </ol>
        </aside>
      </div>
    </div>,
    document.body
  );
}

// Live status + assignment for the route currently open. Polls
// GET /api/routes/{id}/tracking every 5s while mounted.
function DriverTrackingCard({ route, drivers, onAssignDriver, onUnassignDriver, fetchRouteTracking, fetchRoutePlannedPath }) {
  const [tracking, setTracking] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState(null);
  const [showAssignModal, setShowAssignModal] = useState(false);
  const [showLiveMap, setShowLiveMap] = useState(false);
  const [isUnassigning, setIsUnassigning] = useState(false);
  const now = useNow(1000);

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
    const interval = setInterval(load, 5000);
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

  const orders = route.orders || [];
  const delivered = orders.filter((o) => o.is_delivered).length;
  const pct = orders.length ? Math.round((delivered / orders.length) * 100) : 0;
  const nextStop = orders.find((o) => !o.is_delivered);
  const last = tracking?.last_location;
  const secondsSince = last ? Math.max(0, Math.round((now - new Date(last.recorded_at).getTime()) / 1000)) : null;
  const speedKmh = last?.speed != null && last.speed > 0 ? Math.round(last.speed * 3.6) : 0;
  const legsById = Object.fromEntries((tracking?.delivery_legs || []).map((l) => [String(l.order_id), l]));
  const status = tracking?.tracking_status || 'not_started';

  return (
    <div className={`driver-card dtc${tracking?.driver ? ` driver-card--${status}` : ''}`}>
      <div className="driver-card__head">
        <h3><IconUsers width={14} height={14} /> Driver & Live Tracking</h3>
        <button type="button" className="modal__close driver-card__refresh" title="Refresh" onClick={load}>
          <IconRefresh width={14} height={14} />
        </button>
      </div>

      {isLoading ? (
        <div className="driver-card__loading"><span className="spinner" /> Loading…</div>
      ) : error ? (
        <div className="empty-state">{error}</div>
      ) : !tracking.driver ? (
        <div className="dtc__empty">
          <div className="dtc__empty-icon"><UserPlus size={26} /></div>
          <strong>No driver on this route yet</strong>
          <span>Assign a driver and the route appears in their app instantly.</span>
          <button type="button" className="btn btn--primary" onClick={() => setShowAssignModal(true)}>
            <IconPlus width={14} height={14} /> Assign Driver
          </button>
        </div>
      ) : (
        <>
          <div className="dtc__hero">
            <div className={`lmx__avatar lmx__avatar--${status}`}>
              {initials(tracking.driver.name)}
              <span className="lmx__avatar-vehicle">{route.vehicle_type === 'car' ? <IconCar width={12} height={12} /> : <IconBike width={12} height={12} />}</span>
            </div>
            <div className="dtc__who">
              <span className="driver-card__name">{tracking.driver.name}</span>
              <span className="dtc__meta mono-num">
                {tracking.driver.driver_code}{tracking.driver.vehicle_number ? ` · ${tracking.driver.vehicle_number}` : ''}
              </span>
              <TrackingStatusPill status={status} />
            </div>
            {tracking.driver.mobile && (
              <a className="dtc__call" href={`tel:${tracking.driver.mobile}`} title={`Call ${tracking.driver.mobile}`}>
                <Phone size={18} />
              </a>
            )}
          </div>

          <div className="dtc__kpis">
            <div className="dtc__kpi">
              <span className="dtc__kpi-value">{delivered}<small>/{orders.length}</small></span>
              <span className="dtc__kpi-label">Delivered</span>
            </div>
            <div className="dtc__kpi">
              <span className="dtc__kpi-value">{tracking.distance_travelled_km ?? 0}<small>km</small></span>
              <span className="dtc__kpi-label">Driven</span>
            </div>
            <div className="dtc__kpi">
              <span className="dtc__kpi-value">{speedKmh}<small>km/h</small></span>
              <span className="dtc__kpi-label">Speed</span>
            </div>
            <div className="dtc__kpi">
              <span className="dtc__kpi-value dtc__kpi-value--sm">
                {secondsSince == null ? '—' : secondsSince < 60 ? `${secondsSince}s` : timeAgo(last.recorded_at).replace(' ago', '')}
              </span>
              <span className="dtc__kpi-label">Last ping</span>
            </div>
          </div>

          <div className="dtc__progress">
            <div className="dtc__progress-bar"><span style={{ width: `${pct}%` }} /></div>
            <span className="dtc__progress-pct">{pct}%</span>
          </div>

          {nextStop ? (
            <div className="dtc__next">
              <span className="dtc__next-label">Next stop · {orders.indexOf(nextStop) + 1}</span>
              <span className="dtc__next-name">{nextStop.customer_name || `Order #${nextStop.order_id}`}</span>
              <span className="dtc__next-addr">{nextStop.address}</span>
            </div>
          ) : orders.length > 0 && (tracking.route_run_status ?? route.route_run_status) === 'in_progress' ? (
            <div className="dtc__next dtc__next--return">
              <span className="dtc__next-label">All {orders.length} delivered · returning</span>
              <span className="dtc__next-name">Heading back to the warehouse</span>
              <span className="dtc__next-addr">The driver ends the route on reaching the warehouse.</span>
            </div>
          ) : null}

          <ol className="lmx__timeline dtc__timeline">
            {orders.map((o, idx) => {
              const isNext = nextStop && String(nextStop.order_id) === String(o.order_id);
              const leg = legsById[String(o.order_id)];
              return (
                <li key={o.order_id} className={`lmx__stop${o.is_delivered ? ' is-done' : ''}${isNext ? ' is-next' : ''}`}>
                  <div className="lmx__stop-row">
                    <span className="lmx__stop-dot">{o.is_delivered ? <IconCheck width={11} height={11} /> : idx + 1}</span>
                    <span className="lmx__stop-body">
                      <span className="lmx__stop-name">{o.customer_name || `Order #${o.order_id}`}</span>
                      <span className="lmx__stop-meta">
                        {o.is_delivered
                          ? `Delivered${leg?.time_minutes != null ? ` · ${leg.time_minutes} min` : ''}${leg?.distance_km != null ? ` · ${leg.distance_km} km` : ''}`
                          : [o.delivery_time && `Slot ${o.delivery_time}`, o.eta && `ETA ${o.eta}`].filter(Boolean).join(' · ')}
                      </span>
                    </span>
                  </div>
                </li>
              );
            })}
          </ol>

          <div className="driver-card__actions dtc__actions">
            <button
              type="button"
              className="dtc__track"
              onClick={() => setShowLiveMap(true)}
              disabled={!tracking.last_location}
              title={tracking.last_location ? 'Open the live map' : 'No location reported yet'}
            >
              <span className="lmx__live-dot" /> Track live
            </button>
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
  onReassignOrder, onReopenDelivered, onAssignOrders, onReorderRoute,
  fetchRouteTracking, fetchRoutePlannedPath, onOpenDriverPage,
  selectedOrderId, onSelectOrder,
  maxCapacityFor, onMoveOrders, isMovingAddresses,
  onRemoveManualExtra, isRemovingManualExtra,
}) {
  const capacity = capacityFor(route.vehicle_type);
  const maxCapacity = maxCapacityFor(route.vehicle_type);
  const count = route.orders.length;
  const isFull = count >= capacity;
  const isAtMaxCapacity = count >= maxCapacity;
  const canFlexAddresses = maxCapacity > capacity;
  // The one admin override this route has used, if any (crud.
  // move_orders_between_routes / Route.manual_extra_order_id) - a real
  // tracked fact, not inferred from count > capacity (which can't tell
  // "used the override" apart from "just has more stops for some other
  // reason" - see the backend's own reasoning for why).
  const manualExtraOrderId = route.manual_extra_order_id != null ? String(route.manual_extra_order_id) : null;
  const isEdited = route.status === 'manually_edited';
  const status = routeStatus(route, capacity);
  const nodeRefs = useRef({});
  const [showAddAddressModal, setShowAddAddressModal] = useState(false);
  const deliveredCount = route.orders.filter((o) => o.is_delivered).length;

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
    if (target === '__reopen') {
      if (window.confirm(`Re-assign order #${orderId}?

It goes back to pending on ${route.route_name} and shows up in the driver's app again.`)) {
        onReopenDelivered?.(route, orderId);
      }
      return;
    }
    if (target === 'pending') {
      if (window.confirm('Remove this order from the route?\n\nIt will move to Unassigned Orders and can be assigned to another route later.')) {
        onReassignOrder(orderId, route.route_name, target);
      }
      return;
    }
    onReassignOrder(orderId, route.route_name, target);
  };

  return (
    <div className={`route-detail rdx route-hue-${routeIdx % 6}`}>
      <section className="rdx__hero">
        <div className="rdx__hero-top">
          <button type="button" className="rdx__back" onClick={onBack}>
            <IconChevron width={14} height={14} className="route-detail__back-icon" />
            All routes
          </button>
          <div className="rdx__hero-tags">
            <StatusBadge status={status} />
            {route.is_auto_created && <span className="tag tag--auto"><IconPlus width={11} height={11} />Auto-added</span>}
            {isEdited && <span className="tag tag--edited"><IconAlert width={11} height={11} />Manually edited</span>}
          </div>
        </div>

        <div className="rdx__hero-main">
          <div className="rdx__identity">
            <span className="rdx__vehicle-badge" title={route.vehicle_type === 'car' ? 'Car route' : 'Bike route'}>
              {route.vehicle_type === 'car' ? <LIconCarFront width={26} height={26} /> : <LIconBike width={26} height={26} />}
            </span>
            <div className="rdx__identity-text">
              <h2 className="rdx__title">{route.route_name}</h2>
              <span className="rdx__subtitle"><IconPin width={13} height={13} /> {formatAreaPath(route.areas, 5) || 'No areas yet'}</span>
            </div>
          </div>

          <div className="rdx__meters">
            <div className="rdx__meter">
              <div className="rdx__meter-head">
                <span>Load</span>
                <b>{count}<small>/{maxCapacity}</small></b>
              </div>
              <div className="rdx__meter-track">
                <span style={{ width: `${Math.min(100, (count / Math.max(1, maxCapacity)) * 100)}%` }} className={isAtMaxCapacity ? 'is-full' : ''} />
              </div>
            </div>
            <div className="rdx__meter">
              <div className="rdx__meter-head">
                <span>Delivered</span>
                <b>{deliveredCount}<small>/{count}</small></b>
              </div>
              <div className="rdx__meter-track">
                <span style={{ width: `${count ? (deliveredCount / count) * 100 : 0}%` }} className="is-good" />
              </div>
            </div>
          </div>
        </div>

        <div className="rdx__actions">
          <div className="rdx__actions-main">
            <button type="button" className="rdx__btn rdx__btn--primary" onClick={() => onOpenDriverPage(route)}>
              <IconUsers width={15} height={15} />
              Driver & Tracking
            </button>
            <button type="button" className="rdx__btn" onClick={() => onDownload(route)}>
              <IconDownload width={15} height={15} />
              Download sheet
            </button>
            <span className="rdx__divider" />
            <select
              className="rdx__select"
              value=""
              disabled={isFull || pendingOrders.length === 0}
              title={isFull ? 'This route is full' : 'Add an unassigned order to this route'}
              onChange={(e) => { const orderId = e.target.value; if (orderId) onAssignOrders([orderId], route.route_name); e.target.value = ''; }}
            >
              <option value="">
                {isFull ? 'Route full' : pendingOrders.length === 0 ? 'No unassigned orders' : '+ Add delivery'}
              </option>
              {pendingOrders.map((order) => (
                <option key={order.order_id} value={order.order_id}>
                  #{order.order_id} — {order.customer_name || 'Unnamed'}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="rdx__btn"
              disabled={canFlexAddresses && isAtMaxCapacity}
              title={
                canFlexAddresses && isAtMaxCapacity
                  ? `This route already used its one manual override (normal capacity ${capacity} + 1). Undo it below to free the override up again.`
                  : isFull
                  ? `Normal capacity (${capacity}) reached - one manual override is available to add exactly one more.`
                  : 'Add one specific delivery point from Unassigned Orders or another route.'
              }
              onClick={() => setShowAddAddressModal(true)}
            >
              <IconPlus width={15} height={15} />
              {canFlexAddresses && isAtMaxCapacity ? 'Manual override used' : 'Add specific address'}
            </button>
            <button
              type="button"
              className="rdx__btn"
              disabled={isChangingVehicle === route.route_name}
              title="Switch this route's vehicle type"
              onClick={() => onToggleVehicle(route)}
            >
              <IconRefresh width={14} height={14} />
              Switch to {route.vehicle_type === 'car' ? 'Bike' : 'Car'}
            </button>
          </div>
          <button
            type="button"
            className="rdx__btn rdx__btn--danger"
            disabled={isDeletingRoute === route.route_name}
            onClick={() => {
              if (window.confirm(`Delete ${route.route_name}? Its ${count} deliver${count === 1 ? 'y' : 'ies'} will move to Unassigned Orders - nothing is deleted.`)) {
                onDeleteRoute(route);
              }
            }}
          >
            {isDeletingRoute === route.route_name ? <span className="spinner" /> : <Trash2 width={14} height={14} />}
            {isDeletingRoute === route.route_name ? 'Deleting…' : 'Delete route'}
          </button>
        </div>
      </section>

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
                      {order.is_late ? 'Late' : 'On time'}
                    </span>
                    {order.is_delivered && <span className="stop-status stop-status--delivered"><IconCheck width={11} height={11} /> Delivered</span>}
                    {manualExtraOrderId === String(order.order_id) && (
                      <span className="stop-status stop-status--manual" title="Added past this route's normal capacity via the admin's one-time override">
                        🟡 Manual Override
                      </span>
                    )}
                  </div>
                  {manualExtraOrderId === String(order.order_id) && (
                    <button
                      type="button"
                      className="stop-undo-manual"
                      disabled={isRemovingManualExtra}
                      title="Remove this delivery and free the manual override up again for this route"
                      onClick={(e) => {
                        e.stopPropagation();
                        if (window.confirm('Undo the manual override?\n\nThis delivery moves back to Unassigned Orders, and this route can accept one manual addition again.')) {
                          onRemoveManualExtra?.(route.route_id);
                        }
                      }}
                    >
                      {isRemovingManualExtra ? 'Undoing…' : 'Undo manual override'}
                    </button>
                  )}
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
                      className={`stop-move${order.is_delivered ? ' stop-move--reassign' : ''}`}
                      value=""
                      title={order.is_delivered ? 'Re-assign this delivered order - deliver again on this route, or move it' : 'Move this stop to another route'}
                      onChange={(e) => { const target = e.target.value; if (target) handleMoveTo(order.order_id, target); e.target.value = ''; }}
                    >
                      <option value="">{order.is_delivered ? '↩ Re-assign…' : 'Move to…'}</option>
                      {order.is_delivered && <option value="__reopen">Deliver again on {route.route_name} (same driver)</option>}
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
          pendingOrders={pendingOrders}
          capacityFor={capacityFor}
          maxCapacityFor={maxCapacityFor}
          isSubmitting={isMovingAddresses}
          onClose={() => setShowAddAddressModal(false)}
          onConfirm={(sourceRouteId, orderIds) => onMoveOrders(sourceRouteId, route.route_id, orderIds)}
          onConfirmFromUnassigned={(orderIds) => onAssignOrders(orderIds, route.route_name, true)}
        />
      )}
    </div>
  );
}

// --------------------------------------------------------------------------
// Unassigned Orders panel (unchanged workflow, out of scope for this pass).
// --------------------------------------------------------------------------

function UnassignedPanel({ orders, routes, pendingOrders, capacityFor, isRouteFull, onAssignOrders, onCreateRoute, isCreatingRoute, onAdjustLocation }) {
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
              {onAdjustLocation && (
                // An unassigned order still has SOME lat/lng (that's why
                // it's not in Failed Addresses - it geocoded "successfully")
                // but that pin can still be wrong, especially for an order
                // created before a geocoding-accuracy fix - deploying a fix
                // never retroactively re-checks already-stored coordinates,
                // only new geocode attempts. Adjust Location was previously
                // reachable only from the Failed Orders panel, leaving no
                // way to correct a wrong-but-present pin sitting here.
                <button
                  type="button"
                  className="btn btn--ghost stop-move"
                  title="Move this order's pin to the correct spot"
                  onClick={() => onAdjustLocation(order.order_id)}
                >
                  Adjust Location
                </button>
              )}
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
  isCreatingRoute, isAddingManualAddress, isChangingVehicle, isDeletingRoute, isMovingAddresses, isRemovingManualExtra,
  onCreateRoute, onAddManualAddress, onToggleVehicle, onDeleteRoute, onReassignOrder, onReopenDelivered, onReorderRoute,
  onAssignOrders, onDownloadRoute, onMoveOrders, onRemoveManualExtra, onAdjustLocation, onRouteUpdated,
  requestedTab, requestedView, requestedLiveTracking, navCommandSeq,
  drivers, onAssignDriver, onUnassignDriver, fetchRouteTracking, fetchRoutePlannedPath,
  onViewChange, onTabChange,
}) {
  const [tab, setTab] = useState('routes');
  // Instant updates pushed by the backend (driver marked a stop delivered
  // in the app, admin re-assigned one) - patch that route in place.
  useTrackingSocket(true, (msg) => {
    if (msg?.type === 'route_updated' && msg.route) onRouteUpdated?.(msg.route);
  });
  const switchTab = (next) => { setTab(next); onTabChange?.(next); };
  // requestedTab is a one-way command from the sidebar. Keyed on
  // navCommandSeq (bumped only on explicit navigation) so it never fights
  // a tab the user picked here - switchTab reports those back up instead.
  useEffect(() => {
    if (requestedTab === 'routes' || requestedTab === 'unassigned') setTab(requestedTab);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [navCommandSeq]);
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

  // "Add Address Manually" - opens next to both Create Route selects below
  // (empty-state and the routes-page header).
  const [showManualAddressModal, setShowManualAddressModal] = useState(false);

  // List | Map | Split, for the Routes tab only - independent of
  // `selectedRouteName`/`view` above (which is specifically "a route's
  // full detail page is open"). Highlighting a route on the map/in the
  // split list doesn't open its detail page, so this gets its own state.
  const [viewMode, setViewMode] = useState('list');
  const [mapSelectedRouteName, setMapSelectedRouteName] = useState(null);
  // Same one-way command shape as requestedTab above - "Live Tracking" in
  // the sidebar jumps the Routes tab into Map view, "Routes" back to the
  // list. requestedLiveTracking (passed straight through to FleetMap
  // below) additionally flips its live layer on.
  useEffect(() => {
    if (requestedView === 'map' || requestedView === 'list') setViewMode(requestedView);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [navCommandSeq]);

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
    <div className={`board board--workspace${tab === 'unassigned' ? ' board--workspace-unassigned' : ' board--workspace-routes'}`} id="route-workspace">
      <div className="workspace-tabs">
        <button type="button" className={`workspace-tab${tab === 'routes' ? ' workspace-tab--active' : ''}`} onClick={() => switchTab('routes')}>
          <LIconRoute width={14} height={14} />
          Routes
          <span className="workspace-tab__count mono-num">{routes.length}</span>
        </button>
        <button
          type="button"
          className={`workspace-tab${tab === 'unassigned' ? ' workspace-tab--active' : ''}`}
          onClick={() => switchTab('unassigned')}
        >
          <PackageSearch width={14} height={14} />
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
          onAdjustLocation={onAdjustLocation}
          isCreatingRoute={isCreatingRoute}
        />
      ) : routes.length === 0 ? (
        <div className="empty-state">
          <PackageSearch width={22} height={22} />
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
          <button type="button" className="btn btn--ghost" onClick={() => setShowManualAddressModal(true)}>
            + Add Address Manually
          </button>
        </div>
      ) : (
        <>
        <div className="routes-page">
          <div className="routes-page__header">
            <div>
              <h2 className="routes-page__title"><LIconRoute width={20} height={20} /> Routes</h2>
              <p className="routes-page__subtitle">Manage delivery routes, vehicles and stops</p>
            </div>
            <div className="routes-page__header-actions">
              <div className="view-toggle" role="group" aria-label="Routes view">
                <button type="button" className={`view-toggle__btn${viewMode === 'list' ? ' view-toggle__btn--active' : ''}`} onClick={() => setViewMode('list')}>
                  <LIconList width={13} height={13} /> List
                </button>
                <button type="button" className={`view-toggle__btn${viewMode === 'map' ? ' view-toggle__btn--active' : ''}`} onClick={() => setViewMode('map')}>
                  <LIconMap width={13} height={13} /> Map
                </button>
                <button type="button" className={`view-toggle__btn${viewMode === 'split' ? ' view-toggle__btn--active' : ''}`} onClick={() => setViewMode('split')}>
                  <Columns2 width={13} height={13} /> Split
                </button>
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
              <button type="button" className="btn btn--ghost" onClick={() => setShowManualAddressModal(true)}>
                <MapPinPlus width={14} height={14} /> Add Address Manually
              </button>
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
              drivers={drivers}
              onAssignDriver={onAssignDriver}
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
                onReopenDelivered={onReopenDelivered}
                onAssignOrders={onAssignOrders}
                onReorderRoute={onReorderRoute}
                maxCapacityFor={maxCapacityFor}
                onMoveOrders={onMoveOrders}
                isMovingAddresses={isMovingAddresses}
                onRemoveManualExtra={onRemoveManualExtra}
                isRemovingManualExtra={isRemovingManualExtra}
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

      {showManualAddressModal && (
        <ManualAddressModal
          onConfirm={onAddManualAddress}
          onClose={() => setShowManualAddressModal(false)}
          isSubmitting={isAddingManualAddress}
        />
      )}
    </div>
  );
}
