import { useEffect, useMemo, useRef, useState } from 'react';
import { GoogleMap, OverlayView, useJsApiLoader } from '@react-google-maps/api';
import {
  IconRoute, IconCar, IconBike, IconClock, IconCheck, IconAlert, IconPin, IconPlus, IconInbox,
  IconDownload, IconRefresh, IconArrowUp, IconArrowDown, IconGauge, IconFlag, IconSearch, IconX,
  IconUsers, IconChevron,
} from '../icons';
import './routeWorkspace.css';

// Set VITE_GOOGLE_MAPS_API_KEY (a browser-restricted Google Maps
// JavaScript API key - a different product from the server-side Geocoding
// key) to turn on the live marker map. Every other part of this workspace
// works with or without it - the map panel falls back to a plain "open in
// Google Maps" link until it's set.
const GOOGLE_MAPS_API_KEY = (import.meta.env.VITE_GOOGLE_MAPS_API_KEY || '').trim();

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

function RouteSidebarCard({ route, isSelected, onSelect, capacityFor }) {
  const capacity = capacityFor(route.vehicle_type);
  const count = route.orders.length;
  const areas = route.areas && route.areas.length ? route.areas.slice(0, 3).join(' • ') : 'No deliveries yet';
  return (
    <button type="button" className={`route-nav-card${isSelected ? ' route-nav-card--selected' : ''}`} onClick={onSelect}>
      <div className="route-nav-card__top">
        <span className="route-nav-card__name">{route.route_name}</span>
        <span className="vehicle-pill">
          {route.vehicle_type === 'car' ? <IconCar width={12} height={12} /> : <IconBike width={12} height={12} />}
          {route.vehicle_type === 'car' ? 'Car' : 'Bike'}
        </span>
      </div>
      <div className="route-nav-card__stops mono-num">{capacityText(count, capacity)}</div>
      <CapacityBar count={count} capacity={capacity} />
      <div className="route-nav-card__areas">{areas}</div>
      <div className="route-nav-card__status">
        <span className={`status-dot status-dot--${route.status === 'manually_edited' ? 'edited' : 'planned'}`} />
        {route.status === 'manually_edited' ? 'Manually edited' : 'Assigned'}
      </div>
    </button>
  );
}

// Big red numbered pins (matching the "View pin" red used elsewhere), one
// per stop, click-synced with the delivery timeline in both directions.
function RouteMapPanel({ route, selectedOrderId, onSelectOrder }) {
  // useJsApiLoader must run every render regardless of branch (rules of
  // hooks) - it only actually injects Google's script tag once a non-empty
  // key is passed, so an unset key never fires a network request.
  const { isLoaded, loadError } = useJsApiLoader({
    id: 'rootplan-google-map',
    googleMapsApiKey: GOOGLE_MAPS_API_KEY,
  });

  if (!GOOGLE_MAPS_API_KEY) {
    return (
      <div className="route-map route-map--placeholder">
        <IconPin width={26} height={26} />
        <p>Live map view needs a Google Maps API key.</p>
        <p className="route-map__hint">Set VITE_GOOGLE_MAPS_API_KEY (Maps JavaScript API, browser-restricted) to turn this on.</p>
        {route?.google_maps_url && (
          <a className="btn btn--outline" href={route.google_maps_url} target="_blank" rel="noopener noreferrer">
            <IconPin width={14} height={14} />
            Open full route in Google Maps
          </a>
        )}
      </div>
    );
  }
  if (loadError) {
    return <div className="route-map route-map--placeholder"><p>Map unavailable.</p></div>;
  }
  if (!isLoaded) {
    return <div className="route-map route-map--placeholder"><div className="skeleton-line" /></div>;
  }
  if (!route) {
    return <div className="route-map route-map--placeholder"><p>Select a route to see it on the map.</p></div>;
  }

  const stops = route.orders.filter((o) => o.lat != null && o.lng != null);
  if (stops.length === 0) {
    return <div className="route-map route-map--placeholder"><p>No located deliveries on this route yet.</p></div>;
  }

  const center = { lat: stops[0].lat, lng: stops[0].lng };

  return (
    <GoogleMap
      mapContainerClassName="route-map__canvas"
      center={center}
      zoom={12}
      options={{ disableDefaultUI: true, zoomControl: true, clickableIcons: false, gestureHandling: 'greedy' }}
    >
      {stops.map((order, idx) => {
        const isSelected = String(order.order_id) === String(selectedOrderId);
        return (
          <OverlayView
            key={order.order_id}
            position={{ lat: order.lat, lng: order.lng }}
            mapPaneName={OverlayView.OVERLAY_MOUSE_TARGET}
            getPixelPositionOffset={(w, h) => ({ x: -(w / 2), y: -h })}
          >
            <button
              type="button"
              className={`map-marker${isSelected ? ' map-marker--selected' : ''}${order.is_late ? ' map-marker--late' : ''}`}
              onClick={() => onSelectOrder(order.order_id)}
              title={`${idx + 1} — ${order.customer_name || order.order_id}`}
            >
              {idx + 1}
            </button>
          </OverlayView>
        );
      })}
    </GoogleMap>
  );
}

