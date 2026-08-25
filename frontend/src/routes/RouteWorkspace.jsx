import { useEffect, useMemo, useRef, useState } from 'react';
import { GoogleMap, Marker, Polyline, InfoWindow, useJsApiLoader } from '@react-google-maps/api';
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
const GOOGLE_MAPS_API_KEY = import.meta.env.VITE_GOOGLE_MAPS_API_KEY || 'AIzaSyDodjkyPxxK0C_5m6pX0u-hAj2kHeeI-Zo';
const GOOGLE_MAPS_LIBRARIES = [];

// A dark map style matching this app's own dark theme (--paper/--surface/
// --rule/--ink-soft) instead of Google's default light basemap fighting
// the rest of the UI around it.
const DARK_MAP_STYLE = [
  { elementType: 'geometry', stylers: [{ color: '#141414' }] },
  { elementType: 'labels.text.stroke', stylers: [{ color: '#141414' }] },
  { elementType: 'labels.text.fill', stylers: [{ color: '#9a9aa4' }] },
  { featureType: 'administrative', elementType: 'geometry', stylers: [{ color: '#262626' }] },
  { featureType: 'poi', elementType: 'labels', stylers: [{ visibility: 'off' }] },
  { featureType: 'poi.park', elementType: 'geometry', stylers: [{ color: '#1c2a1f' }] },
  { featureType: 'road', elementType: 'geometry', stylers: [{ color: '#1c1c1c' }] },
  { featureType: 'road', elementType: 'geometry.stroke', stylers: [{ color: '#262626' }] },
  { featureType: 'road.highway', elementType: 'geometry', stylers: [{ color: '#262626' }] },
  { featureType: 'transit', elementType: 'geometry', stylers: [{ color: '#1c1c1c' }] },
  { featureType: 'water', elementType: 'geometry', stylers: [{ color: '#0a0a0a' }] },
  { featureType: 'water', elementType: 'labels.text.fill', stylers: [{ color: '#63636b' }] },
];

function capacityText(count, capacity) {
  if (!capacity) return `${count}`;
  if (count >= capacity) return `${count} / ${capacity} — FULL`;
  if (capacity - count === 1) return `${count} / ${capacity} — 1 slot remaining`;
  return `${count} / ${capacity}`;
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
          {route.google_maps_url && (
            <a href={route.google_maps_url} target="_blank" rel="noopener noreferrer" onClick={() => setOpen(false)}>
              <IconPin width={13} height={13} />
              Open in Google Maps
            </a>
          )}
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
            <IconX width={13} height={13} />
            Delete Route
          </button>
        </div>
      )}
    </div>
  );
}

