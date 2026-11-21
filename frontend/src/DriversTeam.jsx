import { useMemo, useState } from 'react';
import {
  Eye, EyeOff, Copy, Check, KeyRound, Pencil, Trash2, UserCheck, UserX, UserPlus,
  Phone, Truck, Search, Route as RouteIcon, Users, X,
} from 'lucide-react';
import './driversTeam.css';

// Drivers team page - roster as cards, with the driver's login and (for the
// admin) their password behind an eye toggle, fetched only on reveal.

const PALETTE = ['#6366f1', '#0d9488', '#f59e0b', '#e11d48', '#0284c7', '#7c3aed', '#16a34a', '#db2777'];
function colorFor(key) {
  const s = String(key || '');
  let h = 0;
  for (let i = 0; i < s.length; i += 1) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return PALETTE[h % PALETTE.length];
}
function initialsOf(name) {
  return String(name || '?').trim().split(/\s+/).slice(0, 2).map((p) => p[0]?.toUpperCase() || '').join('') || '?';
}
const STATUS_LABEL = { active: 'Active', pending: 'Awaiting approval', inactive: 'Inactive' };

function CopyButton({ value, label }) {
  const [done, setDone] = useState(false);
  if (!value) return null;
  return (
    <button
      type="button"
      className="dt__icon"
      title={`Copy ${label}`}
      aria-label={`Copy ${label}`}
      onClick={async () => {
        try { await navigator.clipboard.writeText(String(value)); setDone(true); setTimeout(() => setDone(false), 1400); } catch { /* clipboard blocked */ }
      }}
    >
      {done ? <Check size={14} /> : <Copy size={14} />}
    </button>
  );
}

function PasswordField({ driver, fetchPassword, onReset }) {
  const [state, setState] = useState({ shown: false, loading: false, value: null, missing: false, error: false });
  const reveal = async () => {
    if (state.shown) { setState((s) => ({ ...s, shown: false, value: null })); return; }
    setState({ shown: true, loading: true, value: null, missing: false, error: false });
    try {
      const data = await fetchPassword(driver.id);
      setState({ shown: true, loading: false, value: data.password, missing: !data.password, error: false });
    } catch {
      setState({ shown: true, loading: false, value: null, missing: false, error: true });
    }
  };
  return (
    <div className="dt__cred">
      <span className="dt__cred-label">Password</span>
      <div className="dt__cred-row">
        <span className={`dt__cred-value dt__mono${state.shown && state.value ? '' : ' is-masked'}`}>
          {!state.shown ? '••••••••'
            : state.loading ? 'Loading…'
            : state.value ? state.value
            : state.error ? 'Could not load'
            : 'Not saved yet'}
        </span>
        {state.shown && state.value && <CopyButton value={state.value} label="password" />}
        <button type="button" className="dt__icon" onClick={reveal} title={state.shown ? 'Hide password' : 'Show password'} aria-label={state.shown ? 'Hide password' : 'Show password'}>
          {state.shown ? <EyeOff size={14} /> : <Eye size={14} />}
        </button>
      </div>
      {state.shown && state.missing && (
        <p className="dt__cred-note">
          Set before passwords were saved. It appears after the driver's next login, or{' '}
          <button type="button" onClick={onReset}>set a new one</button>.
        </p>
      )}
    </div>
  );
}