// Faithful reconstruction of the original route card design (vehicle chip,
// data-strip stats, collapsible route-timeline with a depot -> stops ->
// finish line) for the selected route, per feedback asking for the old
// look back - now living inside the workspace's delivery pane instead of a
// page-wide stacked list. The AREA line, red map pin, vehicle toggle, Add
// Delivery and Delete Route additions (all requested separately, after the
// original design shipped) are kept.
function RouteManifest({
  route, routeIdx, routes, capacityFor, pendingOrders,
  onToggleVehicle, isChangingVehicle, onDeleteRoute, isDeletingRoute, onDownload,
  onReassignOrder, onAssignOrders, onReorderRoute,
  selectedOrderId, onSelectOrder,
}) {
  const [collapsed, setCollapsed] = useState(false);
  const capacity = capacityFor(route.vehicle_type);
  const count = route.orders.length;
  const isFull = count >= capacity;
  const isEdited = route.status === 'manually_edited';
  const nodeRefs = useRef({});

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

  return (
    <div className={`manifest route-hue-${routeIdx % 6}`}>
      <div className="manifest__head">
        <div className="manifest__head-left">
          <button
            type="button"
            className="vehicle-chip vehicle-chip--toggle"
            disabled={isChangingVehicle === route.route_name}
            title="Switch this route's vehicle type"
            onClick={() => onToggleVehicle(route)}
          >
            {route.vehicle_type === 'car' ? <IconCar width={14} height={14} /> : <IconBike width={14} height={14} />}
            {route.vehicle_type === 'car' ? 'Car' : 'Bike'}
            <IconRefresh width={11} height={11} className="vehicle-chip__swap" />
          </button>
          <h3 className="manifest__name">{route.route_name}</h3>
          <span className="driver-chip" title="No driver roster is tracked yet">
            <IconUsers width={11} height={11} />
            Unassigned
          </span>
          <span className="tag tag--capacity mono-num">{count}/{capacity}</span>
          {route.is_auto_created && (
            <span className="tag tag--auto"><IconPlus width={11} height={11} />Auto-added</span>
          )}
          {route.late_deliveries && route.late_deliveries.length > 0 && (
            <span className="tag tag--late"><IconClock width={11} height={11} />{route.late_deliveries.length} late</span>
          )}
          {isEdited && (
            <span className="tag tag--edited"><IconAlert width={11} height={11} />Manually edited</span>
          )}
        </div>

        <div className="manifest__actions">
          <select
            className="stop-move add-delivery-select"
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
          <button className="download-link" onClick={() => onDownload(route)}>
            <IconDownload width={14} height={14} />
            Download sheet
          </button>
          {route.google_maps_url && (
            <a className="map-link" href={route.google_maps_url} target="_blank" rel="noopener noreferrer">
              <IconPin width={14} height={14} />
              Open in Google Maps
            </a>
          )}
          <button
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

      <div className="data-strip">
        <div className="data-strip__item">
          <span className="data-strip__label"><IconRoute width={11} height={11} />Road distance</span>
          <span className="data-strip__value">{route.route_distance_km ?? 0} km</span>
        </div>
        <div className="data-strip__item">
          <span className="data-strip__label"><IconClock width={11} height={11} />Travel time</span>
          <span className="data-strip__value">{route.route_time_minutes ?? 0} min</span>
        </div>
        <div className="data-strip__item">
          <span className="data-strip__label"><IconPin width={11} height={11} />Stops</span>
          <span className="data-strip__value">{count}</span>
        </div>
        <div className="data-strip__item">
          <span className="data-strip__label"><IconFlag width={11} height={11} />Finish ETA</span>
          <span className="data-strip__value">{route.estimated_finish_time ?? '—'}</span>
        </div>
        <div className="data-strip__item">
          <span className="data-strip__label"><IconClock width={11} height={11} />Avg stop time</span>
          <span className="data-strip__value">{route.average_stop_time ?? '—'} min</span>
        </div>
        <div className="data-strip__item">
          <span className="data-strip__label"><IconGauge width={11} height={11} />Utilization</span>
          <span className="data-strip__value">{route.utilization_percent ?? '—'}%</span>
        </div>
      </div>

      {route.areas && route.areas.length > 0 && (
        <div className="route-header__areas">{route.areas.length} area{route.areas.length === 1 ? '' : 's'}: {route.areas.join(', ')}</div>
      )}

      <button
        type="button"
        className="details-toggle"
        onClick={() => setCollapsed((c) => !c)}
        aria-expanded={!collapsed}
      >
        <IconChevron className={`ticket__chevron${collapsed ? '' : ' ticket__chevron--open'}`} width={14} height={14} />
        {collapsed ? `Show route timeline (${count} stop${count === 1 ? '' : 's'})` : 'Hide route timeline'}
      </button>

      {!collapsed && (
        <div className="route-timeline">
          <div className="timeline-node timeline-node--terminal">
            <span className="timeline-node__dot timeline-node__dot--depot"><IconInbox width={12} height={12} /></span>
            <span className="timeline-node__terminal-label">Warehouse</span>
          </div>

          {route.orders.map((order, stopIdx) => (
            <div
              key={order.order_id}
              className={`timeline-node timeline-node--draggable${String(order.order_id) === String(selectedOrderId) ? ' timeline-node--selected' : ''}`}
              ref={(el) => { nodeRefs.current[order.order_id] = el; }}
              draggable
              title="Drag to reorder this delivery within the route"
              onClick={() => onSelectOrder?.(order.order_id)}
              onDragStart={() => handleDragStart(order.order_id)}
              onDragOver={(e) => e.preventDefault()}
              onDrop={() => handleDrop(order.order_id)}
            >
              <span className={`timeline-node__dot${order.is_late ? ' timeline-node__dot--late' : ''}`}>{stopIdx + 1}</span>
              <div className="timeline-node__body">
                <div className="timeline-node__row">
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
                  <span className="stop__id">#{order.order_id}</span>
                  <span className="timeline-node__name">{order.customer_name}</span>
                  <span className={`stop-status${order.is_late ? ' stop-status--late' : ''}`}>
                    {order.is_late ? 'Late' : 'On time'}
                  </span>
                  <select
                    className="stop-move"
                    value=""
                    title="Move this stop to another route"
                    onChange={(e) => {
                      const target = e.target.value;
                      if (target) onReassignOrder(order.order_id, route.route_name, target);
                    }}
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
                    <option value="pending">Pending / unassigned</option>
                  </select>
                </div>
                <div className="timeline-node__meta">
                  {order.area && <span className="delivery-identity__area">{order.area}</span>}
                  <span>{order.address}</span>
                  <span>Slot {order.delivery_time}</span>
                  {order.eta && <span>ETA {order.eta}</span>}
                  {order.map_link && (
                    <a className="pin-badge" href={order.map_link} target="_blank" rel="noopener noreferrer" onClick={(e) => e.stopPropagation()}>
                      <IconPin width={12} height={12} />
                      View pin
                    </a>
                  )}
                </div>
              </div>
            </div>
          ))}

          <div className="timeline-node timeline-node--terminal">
            <span className="timeline-node__dot timeline-node__dot--end"><IconFlag width={12} height={12} /></span>
            <span className="timeline-node__terminal-label">
              Finish{route.estimated_finish_time ? ` · ${route.estimated_finish_time}` : ''}
            </span>
          </div>
        </div>
      )}
    </div>
  );
}

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

