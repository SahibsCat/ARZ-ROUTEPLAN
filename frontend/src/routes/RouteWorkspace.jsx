import { useEffect, useMemo, useRef, useState } from 'react';
import {
  IconRoute, IconCar, IconBike, IconClock, IconCheck, IconAlert, IconPin, IconPlus, IconInbox,
  IconDownload, IconRefresh, IconArrowUp, IconArrowDown, IconGauge, IconFlag, IconSearch, IconX,
  IconUsers, IconChevron,
} from '../icons';
import './routeWorkspace.css';

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

// This app has no driver roster, no live GPS, and no route-execution state
// machine (start/pause/complete) - a route is either empty, has room, is at
// capacity, or has late deliveries. Every "status" concept in this
// workspace is derived from those four real signals, not invented ones.
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
// Routes List — summary strip, filter bar, wide route table.
// --------------------------------------------------------------------------

function RoutesSummaryStrip({ routes, pendingOrders, capacityFor }) {
  const counts = useMemo(() => {
    const acc = { total: routes.length, open: 0, full: 0, delayed: 0 };
    routes.forEach((route) => {
      const status = routeStatus(route, capacityFor(route.vehicle_type));
      if (status === 'open') acc.open += 1;
      else if (status === 'full') acc.full += 1;
      else if (status === 'delayed') acc.delayed += 1;
    });
    return acc;
  }, [routes, capacityFor]);

  const items = [
    { label: 'Total routes', value: counts.total },
    { label: 'Open', value: counts.open, tone: 'open' },
    { label: 'Full', value: counts.full, tone: 'full' },
    { label: 'Delayed', value: counts.delayed, tone: 'delayed' },
    { label: 'Unassigned orders', value: pendingOrders.length, tone: 'unassigned' },
  ];

  return (
    <div className="routes-summary">
      {items.map((item) => (
        <div key={item.label} className="routes-summary__item">
          <span className={`routes-summary__value mono-num${item.tone ? ` routes-summary__value--${item.tone}` : ''}`}>{item.value}</span>
          <span className="routes-summary__label">{item.label}</span>
        </div>
      ))}
    </div>
  );
}

function RoutesFilterBar({
  search, onSearchChange, vehicleFilter, onVehicleFilterChange, statusFilter, onStatusFilterChange, onClear, hasActiveFilters,
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

function RouteRow({ route, capacityFor, onOpen, onDownload, onDeleteRoute, isDeletingRoute }) {
  const capacity = capacityFor(route.vehicle_type);
  const count = route.orders.length;
  const status = routeStatus(route, capacity);
  const subtitle = route.areas && route.areas.length ? route.areas.slice(0, 3).join(' → ') : 'No deliveries yet';

  return (
    <div className="route-row" role="button" tabIndex={0} onClick={onOpen} onKeyDown={(e) => { if (e.key === 'Enter') onOpen(); }}>
      <div className="route-row__cell route-row__cell--route">
        <span className="route-row__name">{route.route_name}</span>
        <span className="route-row__subtitle">{subtitle}</span>
      </div>
      <div className="route-row__cell route-row__cell--vehicle">
        <span className="vehicle-pill">
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
        {routes.map((route) => (
          <RouteRow
            key={route.route_name}
            route={route}
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

function RouteOverviewStrip({ route, capacity }) {
  const count = route.orders.length;
  const stats = [
    { label: 'Vehicle', value: route.vehicle_type === 'car' ? 'Car' : 'Bike' },
    { label: 'Stops', value: `${count} / ${capacity}` },
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

function RouteDetail({
  route, routeIdx, routes, capacityFor, pendingOrders,
  onBack, onToggleVehicle, isChangingVehicle, onDeleteRoute, isDeletingRoute, onDownload,
  onReassignOrder, onAssignOrders, onReorderRoute,
  selectedOrderId, onSelectOrder,
}) {
  const capacity = capacityFor(route.vehicle_type);
  const count = route.orders.length;
  const isFull = count >= capacity;
  const isEdited = route.status === 'manually_edited';
  const status = routeStatus(route, capacity);
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

      <RouteOverviewStrip route={route} capacity={capacity} />

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
                className={`route-stop${String(order.order_id) === String(selectedOrderId) ? ' route-stop--selected' : ''}`}
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
  routes, pendingOrders, isProcessing, capacityFor, isRouteFull,
  isCreatingRoute, isChangingVehicle, isDeletingRoute,
  onCreateRoute, onToggleVehicle, onDeleteRoute, onReassignOrder, onReorderRoute,
  onAssignOrders, onDownloadRoute,
}) {
  const [tab, setTab] = useState('routes');
  const [view, setView] = useState('list'); // 'list' | 'detail'
  const [selectedRouteName, setSelectedRouteName] = useState(null);
  const [selectedOrderId, setSelectedOrderId] = useState(null);
  const [routeSearch, setRouteSearch] = useState('');
  const [vehicleFilter, setVehicleFilter] = useState('all');
  const [statusFilter, setStatusFilter] = useState('all');

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

  const hasActiveFilters = Boolean(routeSearch.trim()) || vehicleFilter !== 'all' || statusFilter !== 'all';
  const clearFilters = () => { setRouteSearch(''); setVehicleFilter('all'); setStatusFilter('all'); };

  const filteredRoutes = useMemo(() => {
    let result = routes;
    if (vehicleFilter !== 'all') result = result.filter((r) => r.vehicle_type === vehicleFilter);
    if (statusFilter !== 'all') result = result.filter((r) => routeStatus(r, capacityFor(r.vehicle_type)) === statusFilter);
    if (routeSearch.trim()) {
      const q = routeSearch.toLowerCase();
      result = result.filter((r) => (
        r.route_name.toLowerCase().includes(q)
        || (r.areas || []).some((a) => a.toLowerCase().includes(q))
        || r.orders.some((o) => matchesQuery(o, q))
      ));
    }
    return result;
  }, [routes, routeSearch, vehicleFilter, statusFilter, capacityFor]);

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
          selectedOrderId={selectedOrderId}
          onSelectOrder={setSelectedOrderId}
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

          <RoutesSummaryStrip routes={routes} pendingOrders={pendingOrders} capacityFor={capacityFor} />

          <RoutesFilterBar
            search={routeSearch}
            onSearchChange={setRouteSearch}
            vehicleFilter={vehicleFilter}
            onVehicleFilterChange={setVehicleFilter}
            statusFilter={statusFilter}
            onStatusFilterChange={setStatusFilter}
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