export default function DriversTeam({
  drivers, isLoading, error, busyId, fetchPassword,
  onAdd, onEdit, onResetPassword, onToggleStatus, onDelete,
}) {
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState('all');

  const counts = useMemo(() => {
    const c = { all: drivers.length, active: 0, pending: 0, inactive: 0, onRoute: 0 };
    for (const d of drivers) {
      if (c[d.status] != null) c[d.status] += 1;
      if (d.assigned_route_name) c.onRoute += 1;
    }
    return c;
  }, [drivers]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return drivers
      .filter((d) => filter === 'all' || d.status === filter)
      .filter((d) => !q || [d.name, d.username, d.driver_code, d.mobile, d.vehicle_number, d.assigned_route_name]
        .some((v) => String(v || '').toLowerCase().includes(q)))
      .sort((a, b) => (a.status === 'pending' ? -1 : 0) - (b.status === 'pending' ? -1 : 0));
  }, [drivers, query, filter]);

  return (
    <div className="board board--drivers dt" id="drivers-board">
      <header className="dt__head">
        <div className="dt__title">
          <span className="dt__title-icon"><Users size={20} /></span>
          <div>
            <h2>Drivers team</h2>
            <p>Logins for the Driver App. Assign drivers to routes from each route's page. Deactivating signs a driver out everywhere.</p>
          </div>
        </div>
        <button type="button" className="dt__add" onClick={onAdd}>
          <UserPlus size={16} /> Add driver
        </button>
      </header>

      <div className="dt__stats">
        <div className="dt__stat"><b>{counts.all}</b><span>Drivers</span></div>
        <div className="dt__stat dt__stat--good"><b>{counts.active}</b><span>Active</span></div>
        <div className="dt__stat dt__stat--info"><b>{counts.onRoute}</b><span>On a route</span></div>
        <div className={`dt__stat dt__stat--warn${counts.pending ? ' is-hot' : ''}`}><b>{counts.pending}</b><span>Awaiting approval</span></div>
      </div>

      <div className="dt__toolbar">
        <label className="dt__search">
          <Search size={15} />
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search name, login, mobile, vehicle or route" />
          {query && <button type="button" onClick={() => setQuery('')} aria-label="Clear search"><X size={13} /></button>}
        </label>
        <div className="dt__seg" role="group" aria-label="Filter by status">
          {[['all', 'All'], ['active', 'Active'], ['pending', 'Pending'], ['inactive', 'Inactive']].map(([k, l]) => (
            <button key={k} type="button" className={filter === k ? 'is-on' : ''} onClick={() => setFilter(k)}>
              {l} <small>{counts[k]}</small>
            </button>
          ))}
        </div>
      </div>

      {isLoading && !drivers.length ? (
        <div className="dt__grid">{[0, 1, 2].map((i) => <div key={i} className="dt__card dt__card--skeleton" />)}</div>
      ) : error ? (
        <div className="dt__empty dt__empty--error">{error}</div>
      ) : drivers.length === 0 ? (
        <div className="dt__empty">
          <Users size={28} />
          <strong>No drivers yet</strong>
          <span>Click Add driver to create the first Driver App login.</span>
        </div>
      ) : visible.length === 0 ? (
        <div className="dt__empty"><Search size={24} /><strong>No drivers match</strong><span>Clear the search or pick another filter.</span></div>
      ) : (
        <div className="dt__grid">
          {visible.map((d) => {
            const color = colorFor(d.driver_code || d.id);
            const busy = busyId === d.id;
            return (
              <article key={d.id} className={`dt__card dt__card--${d.status}`} style={{ '--dt-c': color }}>
                <div className="dt__card-top">
                  <span className="dt__avatar" style={{ background: color }}>
                    {initialsOf(d.name)}
                    {d.assigned_route_name && <i className="dt__avatar-dot" title="On a route" />}
                  </span>
                  <div className="dt__who">
                    <div className="dt__name">{d.name}</div>
                    <div className="dt__code dt__mono">{d.driver_code}</div>
                  </div>
                  <span className={`dt__pill dt__pill--${d.status}`}>{STATUS_LABEL[d.status] || d.status}</span>
                </div>

                <div className="dt__creds">
                  <div className="dt__cred">
                    <span className="dt__cred-label">Login</span>
                    <div className="dt__cred-row">
                      <span className="dt__cred-value dt__mono">{d.username}</span>
                      <CopyButton value={d.username} label="login" />
                    </div>
                  </div>
                  <PasswordField driver={d} fetchPassword={fetchPassword} onReset={() => onResetPassword(d)} />
                </div>

                <ul className="dt__facts">
                  <li><Phone size={14} />{d.mobile ? <a href={`tel:${d.mobile}`}>{d.mobile}</a> : <span className="dt__muted">No mobile</span>}</li>
                  <li><Truck size={14} />{d.vehicle_number || <span className="dt__muted">No vehicle number</span>}</li>
                  <li><RouteIcon size={14} />{d.assigned_route_name ? <b>{d.assigned_route_name}</b> : <span className="dt__muted">Not assigned</span>}</li>
                </ul>

                <div className="dt__actions">
                  {busy ? (
                    <span className="dt__busy"><span className="spinner" /> Working…</span>
                  ) : d.status === 'pending' ? (
                    <>
                      <button type="button" className="dt__btn dt__btn--primary" onClick={() => onToggleStatus(d)}><UserCheck size={14} /> Approve</button>
                      <button type="button" className="dt__btn dt__btn--danger" onClick={() => onDelete(d)}>Reject</button>
                      <button type="button" className="dt__btn" onClick={() => onEdit(d)}><Pencil size={13} /> Edit</button>
                    </>
                  ) : (
                    <>
                      <button type="button" className="dt__btn" onClick={() => onEdit(d)}><Pencil size={13} /> Edit</button>
                      <button type="button" className="dt__btn" onClick={() => onResetPassword(d)}><KeyRound size={13} /> Password</button>
                      <button type="button" className="dt__btn" onClick={() => onToggleStatus(d)} title={d.status === 'active' ? 'Deactivate (signs them out)' : 'Activate'}>
                        {d.status === 'active' ? <><UserX size={13} /> Deactivate</> : <><UserCheck size={13} /> Activate</>}
                      </button>
                      <button type="button" className="dt__btn dt__btn--icon dt__btn--danger" onClick={() => onDelete(d)} title="Delete driver" aria-label={`Delete ${d.name}`}><Trash2 size={14} /></button>
                    </>
                  )}
                </div>
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}