export default function RouteWorkspace({
  routes, pendingOrders, isProcessing, capacityFor, isRouteFull,
  isCreatingRoute, isChangingVehicle, isDeletingRoute,
  onCreateRoute, onToggleVehicle, onDeleteRoute, onReassignOrder, onReorderRoute,
  onAssignOrders, onDownloadRoute,
}) {
  const [tab, setTab] = useState('routes');
  const [selectedRouteName, setSelectedRouteName] = useState(null);
  const [selectedOrderId, setSelectedOrderId] = useState(null);
  const [routeSearch, setRouteSearch] = useState('');

  useEffect(() => {
    if (routes.length === 0) { setSelectedRouteName(null); return; }
    if (!routes.some((r) => r.route_name === selectedRouteName)) {
      setSelectedRouteName(routes[0].route_name);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routes]);

  const selectedRoute = routes.find((r) => r.route_name === selectedRouteName) || null;
  const selectedRouteIdx = selectedRoute ? routes.findIndex((r) => r.route_name === selectedRoute.route_name) : 0;

  const filteredRoutes = useMemo(() => {
    if (!routeSearch.trim()) return routes;
    const q = routeSearch.toLowerCase();
    return routes.filter((r) => (
      r.route_name.toLowerCase().includes(q)
      || (r.areas || []).some((a) => a.toLowerCase().includes(q))
      || r.orders.some((o) => matchesQuery(o, q))
    ));
  }, [routes, routeSearch]);

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
          {isProcessing ? 'Building routes…' : 'No routes yet — generate routes from an upload, or create one.'}
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
      ) : (
        <div className="route-workspace-grid">
          <div className="route-workspace-pane route-workspace-pane--sidebar">
            <div className="search-field search-field--compact">
              <IconSearch width={13} height={13} />
              <input type="text" placeholder="Search routes…" value={routeSearch} onChange={(e) => setRouteSearch(e.target.value)} />
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
            <div className="route-nav-list">
              {filteredRoutes.map((route) => (
                <RouteSidebarCard
                  key={route.route_name}
                  route={route}
                  capacityFor={capacityFor}
                  isSelected={route.route_name === selectedRouteName}
                  onSelect={() => { setSelectedRouteName(route.route_name); setSelectedOrderId(null); }}
                />
              ))}
            </div>
          </div>

          <div className="route-workspace-pane route-workspace-pane--deliveries">
            {selectedRoute && (
              <RouteManifest
                route={selectedRoute}
                routeIdx={selectedRouteIdx}
                routes={routes}
                capacityFor={capacityFor}
                pendingOrders={pendingOrders}
                onToggleVehicle={onToggleVehicle}
                isChangingVehicle={isChangingVehicle}
                onDeleteRoute={onDeleteRoute}
                isDeletingRoute={isDeletingRoute}
                onDownload={onDownloadRoute}
                onReassignOrder={onReassignOrder}
                onAssignOrders={onAssignOrders}
                onReorderRoute={onReorderRoute}
                selectedOrderId={selectedOrderId}
                onSelectOrder={setSelectedOrderId}
              />
            )}
          </div>

          <div className="route-workspace-pane route-workspace-pane--map">
            <RouteMapPanel route={selectedRoute} selectedOrderId={selectedOrderId} onSelectOrder={setSelectedOrderId} />
          </div>
        </div>
      )}
    </div>
  );
}
