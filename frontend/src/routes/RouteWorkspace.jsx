import { useEffect, useMemo, useRef, useState } from 'react';
import {
  IconRoute, IconCar, IconBike, IconClock, IconCheck, IconAlert, IconPin, IconPlus, IconInbox,
  IconDownload, IconRefresh, IconArrowUp, IconArrowDown, IconGauge, IconFlag, IconSearch, IconX,
  IconUsers,
} from '../icons';
import './routeWorkspace.css';

const SEQUENCE_LETTERS = 'ABCDEFGHIJ';
const sequenceLabel = (idx) => SEQUENCE_LETTERS[idx] || String(idx + 1);

// No embedded live map here by design - "View on map" / "Open in Maps"
// deep-links straight to Google Maps with a precise pin (see
// route_service.single_stop_maps_link on the backend) instead. Every
// delivery card and the route header both carry that link.

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

// The shared identity block every delivery card (route list, unassigned
// list, map popup context) uses: Customer -> AREA -> full address, in that
// visual weight, then order id / secondary info last. One component so a
// future hierarchy tweak only changes one place.
function DeliveryIdentityBlock({ order }) {
  return (
    <div className="delivery-identity">
      <span className="delivery-identity__customer">{order.customer_name || 'Unnamed customer'}</span>
      {order.area && <span className="delivery-identity__area">{order.area}</span>}
      <span className="delivery-identity__address">{order.address || 'No address on file'}</span>
      <span className="delivery-identity__meta">
        <span>Order #{order.order_id}</span>
        {order.lat != null && order.lng != null ? (
          <span className="delivery-identity__located"><IconPin width={11} height={11} />Located</span>
        ) : (
          <span className="delivery-identity__unlocated"><IconAlert width={11} height={11} />Needs geocoding</span>
        )}
      </span>
    </div>
  );
}

