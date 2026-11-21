import { useMemo, useState } from 'react';
import { IconX, IconSearch, IconRefresh, IconDownload, IconClock, IconGauge, IconCheck, IconRoute, IconUsers } from './icons';
import './driverData.css';

// Driver Data - every driver's work runs, redesigned as a console:
// KPI strip, filters, a per-driver leaderboard and rich run cards.
// Pure presentation over GET /api/drivers/history; no backend changes.

const RANGES = [
  { key: 'today', label: 'Today', days: 0 },
  { key: '7d', label: '7 days', days: 7 },
  { key: '30d', label: '30 days', days: 30 },
  { key: 'all', label: 'All time', days: null },
];
const STATUSES = [
  { key: 'all', label: 'All' },
  { key: 'in_progress', label: 'Live' },
  { key: 'completed', label: 'Completed' },
];
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
function fmtKm(v) {
  if (v == null || Number.isNaN(Number(v))) return '—';
  const n = Number(v);
  return n >= 100 ? `${Math.round(n)} km` : `${n.toFixed(1)} km`;
}
function fmtDuration(min) {
  if (min == null) return '—';
  const m = Math.round(min);
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`;
}
function fmtDay(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const today = new Date();
  const y = new Date(); y.setDate(today.getDate() - 1);
  if (d.toDateString() === today.toDateString()) return 'Today';
  if (d.toDateString() === y.toDateString()) return 'Yesterday';
  return d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short', year: d.getFullYear() === today.getFullYear() ? undefined : 'numeric' });
}
function fmtTime(iso) {
  return iso ? new Date(iso).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }) : '—';
}
function liveMinutes(run, now) {
  if (run.duration_minutes != null) return run.duration_minutes;
  if (run.route_run_status === 'in_progress' && run.started_at) return (now - new Date(run.started_at).getTime()) / 60000;
  return null;
}
function driverKey(run) {
  return run.driver_id != null ? `id:${run.driver_id}` : `name:${run.driver_name || 'Unknown'}`;
}

function exportCsv(runs) {
  const head = ['Driver', 'Driver code', 'Route', 'Vehicle', 'Status', 'Started', 'Ended', 'Duration (min)', 'Distance (km)', 'Delivered', 'Stops'];
  const esc = (v) => `"${String(v ?? '').replace(/"/g, '""')}"`;
  const rows = runs.map((r) => [
    r.driver_name, r.driver_code, r.route_name, r.vehicle_type, r.route_run_status,
    r.started_at ? new Date(r.started_at).toLocaleString() : '',
    r.completed_at ? new Date(r.completed_at).toLocaleString() : '',
    r.duration_minutes ?? '', r.distance_travelled_km ?? '', r.delivered_count, r.total_stops,
  ].map(esc).join(','));
  const blob = new Blob([[head.map(esc).join(','), ...rows].join('\n')], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `driver-data-${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function Ring({ value, total, color }) {
  const pct = total > 0 ? Math.min(1, value / total) : 0;
  const r = 17;
  const c = 2 * Math.PI * r;
  return (
    <svg className="dd-ring" width="44" height="44" viewBox="0 0 44 44" aria-hidden="true">
      <circle cx="22" cy="22" r={r} fill="none" stroke="var(--rule)" strokeWidth="5" />
      <circle
        cx="22" cy="22" r={r} fill="none" stroke={color} strokeWidth="5" strokeLinecap="round"
        strokeDasharray={`${c * pct} ${c}`} transform="rotate(-90 22 22)"
      />
      <text x="22" y="25.5" textAnchor="middle" fontSize="9.5" fontWeight="700" letterSpacing="-0.3" fill="currentColor">{Math.round(pct * 100)}%</text>
    </svg>
  );
}

export default function DriverDataPanel({ runs, isLoading, error, deletingRunId, onDelete, onRefresh, onClose }) {
  const [query, setQuery] = useState('');
  const [range, setRange] = useState('all');
  const [status, setStatus] = useState('all');
  const [driverFilter, setDriverFilter] = useState(null);
  const [sort, setSort] = useState('recent');
  const now = Date.now();

  const rangeFiltered = useMemo(() => {
    const def = RANGES.find((r) => r.key === range);
    if (!def || def.days == null) return runs;
    const start = new Date();
    start.setHours(0, 0, 0, 0);
    start.setDate(start.getDate() - def.days);
    return runs.filter((r) => r.started_at && new Date(r.started_at) >= start);
  }, [runs, range]);

  const drivers = useMemo(() => {
    const map = new Map();
    for (const r of rangeFiltered) {
      const k = driverKey(r);
      const d = map.get(k) || { key: k, name: r.driver_name || 'Unknown driver', code: r.driver_code, runs: 0, km: 0, delivered: 0, stops: 0, minutes: 0, live: false };
      d.runs += 1;
      d.km += Number(r.distance_travelled_km) || 0;
      d.delivered += r.delivered_count || 0;
      d.stops += r.total_stops || 0;
      d.minutes += r.duration_minutes || 0;
      if (r.route_run_status === 'in_progress') d.live = true;
      map.set(k, d);
    }
    return [...map.values()].sort((a, b) => b.km - a.km);
  }, [rangeFiltered]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    let list = rangeFiltered.filter((r) => {
      if (status !== 'all' && r.route_run_status !== status) return false;
      if (driverFilter && driverKey(r) !== driverFilter) return false;
      if (!q) return true;
      return [r.driver_name, r.driver_code, r.route_name].some((v) => String(v || '').toLowerCase().includes(q));
    });
    if (sort === 'distance') list = [...list].sort((a, b) => (Number(b.distance_travelled_km) || 0) - (Number(a.distance_travelled_km) || 0));
    else if (sort === 'duration') list = [...list].sort((a, b) => (liveMinutes(b, now) || 0) - (liveMinutes(a, now) || 0));
    return list;
  }, [rangeFiltered, query, status, driverFilter, sort, now]);

  const totals = useMemo(() => {
    const t = { runs: visible.length, km: 0, delivered: 0, stops: 0, minutes: 0, timed: 0, live: 0 };
    for (const r of visible) {
      t.km += Number(r.distance_travelled_km) || 0;
      t.delivered += r.delivered_count || 0;
      t.stops += r.total_stops || 0;
      if (r.duration_minutes != null) { t.minutes += r.duration_minutes; t.timed += 1; }
      if (r.route_run_status === 'in_progress') t.live += 1;
    }
    return t;
  }, [visible]);

  const groups = useMemo(() => {
    if (sort !== 'recent') return [{ day: null, items: visible }];
    const out = [];
    for (const r of visible) {
      const day = fmtDay(r.started_at);
      const last = out[out.length - 1];
      if (last && last.day === day) last.items.push(r);
      else out.push({ day, items: [r] });
    }
    return out;
  }, [visible, sort]);

  const maxKm = Math.max(1, ...drivers.map((d) => d.km));
  const successRate = totals.stops > 0 ? Math.round((totals.delivered / totals.stops) * 100) : null;

  return (
    <div className="history-overlay dd" role="dialog" aria-modal="true" aria-label="Driver Data">
      <div className="history-overlay__scrim" onClick={onClose} />
      <div className="dd__panel">
        <header className="dd__head">
          <div className="dd__title">
            <span className="dd__title-icon"><IconUsers width={20} height={20} /></span>
            <div>
              <h2>Driver Data</h2>
              <p>Every run from Start Route to End Route, with GPS distance, time and deliveries.</p>
            </div>
          </div>
          <div className="dd__head-actions">
            <button type="button" className="dd__icon-btn" onClick={onRefresh} disabled={isLoading} title="Refresh">
              <IconRefresh width={16} height={16} className={isLoading ? 'dd__spin' : ''} />
            </button>
            <button type="button" className="dd__btn" onClick={() => exportCsv(visible)} disabled={!visible.length}>
              <IconDownload width={15} height={15} /> Export CSV
            </button>
            <button type="button" className="dd__icon-btn" onClick={onClose} aria-label="Close Driver Data">
              <IconX width={18} height={18} />
            </button>
          </div>
        </header>

        <section className="dd__kpis">
          <div className="dd__kpi dd__kpi--hero">
            <span className="dd__kpi-label"><IconGauge width={14} height={14} /> Distance driven</span>
            <span className="dd__kpi-value">{fmtKm(totals.km)}</span>
            <span className="dd__kpi-sub">{totals.runs ? `${fmtKm(totals.km / totals.runs)} per run` : 'No runs in view'}</span>
          </div>
          <div className="dd__kpi">
            <span className="dd__kpi-label"><IconRoute width={14} height={14} /> Runs</span>
            <span className="dd__kpi-value">{totals.runs}</span>
            <span className="dd__kpi-sub">{totals.live > 0 ? <><i className="dd__live-dot" /> {totals.live} live now</> : 'None live'}</span>
          </div>
          <div className="dd__kpi">
            <span className="dd__kpi-label"><IconCheck width={14} height={14} /> Delivered</span>
            <span className="dd__kpi-value">{totals.delivered}<small>/{totals.stops}</small></span>
            <span className="dd__kpi-sub">{successRate != null ? `${successRate}% of stops` : '—'}</span>
          </div>
          <div className="dd__kpi">
            <span className="dd__kpi-label"><IconClock width={14} height={14} /> Time on road</span>
            <span className="dd__kpi-value">{fmtDuration(totals.minutes)}</span>
            <span className="dd__kpi-sub">{totals.timed ? `${fmtDuration(totals.minutes / totals.timed)} avg run` : '—'}</span>
          </div>
        </section>

        <section className="dd__toolbar">
          <label className="dd__search">
            <IconSearch width={15} height={15} />
            <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search driver, code or route" />
            {query && <button type="button" onClick={() => setQuery('')} aria-label="Clear search"><IconX width={13} height={13} /></button>}
          </label>
          <div className="dd__seg" role="group" aria-label="Date range">
            {RANGES.map((r) => (
              <button key={r.key} type="button" className={range === r.key ? 'is-on' : ''} onClick={() => setRange(r.key)}>{r.label}</button>
            ))}
          </div>
          <div className="dd__seg" role="group" aria-label="Status">
            {STATUSES.map((s) => (
              <button key={s.key} type="button" className={status === s.key ? 'is-on' : ''} onClick={() => setStatus(s.key)}>{s.label}</button>
            ))}
          </div>
          <select className="dd__select" value={sort} onChange={(e) => setSort(e.target.value)} aria-label="Sort runs">
            <option value="recent">Most recent</option>
            <option value="distance">Longest distance</option>
            <option value="duration">Longest time</option>
          </select>
        </section>

        <div className="dd__body">
          {isLoading && !runs.length ? (
            <div className="dd__skeletons">{[0, 1, 2, 3].map((i) => <div key={i} className="dd__skeleton" />)}</div>
          ) : error ? (
            <div className="dd__empty dd__empty--error">{error}</div>
          ) : runs.length === 0 ? (
            <div className="dd__empty">
              <IconRoute width={28} height={28} />
              <strong>No driver runs yet</strong>
              <span>When a driver taps Start Route in the Driver App, the run shows up here.</span>
            </div>
          ) : (
            <div className="dd__grid">
              <aside className="dd__board">
                <div className="dd__board-head">
                  <h3>Drivers</h3>
                  {driverFilter && <button type="button" onClick={() => setDriverFilter(null)}>Show all</button>}
                </div>
                {drivers.length === 0 ? (
                  <div className="dd__board-empty">No runs in this period.</div>
                ) : drivers.map((d, i) => {
                  const color = colorFor(d.key);
                  const on = driverFilter === d.key;
                  return (
                    <button
                      key={d.key}
                      type="button"
                      className={`dd__driver${on ? ' is-on' : ''}${driverFilter && !on ? ' is-dim' : ''}`}
                      onClick={() => setDriverFilter(on ? null : d.key)}
                    >
                      <span className="dd__rank">{i + 1}</span>
                      <span className="dd__avatar" style={{ background: color }}>
                        {initialsOf(d.name)}
                        {d.live && <i className="dd__avatar-live" />}
                      </span>
                      <span className="dd__driver-body">
                        <span className="dd__driver-name">{d.name}</span>
                        <span className="dd__driver-meta">{d.runs} run{d.runs === 1 ? '' : 's'} · {d.delivered}/{d.stops} delivered</span>
                        <span className="dd__bar"><span style={{ width: `${(d.km / maxKm) * 100}%`, background: color }} /></span>
                      </span>
                      <span className="dd__driver-km">{fmtKm(d.km)}</span>
                    </button>
                  );
                })}
              </aside>

              <main className="dd__runs">
                {visible.length === 0 ? (
                  <div className="dd__empty">
                    <IconSearch width={24} height={24} />
                    <strong>No runs match</strong>
                    <span>Try a wider date range or clear the filters.</span>
                  </div>
                ) : groups.map((g) => (
                  <div key={g.day || 'all'} className="dd__day">
                    {g.day && (
                      <div className="dd__day-head">
                        <span>{g.day}</span>
                        <span>{g.items.length} run{g.items.length === 1 ? '' : 's'} · {fmtKm(g.items.reduce((s, r) => s + (Number(r.distance_travelled_km) || 0), 0))}</span>
                      </div>
                    )}
                    {g.items.map((run) => {
                      const live = run.route_run_status === 'in_progress';
                      const color = colorFor(driverKey(run));
                      const mins = liveMinutes(run, now);
                      const allDone = run.total_stops > 0 && run.delivered_count >= run.total_stops;
                      return (
                        <article key={run.route_id} className={`dd__run${live ? ' is-live' : ''}`} style={{ '--dd-c': color }}>
                          <div className="dd__run-who">
                            <span className="dd__avatar dd__avatar--sm" style={{ background: color }}>{initialsOf(run.driver_name)}</span>
                            <div>
                              <div className="dd__run-driver">
                                {run.driver_name || 'Unknown driver'}
                                {run.driver_code && <span className="dd__code">{run.driver_code}</span>}
                              </div>
                              <div className="dd__run-route">
                                {run.route_name}
                                {run.vehicle_type && <span className="dd__vehicle">{run.vehicle_type}</span>}
                              </div>
                            </div>
                          </div>

                          <div className="dd__run-time">
                            <div className="dd__tl">
                              <span className="dd__tl-dot" />
                              <span className="dd__tl-line" />
                              <span className={`dd__tl-dot${live ? ' dd__tl-dot--live' : ' dd__tl-dot--end'}`} />
                            </div>
                            <div className="dd__tl-labels">
                              <span><b>{fmtTime(run.started_at)}</b> start</span>
                              <span>{live ? <b className="dd__live-text">On the road</b> : <><b>{fmtTime(run.completed_at)}</b> end</>}</span>
                            </div>
                          </div>

                          <div className="dd__run-stats">
                            <div><span>Distance</span><b>{fmtKm(run.distance_travelled_km)}</b></div>
                            <div><span>{live ? 'Elapsed' : 'Duration'}</span><b>{fmtDuration(mins)}</b></div>
                          </div>

                          <div className="dd__run-ring" title={`${run.delivered_count} of ${run.total_stops} stops delivered`}>
                            <Ring value={run.delivered_count} total={run.total_stops} color={allDone ? 'var(--good)' : color} />
                            <span>{run.delivered_count}/{run.total_stops}<br />delivered</span>
                          </div>

                          <div className="dd__run-end">
                            <span className={`dd__status dd__status--${live ? 'live' : 'done'}`}>
                              {live ? <><i className="dd__live-dot" /> Live</> : <><IconCheck width={12} height={12} /> Completed</>}
                            </span>
                            <button
                              type="button"
                              className="dd__del"
                              disabled={deletingRunId === run.route_id}
                              title={live ? 'Delete this stuck or in-progress run (asks to confirm)' : 'Delete this run record'}
                              onClick={() => onDelete(run)}
                            >
                              {deletingRunId === run.route_id ? <span className="spinner" /> : 'Delete'}
                            </button>
                          </div>
                        </article>
                      );
                    })}
                  </div>
                ))}
              </main>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