function RouteRow({ route, routeIdx, capacityFor, onOpen, onDownload, onDeleteRoute, isDeletingRoute }) {
  const capacity = capacityFor(route.vehicle_type);
  const count = route.orders.length;
  const status = routeStatus(route, capacity);
  const subtitle = route.areas && route.areas.length ? route.areas.slice(0, 3).join(' → ') : 'No deliveries yet';

  return (
    <div
      className={`route-row route-hue-${routeIdx % 6}`}
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => { if (e.key === 'Enter') onOpen(); }}
    >
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
        <span className="route-row__progress-text mono-num">{count} / {capacity}</span>
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

function RoutesTable({
  routes, capacityFor, onOpen, onDownload, onDeleteRoute, isDeletingRoute, hasActiveFilters, onClearFilters,
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

  return (
    <div className="routes-table">
      <div className="routes-table__head">
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
    { label: 'Addresses', value: `${count} / ${maxCapacity}` },
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
    // the tightest poll in the app (the card behind it polls slower).
    const poll = () => fetchRouteTracking(route.route_id).then((data) => { if (!cancelled) setTracking(data); }).catch(() => {});
    poll();
    const interval = setInterval(poll, 3000);
    return () => { cancelled = true; clearInterval(interval); };
  }, [route.route_id, fetchRouteTracking]);

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

  const last = tracking?.last_location;
  const position = last ? { lat: last.lat, lng: last.lng } : null;
  const path = (tracking?.path || []).map((p) => ({ lat: p.lat, lng: p.lng }));

  if (position && initialCenterRef.current === null) {
    initialCenterRef.current = position;
  }

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
                styles: DARK_MAP_STYLE, disableDefaultUI: true, zoomControl: true,
                gestureHandling: 'greedy', backgroundColor: '#0a0a0a',
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
                    scale: 5,
                    fillColor: o.is_delivered ? '#4fc98a' : '#63636b',
                    fillOpacity: 1,
                    strokeColor: '#0a0a0a',
                    strokeWeight: 1,
                  }}
                  title={`${idx + 1}. ${o.customer_name || 'Stop'}`}
                />
              ))}
              {/* The translucent halo behind the solid dot is Google Maps'
                  own "your location" glyph - reads as live/GPS at a glance
                  instead of just another plain pin. */}
              <Marker
                position={position}
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
                position={position}
                zIndex={3}
                onClick={() => setShowInfo((v) => !v)}
                icon={{
                  path: window.google.maps.SymbolPath.CIRCLE,
                  scale: 8,
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
          {last && (
            <a className="btn btn--outline" href={last.maps_url} target="_blank" rel="noopener noreferrer">
              <IconPin width={14} height={14} /> Open in Google Maps
            </a>
          )}
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

function RouteDetail({
  route, routeIdx, routes, capacityFor, pendingOrders,
  onBack, onToggleVehicle, isChangingVehicle, onDeleteRoute, isDeletingRoute, onDownload,
  onReassignOrder, onAssignOrders, onReorderRoute,
  drivers, onAssignDriver, onUnassignDriver, fetchRouteTracking, fetchRoutePlannedPath,
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
        <div className="route-detail__title-row">
          <div className="route-detail__title-block">
            <h2 className="route-detail__title">{route.route_name}</h2>
            <span className="route-detail__subtitle">
              {route.areas && route.areas.length ? route.areas.join(' → ') : 'No deliveries yet'}
            </span>
          </div>
          <StatusBadge status={status} />
          {route.is_auto_created && <span className="tag tag--auto"><IconPlus width={11} height={11} />Auto-added</span>}
          {isEdited && <span className="tag tag--edited"><IconAlert width={11} height={11} />Manually edited</span>}
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
          {route.google_maps_url && (
            <a className="btn btn--secondary" href={route.google_maps_url} target="_blank" rel="noopener noreferrer">
              <IconPin width={14} height={14} />
              Open in Maps
            </a>
          )}
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
            <IconX width={14} height={14} />
            Delete Route
          </button>
        </div>
      </div>

      <RouteOverviewStrip route={route} capacity={capacity} maxCapacity={maxCapacity} />

      <DriverTrackingCard
        route={route}
        drivers={drivers}
        onAssignDriver={onAssignDriver}
        onUnassignDriver={onUnassignDriver}
        fetchRouteTracking={fetchRouteTracking}
        fetchRoutePlannedPath={fetchRoutePlannedPath}
      />

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
          <select value={bulkTarget} onChange={(e) => setBulkTarget(e.target.value)}>
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
  requestedTab,
  drivers, onAssignDriver, onUnassignDriver, fetchRouteTracking, fetchRoutePlannedPath,
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
  const [selectedRouteName, setSelectedRouteName] = useState(null);
  const [selectedOrderId, setSelectedOrderId] = useState(null);
  const [routeSearch, setRouteSearch] = useState('');
  const [vehicleFilter, setVehicleFilter] = useState('all');
  const [statusFilter, setStatusFilter] = useState('all');
  const [locationFilter, setLocationFilter] = useState('all');

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
      ) : view === 'detail' && selectedRoute ? (
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
          drivers={drivers}
          onAssignDriver={onAssignDriver}
          onUnassignDriver={onUnassignDriver}
          fetchRouteTracking={fetchRouteTracking}
          fetchRoutePlannedPath={fetchRoutePlannedPath}
        />
      ) : (
        <div className="routes-page">
          <div className="routes-page__header">
            <div>
              <h2 className="routes-page__title">Routes</h2>
              <p className="routes-page__subtitle">Manage delivery routes, vehicles and stops</p>
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

          <RoutesTable
            routes={filteredRoutes}
            capacityFor={capacityFor}
            onOpen={openRoute}
            onDownload={onDownloadRoute}
            onDeleteRoute={onDeleteRoute}
            isDeletingRoute={isDeletingRoute}
            hasActiveFilters={hasActiveFilters}
            onClearFilters={clearFilters}
          />
        </div>
      )}
    </div>
  );
}