function DeliveryCard({
  order, sequenceIndex, isSelected, onSelect, onRemove, draggable, onDragStart, onDragOver, onDrop, onMoveUp, onMoveDown, canMoveUp, canMoveDown,
}) {
  const mapLink = order.map_link || '';
  return (
    <div
      className={`delivery-card${isSelected ? ' delivery-card--selected' : ''}${order.is_late ? ' delivery-card--late' : ''}`}
      draggable={draggable}
      onDragStart={onDragStart}
      onDragOver={onDragOver}
      onDrop={onDrop}
      onClick={onSelect}
    >
      <div className="delivery-card__seq" title="Delivery sequence">{sequenceLabel(sequenceIndex)}</div>
      <DeliveryIdentityBlock order={order} />
      <div className="delivery-card__side">
        {order.eta && <span className="delivery-card__eta">ETA {order.eta}{order.is_late && ' · LATE'}</span>}
        {typeof onMoveUp === 'function' && (
          <span className="delivery-card__reorder">
            <button type="button" disabled={!canMoveUp} title="Move earlier" onClick={(e) => { e.stopPropagation(); onMoveUp(); }}>
              <IconArrowUp width={12} height={12} />
            </button>
            <button type="button" disabled={!canMoveDown} title="Move later" onClick={(e) => { e.stopPropagation(); onMoveDown(); }}>
              <IconArrowDown width={12} height={12} />
            </button>
          </span>
        )}
        <div className="delivery-card__actions">
          {mapLink ? (
            <a className="icon-btn" href={mapLink} target="_blank" rel="noopener noreferrer" title="View on map" onClick={(e) => e.stopPropagation()}>
              <IconPin width={13} height={13} />
            </a>
          ) : null}
          {typeof onRemove === 'function' && (
            <button type="button" className="icon-btn icon-btn--danger" title="Remove from route" onClick={(e) => { e.stopPropagation(); onRemove(); }}>
              <IconX width={13} height={13} />
            </button>
          )}
        </div>
      </div>
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

function RouteHeader({
  route, capacityFor, onToggleVehicle, isChangingVehicle, onDeleteRoute, isDeletingRoute, onDownload,
}) {
  const capacity = capacityFor(route.vehicle_type);
  const count = route.orders.length;
  return (
    <div className="route-header">
      <div className="route-header__top">
        <div>
          <h2 className="route-header__title">{route.route_name}</h2>
          <span className={`status-badge status-badge--${route.status === 'manually_edited' ? 'edited' : 'planned'}`}>
            {route.status === 'manually_edited' ? 'Manually edited' : 'Assigned'}
          </span>
        </div>
        <div className="route-header__actions">
          <button className="btn btn--outline" onClick={() => onDownload(route)}>
            <IconDownload width={14} height={14} />
            Download Excel
          </button>
          {route.google_maps_url && (
            <a className="btn btn--outline" href={route.google_maps_url} target="_blank" rel="noopener noreferrer">
              <IconPin width={14} height={14} />
              Open in Maps
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

      <div className="route-header__stats">
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
        <div className="route-header__stat">
          <span className="route-header__stat-label"><IconUsers width={12} height={12} />Deliveries</span>
          <span className="route-header__stat-value mono-num">{capacityText(count, capacity)}</span>
          <CapacityBar count={count} capacity={capacity} />
        </div>
        <div className="route-header__stat">
          <span className="route-header__stat-label"><IconRoute width={12} height={12} />Distance</span>
          <span className="route-header__stat-value mono-num">{route.route_distance_km ?? '—'} km</span>
        </div>
        <div className="route-header__stat">
          <span className="route-header__stat-label"><IconClock width={12} height={12} />Duration</span>
          <span className="route-header__stat-value mono-num">{route.route_time_minutes ?? '—'} min</span>
        </div>
        <div className="route-header__stat">
          <span className="route-header__stat-label"><IconFlag width={12} height={12} />Finish ETA</span>
          <span className="route-header__stat-value mono-num">{route.estimated_finish_time ?? '—'}</span>
        </div>
        <div className="route-header__stat">
          <span className="route-header__stat-label"><IconGauge width={12} height={12} />Utilization</span>
          <span className="route-header__stat-value mono-num">{route.utilization_percent ?? '—'}%</span>
        </div>
      </div>

      {route.areas && route.areas.length > 0 && (
        <div className="route-header__areas">{route.areas.length} area{route.areas.length === 1 ? '' : 's'}: {route.areas.join(', ')}</div>
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
  onCreateRoute, onToggleVehicle, onDeleteRoute, onRemoveFromRoute, onReorderRoute,
  onAssignOrders, onDownloadRoute,
}) {
  const [tab, setTab] = useState('routes');
  const [selectedRouteName, setSelectedRouteName] = useState(null);
  const [selectedOrderId, setSelectedOrderId] = useState(null);
  const [routeSearch, setRouteSearch] = useState('');
  const cardRefs = useRef({});

  useEffect(() => {
    if (routes.length === 0) { setSelectedRouteName(null); return; }
    if (!routes.some((r) => r.route_name === selectedRouteName)) {
      setSelectedRouteName(routes[0].route_name);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routes]);

  const selectedRoute = routes.find((r) => r.route_name === selectedRouteName) || null;

  useEffect(() => {
    if (selectedRoute && selectedOrderId && cardRefs.current[selectedOrderId]) {
      cardRefs.current[selectedOrderId].scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }, [selectedOrderId, selectedRoute]);

  const filteredRoutes = useMemo(() => {
    if (!routeSearch.trim()) return routes;
    const q = routeSearch.toLowerCase();
    return routes.filter((r) => (
      r.route_name.toLowerCase().includes(q)
      || (r.areas || []).some((a) => a.toLowerCase().includes(q))
      || r.orders.some((o) => matchesQuery(o, q))
    ));
  }, [routes, routeSearch]);

  const dragRef = useRef(null);
  const handleDragStart = (orderId) => { dragRef.current = String(orderId); };
  const handleDrop = (targetOrderId) => {
    const draggedId = dragRef.current;
    dragRef.current = null;
    if (!selectedRoute || !draggedId || draggedId === String(targetOrderId)) return;
    const ids = selectedRoute.orders.map((o) => String(o.order_id));
    const fromIdx = ids.indexOf(draggedId);
    const toIdx = ids.indexOf(String(targetOrderId));
    if (fromIdx === -1 || toIdx === -1) return;
    const reordered = [...ids];
    const [moved] = reordered.splice(fromIdx, 1);
    reordered.splice(toIdx, 0, moved);
    onReorderRoute(selectedRoute, reordered);
  };
  const moveStop = (direction) => {
    if (!selectedOrderId || !selectedRoute) return;
    const ids = selectedRoute.orders.map((o) => String(o.order_id));
    const idx = ids.indexOf(String(selectedOrderId));
    const targetIdx = direction === 'up' ? idx - 1 : idx + 1;
    if (idx === -1 || targetIdx < 0 || targetIdx >= ids.length) return;
    const reordered = [...ids];
    [reordered[idx], reordered[targetIdx]] = [reordered[targetIdx], reordered[idx]];
    onReorderRoute(selectedRoute, reordered);
  };

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
              <>
                <RouteHeader
                  route={selectedRoute}
                  capacityFor={capacityFor}
                  onToggleVehicle={onToggleVehicle}
                  isChangingVehicle={isChangingVehicle}
                  onDeleteRoute={onDeleteRoute}
                  isDeletingRoute={isDeletingRoute}
                  onDownload={onDownloadRoute}
                />
                {selectedRoute.orders.length === 0 ? (
                  <div className="empty-state">No deliveries assigned to this route. Add one from Unassigned Orders.</div>
                ) : (
                  <div className="delivery-list">
                    {selectedRoute.orders.map((order, idx) => (
                      <div key={order.order_id} ref={(el) => { cardRefs.current[order.order_id] = el; }}>
                        <DeliveryCard
                          order={order}
                          sequenceIndex={idx}
                          isSelected={String(order.order_id) === String(selectedOrderId)}
                          onSelect={() => setSelectedOrderId(order.order_id)}
                          onRemove={() => {
                            if (window.confirm(`Remove this delivery from ${selectedRoute.route_name} and move it to Unassigned Orders?`)) {
                              onRemoveFromRoute(order.order_id, selectedRoute.route_name);
                            }
                          }}
                          draggable
                          onDragStart={() => handleDragStart(order.order_id)}
                          onDragOver={(e) => e.preventDefault()}
                          onDrop={() => handleDrop(order.order_id)}
                          onMoveUp={() => moveStop('up')}
                          onMoveDown={() => moveStop('down')}
                          canMoveUp={idx > 0}
                          canMoveDown={idx < selectedRoute.orders.length - 1}
                        />
                      </div>
                    ))}
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
