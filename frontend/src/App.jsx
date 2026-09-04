import { useState, useEffect, useRef, Fragment } from 'react';
import RouteWorkspace, { AdjustLocationModal } from './routes/RouteWorkspace';
import './App.css';
// Reuses RouteWorkspace's .modal / .modal-backdrop styles (Add Address
// modal) for the Drivers panel's create/edit/reset-password forms below,
// rather than duplicating that CSS - importing a stylesheet twice is a
// no-op, not a double-apply.
import './routes/routeWorkspace.css';
import arzLogo from './assets/arz-logo.png';
import arzLogoDark from './assets/arz-logo-dark.png';
import {
  IconRoute, IconCar, IconBike, IconClock, IconCheck, IconAlert, IconPin, IconPlus, IconInbox,
  IconDownload, IconRefresh, IconChevron, IconArrowUp, IconArrowDown, IconGauge, IconFlag,
  IconSearch, IconBell, IconMenu, IconX, IconLayoutGrid, IconUpload, IconUsers, IconHistory,
  IconBarChart, IconFileText, IconSettings, IconSun, IconMoon,
} from './icons';

// Backend origin for deployments where frontend and backend aren't
// same-origin (e.g. Render, where they're two separate services). Empty
// string preserves today's same-origin/dev-proxy behavior unless this is
// set at build time - see apiFetch below.
const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/$/, '');

// Mirrors BIKE_CAPACITY / CAR_CAPACITY in backend/app/route_service.py - keep in sync.
const BIKE_CAPACITY = 3;
const CAR_CAPACITY = 6;
const capacityFor = (vehicleType) => (vehicleType === 'car' ? CAR_CAPACITY : BIKE_CAPACITY);
const isRouteFull = (route) => route.orders.length >= capacityFor(route.vehicle_type);

// The admin's one-time "Add Address from Another Route" override - exactly
// one delivery point past normal capacity, for either vehicle type. Mirrors
// MANUAL_OVERRIDE_EXTRA / CAR_MAX_CAPACITY / BIKE_MAX_CAPACITY in
// route_service.py - keep in sync.
const MANUAL_OVERRIDE_EXTRA = 1;
const CAR_MAX_CAPACITY = CAR_CAPACITY + MANUAL_OVERRIDE_EXTRA;
const BIKE_MAX_CAPACITY = BIKE_CAPACITY + MANUAL_OVERRIDE_EXTRA;
const maxCapacityFor = (vehicleType) => (vehicleType === 'car' ? CAR_MAX_CAPACITY : BIKE_MAX_CAPACITY);

// A small, fixed palette so each route reads as a distinct color at a
// glance - cycles if there are more routes than colors.
const ROUTE_HUES = 6;

// Sidebar structure - grouped by what you're actually trying to do, not by
// "is this real yet" (that used to split a plain Workspace/Platform-Soon
// list, which put Vehicles nowhere near Drivers and Live Tracking nowhere
// near Routes). Dashboard/Routes/Unassigned Orders/Failed Addresses/Drivers
// are all one continuous page again - clicking one of these just smooth-
// scrolls to its anchor rather than swapping the console's content out.
// Drivers used to be a right-side overlay panel; it's a full board in the
// page flow now, same as Failed Addresses, so both creating a driver and
// seeing every driver's status live on a real board instead of a drawer
// you dip into and dismiss. 'history' (Route History) and 'driver-data'
// (every driver's past work runs - start/end time, km travelled, with a
// delete option) are both long append-only record lists rather than
// something you work in, so they're the nav items that open as an overlay
// instead. Items marked soon:true have no real content to scroll to, so
// clicking one opens ComingSoonPage as its own small overlay instead (see
// soonOverlay).
const NAV_GROUPS = [
  {
    label: 'Overview',
    items: [
      { key: 'dashboard', label: 'Dashboard', icon: IconLayoutGrid, anchor: 'top' },
    ],
  },
  {
    label: 'Dispatch',
    items: [
      { key: 'generate', label: 'Routes', icon: IconRoute, anchor: 'toolbar-section' },
      { key: 'unassigned', label: 'Unassigned Orders', icon: IconInbox, anchor: 'unassigned-board' },
      { key: 'failed', label: 'Failed Addresses', icon: IconAlert, anchor: 'returns-board' },
      { key: 'history', label: 'Route History', icon: IconHistory },
      { key: 'live-tracking', label: 'Live Tracking', icon: IconGauge, anchor: 'unassigned-board' },
    ],
  },
  {
    label: 'Fleet',
    items: [
      { key: 'drivers', label: 'Drivers', icon: IconUsers, anchor: 'drivers-board' },
      { key: 'driver-data', label: 'Driver Data', icon: IconClock },
      { key: 'vehicles', label: 'Vehicles', icon: IconCar, soon: true },
    ],
  },
  {
    label: 'Insights',
    items: [
      { key: 'reports', label: 'Reports', icon: IconFileText, soon: true },
      { key: 'analytics', label: 'Analytics', icon: IconBarChart, soon: true },
    ],
  },
  {
    label: 'System',
    items: [
      { key: 'notifications', label: 'Notifications', icon: IconBell, soon: true },
      { key: 'settings', label: 'Settings', icon: IconSettings, soon: true },
    ],
  },
];
// Flattened views - most of the app just needs "every item" or "every soon
// item" without caring which group it's in.
const NAV_ITEMS = NAV_GROUPS.flatMap((g) => g.items);
const NAV_ITEMS_SOON = NAV_ITEMS.filter((item) => item.soon);
const findNavItem = (key) => NAV_ITEMS.find((item) => item.key === key);
const SOON_BLURBS = {
  vehicles: 'Individual vehicle records - plate number, type, and which driver each one belongs to - not just the car/bike counts on the Routes page. Needs its own backend table.',
  notifications: 'A persistent inbox for alerts like route-started - today those are toasts that vanish once you dismiss them or leave the page.',
  reports: 'Delivery performance over time - on-time rate, distance, driver totals - once there is enough saved route history to report on honestly.',
  analytics: 'Trend and pattern analysis across saved route plans.',
  settings: 'Route capacity defaults and notification preferences as real, saved configuration instead of only what is set per-session in Generate Routes.',
};

// Counts up/down to a new value instead of snapping - used on the KPI tiles.
function useCountUp(value, duration = 500) {
  const [display, setDisplay] = useState(value);
  const prevRef = useRef(value);
  useEffect(() => {
    const from = prevRef.current;
    const to = value;
    if (from === to) return undefined;
    const start = performance.now();
    let raf;
    const tick = (now) => {
      const t = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - t, 3);
      setDisplay(Math.round(from + (to - from) * eased));
      if (t < 1) {
        raf = requestAnimationFrame(tick);
      } else {
        prevRef.current = to;
      }
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [value, duration]);
  return display;
}

// Placeholder shimmer card shown the first time a board is populating,
// so the page reads as "working" rather than blank while data loads.
function SkeletonCard() {
  return (
    <div className="skeleton-card" aria-hidden="true">
      <div className="skeleton-line skeleton-line--title" />
      <div className="skeleton-line" />
      <div className="skeleton-line skeleton-line--short" />
    </div>
  );
}

// A real, standalone page for every sidebar item that doesn't have a
// backend behind it yet - not a toast that vanishes and leaves you on
// whatever page you were already on. Says plainly what's missing instead
// of showing fabricated numbers, and gives a way back to a working page.
// Shown inside a small overlay (see soonOverlay in App) for any sidebar
// item that doesn't have a backend behind it yet - not a toast that
// vanishes and leaves you guessing, and not its own full page either now
// that everything real lives on one page again. Says plainly what's
// missing instead of showing fabricated numbers.
function ComingSoonPage({ label, icon: Icon, blurb, onClose }) {
  return (
    <div className="coming-soon-page">
      <button type="button" className="coming-soon-page__close" onClick={onClose} aria-label="Close">
        <IconX width={16} height={16} />
      </button>
      <div className="coming-soon-page__icon"><Icon width={26} height={26} /></div>
      <h2>{label}</h2>
      <p>{blurb}</p>
      <span className="coming-soon-page__tag">Not built yet - no backend data behind it</span>
    </div>
  );
}

// A plain div when there's nowhere useful to send a click, a real button
// (keyboard-reachable, no default day-over-day trend claimed since the
// backend doesn't track yesterday's numbers) when there is.
function KpiTile({ variant, icon: Icon, value, label, suffix, onClick }) {
  const display = useCountUp(value);
  const Tag = onClick ? 'button' : 'div';
  return (
    <Tag
      type={onClick ? 'button' : undefined}
      className={`kpi-tile kpi-tile--${variant}${onClick ? ' kpi-tile--clickable' : ''}`}
      onClick={onClick}
    >
      <span className="kpi-tile__icon"><Icon width={16} height={16} /></span>
      <div>
        <span className="kpi-tile__value mono-num">
          {display}{suffix && <span className="kpi-tile__suffix">{suffix}</span>}
        </span>
        <span className="kpi-tile__label">{label}</span>
      </div>
    </Tag>
  );
}

// Only ever mounted when there's something in it (App.jsx checks total
// === 0 before rendering) - three real, already-tracked signals rolled
// into one glanceable list instead of three separate boards/alerts the
// dispatcher has to notice on their own. Deliberately doesn't invent a
// "missing data" bucket (no phone/delivery-window field exists on Order
// yet) - only surfaces what the app actually knows about.
function IssuesPanel({ failedOrders, warnings, pendingOrders, isOpen, onToggle, onRetry, onJump, describeErrorDetail }) {
  const total = failedOrders.length + warnings.length + pendingOrders.length;
  if (total === 0) return null;
  const parts = [];
  if (failedOrders.length) parts.push(`${failedOrders.length} failed address${failedOrders.length === 1 ? '' : 'es'}`);
  if (warnings.length) parts.push(`${warnings.length} fleet warning${warnings.length === 1 ? '' : 's'}`);
  if (pendingOrders.length) parts.push(`${pendingOrders.length} unassigned order${pendingOrders.length === 1 ? '' : 's'}`);

  return (
    <div className="issues-panel">
      <button type="button" className="issues-panel__header" onClick={onToggle} aria-expanded={isOpen}>
        <span className="issues-panel__icon"><IconAlert width={15} height={15} /></span>
        <span className="issues-panel__title-group">
          <span className="issues-panel__title">Issues requiring attention · {total}</span>
          <span className="issues-panel__sub">{parts.join(' · ')}</span>
        </span>
        <IconChevron
          width={15}
          height={15}
          className={`issues-panel__chevron${isOpen ? ' issues-panel__chevron--open' : ''}`}
        />
      </button>
      {isOpen && (
        <div className="issues-panel__body">
          {failedOrders.length > 0 && (
            <div className="issue-group">
              <div className="issue-group__label">Failed addresses</div>
              {failedOrders.slice(0, 5).map((o) => (
                <div className="issue-row" key={`issue-fa-${o.order_id}`}>
                  <span className="issue-row__dot" />
                  <span className="issue-row__text">
                    Order #{o.order_id} — {describeErrorDetail(o.geocode_error, 'address could not be geocoded')}
                  </span>
                  <div className="issue-row__actions">
                    <button type="button" className="btn btn--outline btn--compact" onClick={() => onRetry(o.order_id)}>Retry</button>
                    <button type="button" className="btn btn--ghost btn--compact" onClick={() => onJump('failed', o.order_id)}>View</button>
                  </div>
                </div>
              ))}
              {failedOrders.length > 5 && (
                <button type="button" className="issue-group__more" onClick={() => onJump('failed')}>
                  +{failedOrders.length - 5} more — see Failed Addresses
                </button>
              )}
            </div>
          )}
          {warnings.length > 0 && (
            <div className="issue-group">
              <div className="issue-group__label">Capacity &amp; fleet warnings</div>
              {warnings.map((w, idx) => (
                <div className="issue-row" key={`issue-wn-${idx}`}>
                  <span className="issue-row__dot" />
                  <span className="issue-row__text">{describeErrorDetail(w, 'Needs attention.')}</span>
                  <div className="issue-row__actions">
                    <button type="button" className="btn btn--ghost btn--compact" onClick={() => onJump('generate')}>View</button>
                  </div>
                </div>
              ))}
            </div>
          )}
          {pendingOrders.length > 0 && (
            <div className="issue-group">
              <div className="issue-group__label">Unassigned orders</div>
              {pendingOrders.slice(0, 5).map((o) => (
                <div className="issue-row" key={`issue-pd-${o.order_id}`}>
                  <span className="issue-row__dot" />
                  <span className="issue-row__text">Order #{o.order_id} — {o.address || 'no route has capacity'}</span>
                  <div className="issue-row__actions">
                    <button type="button" className="btn btn--outline btn--compact" onClick={() => onJump('unassigned')}>Assign</button>
                  </div>
                </div>
              ))}
              {pendingOrders.length > 5 && (
                <button type="button" className="issue-group__more" onClick={() => onJump('unassigned')}>
                  +{pendingOrders.length - 5} more — see Unassigned Orders
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ⌘K / Ctrl+K overlay. Deliberately doesn't replace the topbar's existing
// live-filter search (searchTerm/matchesSearch, wired into three boards
// already) - this is a separate, additive entry point for jumping
// somewhere or running an action, not another way to filter the same
// list. Every item here calls a handler App() already had for the same
// action elsewhere (dash-links, quick actions, sidebar nav) - the
// palette is a faster way to reach them, not new behavior.
function CommandPalette({ open, query, onQueryChange, onClose, groups }) {
  if (!open) return null;
  const hasResults = groups.some((g) => g.items.length > 0);
  return (
    <div className="modal-backdrop command-palette__backdrop" onClick={onClose}>
      <div className="command-palette" onClick={(e) => e.stopPropagation()}>
        <div className="command-palette__input-wrap">
          <IconSearch width={16} height={16} />
          <input
            autoFocus
            className="command-palette__input"
            placeholder="Search orders, routes, drivers, actions…"
            value={query}
            onChange={(e) => onQueryChange(e.target.value)}
          />
          <button type="button" className="command-palette__close" onClick={onClose} aria-label="Close">
            <IconX width={14} height={14} />
          </button>
        </div>
        <div className="command-palette__list">
          {hasResults ? groups.map((g) => (g.items.length === 0 ? null : (
            <div key={g.label}>
              <div className="command-palette__group">{g.label}</div>
              {g.items.map((item) => (
                <button
                  type="button"
                  key={item.id}
                  className="command-palette__item"
                  disabled={item.disabled}
                  onClick={() => { onClose(); item.onRun(); }}
                >
                  <span className="command-palette__item-icon"><item.icon width={14} height={14} /></span>
                  <span>
                    <span className="command-palette__item-label">{item.label}</span>
                    {item.sub && <span className="command-palette__item-sub">{item.sub}</span>}
                  </span>
                </button>
              ))}
            </div>
          ))) : (
            <div className="command-palette__empty">No matches for "{query}"</div>
          )}
        </div>
        <div className="command-palette__foot">
          <span className="command-palette__hint"><kbd>↵</kbd> select</span>
          <span className="command-palette__hint"><kbd>esc</kbd> close</span>
        </div>
      </div>
    </div>
  );
}

function DriverStatusPill({ status }) {
  return (
    <span className={`driver-status-pill driver-status-pill--${status}`}>
      {status === 'active' ? '🟢 Active' : '⚪ Inactive'}
    </span>
  );
}

// One-time credential reveal shown after a successful create/reset - the
// backend only ever stores a password hash, so this screen (copyable,
// staying open until the admin explicitly dismisses it) is the only chance
// to see the plaintext password again. Replaces the toast-only confirmation
// that let a generated password disappear the instant the modal closed.
function CredentialRevealPanel({ title, note, username, password, onDone }) {
  const [copied, setCopied] = useState(false);
  const copyText = username ? `${username} / ${password}` : password;
  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(copyText);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      // Clipboard permission can be denied - the value is still selectable
      // text on screen, so this isn't a dead end, just no one-click copy.
    }
  };
  return (
    <div className="modal-backdrop">
      <div className="modal">
        <div className="modal__header"><h3>{title}</h3></div>
        <div className="modal__body">
          <p className="modal__hint">{note}</p>
          {username && (
            <div className="credential-row">
              <span className="credential-row__label">Username</span>
              <span className="credential-row__value mono-num">{username}</span>
            </div>
          )}
          <div className="credential-row">
            <span className="credential-row__label">Password</span>
            <span className="credential-row__value mono-num">{password}</span>
          </div>
          <button type="button" className="btn btn--outline" onClick={handleCopy}>
            {copied ? 'Copied!' : 'Copy to clipboard'}
          </button>
        </div>
        <div className="modal__footer">
          <button type="button" className="btn btn--primary" onClick={onDone}>Done</button>
        </div>
      </div>
    </div>
  );
}

// Create/Edit Driver - the username and initial password only apply on
// create (UpdateDriverRequest on the backend deliberately has no username
// or password fields, so an existing login is changed only through Reset
// Password, never edited in place here).
function DriverFormModal({ driver, onSave, onClose, isSubmitting }) {
  const isEdit = Boolean(driver);
  const [name, setName] = useState(driver?.name || '');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [mobile, setMobile] = useState(driver?.mobile || '');
  const [vehicleNumber, setVehicleNumber] = useState(driver?.vehicle_number || '');
  const [notes, setNotes] = useState(driver?.notes || '');
  const [error, setError] = useState(null);
  // Set only after a successful *create* - holds the screen open on the
  // credential reveal instead of closing immediately, since this is the
  // only time the plaintext password is ever visible again.
  const [createdCredentials, setCreatedCredentials] = useState(null);

  const canSubmit = name.trim() && (isEdit || (username.trim() && password.trim().length >= 4));

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!canSubmit) return;
    setError(null);
    const ok = await onSave({
      name: name.trim(),
      username: username.trim(),
      password,
      mobile: mobile.trim(),
      vehicle_number: vehicleNumber.trim(),
      notes: notes.trim(),
    });
    if (!ok) {
      setError('Could not save - see the toast for details.');
    } else if (isEdit) {
      onClose();
    } else {
      setCreatedCredentials({ username: username.trim(), password });
    }
  };

  if (createdCredentials) {
    return (
      <CredentialRevealPanel
        title="Driver Created"
        note="Share this login with the driver directly - it won't be shown again after you close this."
        username={createdCredentials.username}
        password={createdCredentials.password}
        onDone={onClose}
      />
    );
  }

  return (
    <div className="history-overlay" role="dialog" aria-modal="true" aria-label={isEdit ? 'Edit Driver' : 'Add Driver'}>
      <div className="history-overlay__scrim" onClick={onClose} />
      <form className="history-panel history-panel--wide" onSubmit={handleSubmit}>
        <div className="history-panel__head">
          <h2>{isEdit ? `Edit ${driver.name}` : 'Add Driver'}</h2>
          <button type="button" className="topbar__icon-btn" onClick={onClose} aria-label="Close"><IconX width={18} height={18} /></button>
        </div>
        <p className="history-panel__sub">
          {isEdit ? 'Update this driver\'s details.' : 'Create a login for the Driver App - the driver signs in with the username and password set here.'}
        </p>
        <div className="history-panel__body">
          <div className="driver-form-grid">
            <div className="driver-form-grid__full">
              <label className="modal__field-label" htmlFor="driver-name">Full name</label>
              <input id="driver-name" className="modal__input" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Ramesh Kumar" autoFocus />
            </div>

            {isEdit ? (
              <div className="driver-form-grid__full">
                <label className="modal__field-label">Login username</label>
                <div className="modal__static-value">{driver.username} <span className="modal__static-hint">— use Reset Password to change credentials</span></div>
              </div>
            ) : (
              <>
                <div>
                  <label className="modal__field-label" htmlFor="driver-username">Login username</label>
                  <input id="driver-username" className="modal__input" value={username} onChange={(e) => setUsername(e.target.value)} placeholder="e.g. ramesh.k" />
                </div>
                <div>
                  <label className="modal__field-label" htmlFor="driver-password">Initial password</label>
                  <input id="driver-password" className="modal__input" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="At least 4 characters" />
                </div>
              </>
            )}

            <div>
              <label className="modal__field-label" htmlFor="driver-mobile">Mobile number</label>
              <input id="driver-mobile" className="modal__input" value={mobile} onChange={(e) => setMobile(e.target.value)} placeholder="Optional" />
            </div>
            <div>
              <label className="modal__field-label" htmlFor="driver-vehicle">Vehicle number</label>
              <input id="driver-vehicle" className="modal__input" value={vehicleNumber} onChange={(e) => setVehicleNumber(e.target.value)} placeholder="e.g. TN-01-AB-1234 (optional)" />
            </div>

            <div className="driver-form-grid__full">
              <label className="modal__field-label" htmlFor="driver-notes">Notes</label>
              <input id="driver-notes" className="modal__input" value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="Optional" />
            </div>
          </div>
          {error && <div className="modal__warning"><IconAlert width={13} height={13} />{error}</div>}
        </div>
        <div className="history-panel__footer">
          <button type="button" className="btn btn--ghost" onClick={onClose}>Cancel</button>
          <button type="submit" className="btn btn--primary" disabled={!canSubmit || isSubmitting}>
            {isSubmitting ? 'Saving…' : isEdit ? 'Save Changes' : 'Create Driver'}
          </button>
        </div>
      </form>
    </div>
  );
}

function ResetPasswordModal({ driver, onSave, onClose, isSubmitting }) {
  const [password, setPassword] = useState('');
  const [error, setError] = useState(null);
  const [resetDone, setResetDone] = useState(false);
  const canSubmit = password.trim().length >= 4;

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!canSubmit) return;
    setError(null);
    const ok = await onSave(password);
    if (!ok) setError('Could not reset password - see the toast for details.');
    else setResetDone(true);
  };

  if (resetDone) {
    return (
      <CredentialRevealPanel
        title="Password Reset"
        note={`Share this with ${driver.name} directly - it won't be shown again after you close this.`}
        password={password}
        onDone={onClose}
      />
    );
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <form className="modal" onClick={(e) => e.stopPropagation()} onSubmit={handleSubmit}>
        <div className="modal__header">
          <h3>Reset Password — {driver.name}</h3>
          <button type="button" className="modal__close" onClick={onClose}><IconX width={16} height={16} /></button>
        </div>
        <div className="modal__body">
          <p className="modal__hint">
            This immediately signs {driver.name} out of the Driver App on every device. Share the new
            password with them directly - it isn't emailed or texted automatically.
          </p>
          <label className="modal__field-label" htmlFor="driver-new-password">New password</label>
          <input id="driver-new-password" className="modal__input" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="At least 4 characters" autoFocus />
        </div>
        {error && <div className="modal__warning"><IconAlert width={13} height={13} />{error}</div>}
        <div className="modal__footer">
          <button type="button" className="btn btn--ghost" onClick={onClose}>Cancel</button>
          <button type="submit" className="btn btn--primary" disabled={!canSubmit || isSubmitting}>
            {isSubmitting ? 'Resetting…' : 'Reset Password'}
          </button>
        </div>
      </form>
    </div>
  );
}

function App() {
  const [status, setStatus] = useState('Ready to upload Excel');
  const [fileName, setFileName] = useState('');
  const [isProcessing, setIsProcessing] = useState(false);
  const [totalOrders, setTotalOrders] = useState(0);
  const [errors, setErrors] = useState([]);
  const [isValid, setIsValid] = useState(true);
  const [orders, setOrders] = useState([]);
  const [failedOrders, setFailedOrders] = useState([]);
  const [routes, setRoutes] = useState([]);
  const [pendingOrders, setPendingOrders] = useState([]);
  const [warnings, setWarnings] = useState([]);
  const [cars, setCars] = useState(1);
  const [bikes, setBikes] = useState(2);
  const hasVehicles = cars + bikes > 0;

  // The upload this session is attached to - threaded through every
  // generate/regenerate/retry call so the resulting route plan (and any
  // resolved failed address) is saved against the right upload in the
  // database, instead of floating unlinked. Restored on refresh from
  // /api/dashboard, same as everything else below.
  const [batchId, setBatchId] = useState(null);
  // The uploaded sheet's exact left-to-right column layout - [{label,
  // field}, ...] - captured on upload, restored on refresh. Lets
  // "Download all" rebuild the upload's exact columns/order/wording
  // instead of a fixed layout this app made up.
  const [columnOrder, setColumnOrder] = useState([]);
  // True only for the very first render, while we ask the backend whether
  // there's a previous session to restore. Kept separate from isProcessing
  // so the upload spinner and the "restoring your last session" state never
  // get confused for one another.
  const [isRestoringSession, setIsRestoringSession] = useState(true);

  // The route plan currently on screen - every Generate/Regenerate/Retry
  // writes a fresh draft (plan_id) and isPlanSaved always starts false for
  // it. Save/Delete act on this id. Route History only ever lists plans the
  // user explicitly saved.
  const [planId, setPlanId] = useState(null);
  const [isPlanSaved, setIsPlanSaved] = useState(false);
  const [isSavingPlan, setIsSavingPlan] = useState(false);
  const [isDeletingPlan, setIsDeletingPlan] = useState(false);
  const [isDownloadingAll, setIsDownloadingAll] = useState(false);

  // Route History panel (opened from the sidebar) - a separate list from
  // the "current" plan above.
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyPlans, setHistoryPlans] = useState([]);
  const [isLoadingHistory, setIsLoadingHistory] = useState(false);
  const [historyError, setHistoryError] = useState(null);

  // Driver Data panel (opened from the sidebar) - every driver's past work
  // runs (crud_driver.list_driver_history), each backed by a real Route row
  // rather than a separate log table - see that function's own comment for
  // why. Independent of Route History above (that's saved route *plans*;
  // this is who actually drove, when, and how far).
  const [driverDataOpen, setDriverDataOpen] = useState(false);
  const [driverRuns, setDriverRuns] = useState([]);
  const [isLoadingDriverData, setIsLoadingDriverData] = useState(false);
  const [driverDataError, setDriverDataError] = useState(null);
  const [deletingRunId, setDeletingRunId] = useState(null); // route_id currently being deleted, or null

  // Drivers board (roster CRUD) - the assignment picker on a route's detail
  // view (RouteWorkspace) reads from this same `drivers` list, so it stays
  // fresh even before the Drivers board has scrolled into view this session.
  const [drivers, setDrivers] = useState([]);
  const [isLoadingDrivers, setIsLoadingDrivers] = useState(false);
  const [driversError, setDriversError] = useState(null);
  const [driverFormOpen, setDriverFormOpen] = useState(false);
  const [editingDriver, setEditingDriver] = useState(null); // null = create mode
  const [isSavingDriver, setIsSavingDriver] = useState(false);
  const [resetPasswordDriver, setResetPasswordDriver] = useState(null);
  const [isResettingPassword, setIsResettingPassword] = useState(false);
  const [isTogglingDriverStatus, setIsTogglingDriverStatus] = useState(null); // driver id or null
  const [isDeletingDriver, setIsDeletingDriver] = useState(null); // driver id or null

  // The geocoded orders routes were last built from - kept around so the
  // fleet count can change and routes can be rebuilt without re-uploading.
  const [successfulOrders, setSuccessfulOrders] = useState([]);
  const [isRegenerating, setIsRegenerating] = useState(false);
  // Route names touched by a manual stop move - flagged in the UI since
  // their distance/time stats go stale until the next regenerate.
  const [editedRoutes, setEditedRoutes] = useState([]);

  // Editable address state per failed order
  const [editingAddresses, setEditingAddresses] = useState({});
  const [retryingOrderId, setRetryingOrderId] = useState(null);
  // Master/detail selection for the Returns board, and what happened the
  // last time each order was retried (cleared on the next attempt).
  const [selectedFailedId, setSelectedFailedId] = useState(null);
  const [retryFeedback, setRetryFeedback] = useState({});

  // Per-route "show the full stop-by-stop breakdown" toggle - keeps a busy
  // dispatch board from turning into a wall of text once there are several
  // routes on screen at once.
  const [collapsedRoutes, setCollapsedRoutes] = useState({});
  const toggleRouteDetails = (routeName) => {
    setCollapsedRoutes((prev) => ({ ...prev, [routeName]: !prev[routeName] }));
  };

  // Session tabs - a browser-tab-style strip built entirely in our own UI
  // (no window.open, no real new browser tab/window). Each tab holds its
  // own upload/routes/etc.; switching tabs snapshots the one you're leaving
  // and restores the one you're entering. Tabs live only in memory for this
  // page load - they aren't persisted the way a saved route plan is, same
  // as ordinary browser tabs don't survive after the page itself reloads.
  const INITIAL_TAB_KEY = 'tab-1';
  const [tabs, setTabs] = useState([{ key: INITIAL_TAB_KEY, label: 'New session' }]);
  const [activeTabKey, setActiveTabKey] = useState(INITIAL_TAB_KEY);
  const tabCounterRef = useRef(1);
  const tabSnapshotsRef = useRef({});

  const blankTabSnapshot = (seedCars, seedBikes) => ({
    status: 'Ready to upload Excel',
    fileName: '',
    totalOrders: 0,
    errors: [],
    isValid: true,
    orders: [],
    failedOrders: [],
    routes: [],
    pendingOrders: [],
    warnings: [],
    cars: seedCars,
    bikes: seedBikes,
    batchId: null,
    columnOrder: [],
    planId: null,
    isPlanSaved: false,
    successfulOrders: [],
    editedRoutes: [],
    editingAddresses: {},
    retryFeedback: {},
    selectedFailedId: null,
    collapsedRoutes: {},
  });

  const captureTabSnapshot = () => ({
    status, fileName, totalOrders, errors, isValid, orders, failedOrders,
    routes, pendingOrders, warnings, cars, bikes, batchId, columnOrder, planId, isPlanSaved,
    successfulOrders, editedRoutes, editingAddresses, retryFeedback,
    selectedFailedId, collapsedRoutes,
  });

  const applyTabSnapshot = (snap) => {
    setStatus(snap.status);
    setFileName(snap.fileName);
    setTotalOrders(snap.totalOrders);
    setErrors(snap.errors);
    setIsValid(snap.isValid);
    setOrders(snap.orders);
    setFailedOrders(snap.failedOrders);
    setRoutes(snap.routes);
    setPendingOrders(snap.pendingOrders);
    setWarnings(snap.warnings);
    setCars(snap.cars);
    setBikes(snap.bikes);
    setBatchId(snap.batchId);
    setColumnOrder(snap.columnOrder || []);
    setPlanId(snap.planId);
    setIsPlanSaved(snap.isPlanSaved);
    setSuccessfulOrders(snap.successfulOrders);
    setEditedRoutes(snap.editedRoutes);
    setEditingAddresses(snap.editingAddresses);
    setRetryFeedback(snap.retryFeedback);
    setSelectedFailedId(snap.selectedFailedId);
    setCollapsedRoutes(snap.collapsedRoutes);
  };

  // Keep the visible tab label in sync with whatever's loaded into it -
  // the file name once an upload lands, "New session" until then.
  useEffect(() => {
    setTabs((prev) => prev.map((t) => (
      t.key === activeTabKey ? { ...t, label: fileName || 'New session' } : t
    )));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fileName, activeTabKey]);

  // Opens a new tab - exactly like Chrome's "+": the tab you're on keeps
  // everything it had and stays open in the strip, the new one starts
  // blank and becomes active.
  const handleNewTab = () => {
    tabSnapshotsRef.current[activeTabKey] = captureTabSnapshot();
    const newKey = `tab-${++tabCounterRef.current}`;
    tabSnapshotsRef.current[newKey] = blankTabSnapshot(cars, bikes);
    setTabs((prev) => [...prev, { key: newKey, label: 'New session' }]);
    setActiveTabKey(newKey);
    applyTabSnapshot(tabSnapshotsRef.current[newKey]);
    showToast('Opened a new session tab.');
  };

  const handleSwitchTab = (key) => {
    if (key === activeTabKey) return;
    tabSnapshotsRef.current[activeTabKey] = captureTabSnapshot();
    const target = tabSnapshotsRef.current[key] || blankTabSnapshot(cars, bikes);
    applyTabSnapshot(target);
    setActiveTabKey(key);
  };

  // Closing a tab only removes it from this strip - like closing a browser
  // tab, it never deletes anything from the database. Use Delete (on the
  // route plan) or Route History for that.
  const handleCloseTab = (key, event) => {
    event.stopPropagation();
    delete tabSnapshotsRef.current[key];
    setTabs((prev) => {
      const closingIndex = prev.findIndex((t) => t.key === key);
      const remaining = prev.filter((t) => t.key !== key);

      if (key !== activeTabKey) return remaining;

      if (remaining.length === 0) {
        const newKey = `tab-${++tabCounterRef.current}`;
        tabSnapshotsRef.current[newKey] = blankTabSnapshot(cars, bikes);
        setActiveTabKey(newKey);
        applyTabSnapshot(tabSnapshotsRef.current[newKey]);
        return [{ key: newKey, label: 'New session' }];
      }

      const fallback = remaining[Math.max(0, closingIndex - 1)] || remaining[0];
      applyTabSnapshot(tabSnapshotsRef.current[fallback.key] || blankTabSnapshot(cars, bikes));
      setActiveTabKey(fallback.key);
      return remaining;
    });
  };

  // Shell chrome: sidebar, theme, search, toast.
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [activeNav, setActiveNav] = useState('dashboard');
  // A real click on "Routes"/"Unassigned Orders" in the sidebar should
  // force-switch RouteWorkspace's internal tab (see requestedTab below) -
  // but activeNav ALSO changes continuously while merely scrolling the
  // page (the IntersectionObserver above), and RouteWorkspace and
  // Unassigned Orders share one DOM anchor/component, so scrolling that
  // section into view was setting activeNav to 'unassigned' too - which
  // then silently flipped the visible tab away from Routes to Unassigned,
  // making the route list "disappear" with no click involved at all.
  // navCommandSeq only advances inside handleNavClick's own body, never
  // from the scroll-tracking effect, so RouteWorkspace can tell a genuine
  // click apart from activeNav simply passing through 'unassigned' on its
  // way past while scrolling.
  const [navCommandSeq, setNavCommandSeq] = useState(0);
  // Whether RouteWorkspace is currently showing a single route's detail
  // page (as opposed to the Routes/Unassigned Orders list view) - lifted
  // up via onViewChange so the KPI row/shortcuts/Generate Routes panel
  // above it can hide once you've drilled into one route, instead of
  // that whole preamble still sitting above what's meant to be a focused
  // single-route page.
  const [routeDetailOpen, setRouteDetailOpen] = useState(false);
  // Which "Soon" item's overlay (if any) is open - null when closed. See
  // handleSoonClick / ComingSoonPage.
  const [soonOverlay, setSoonOverlay] = useState(null);
  const [searchTerm, setSearchTerm] = useState('');
  const [quickActionsOpen, setQuickActionsOpen] = useState(false);
  const quickActionsRef = useRef(null);
  useEffect(() => {
    if (!quickActionsOpen) return undefined;
    const handler = (e) => {
      if (quickActionsRef.current && !quickActionsRef.current.contains(e.target)) setQuickActionsOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [quickActionsOpen]);

  // Generate Routes panel's own overflow menu (Download all/Save/Delete -
  // Regenerate stays the one always-visible primary action) - same
  // open/outside-click-close shape as quickActionsOpen above.
  const [toolbarOverflowOpen, setToolbarOverflowOpen] = useState(false);
  const toolbarOverflowRef = useRef(null);
  useEffect(() => {
    if (!toolbarOverflowOpen) return undefined;
    const handler = (e) => {
      if (toolbarOverflowRef.current && !toolbarOverflowRef.current.contains(e.target)) setToolbarOverflowOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [toolbarOverflowOpen]);
  const [isDraggingManifest, setIsDraggingManifest] = useState(false);

  // Command palette (⌘K/Ctrl+K) - separate from topbar__search above,
  // which stays a live filter over the boards already on screen. This is
  // a jump-to/run-this overlay instead.
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [paletteQuery, setPaletteQuery] = useState('');
  useEffect(() => {
    const handler = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setPaletteOpen((v) => !v);
        setPaletteQuery('');
      } else if (e.key === 'Escape') {
        setPaletteOpen(false);
      }
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, []);

  // Issues panel's own collapse state - a UI toggle like quickActionsOpen
  // above, not part of the session-tab snapshot/restore system.
  const [issuesOpen, setIssuesOpen] = useState(true);

  const [toast, setToast] = useState(null);
  const toastTimerRef = useRef(null);
  const showToast = (message) => {
    setToast(message);
    clearTimeout(toastTimerRef.current);
    toastTimerRef.current = setTimeout(() => setToast(null), 2600);
  };
  useEffect(() => () => clearTimeout(toastTimerRef.current), []);

  // Click ripple for every .btn on the page - one delegated listener
  // instead of wiring each button by hand.
  useEffect(() => {
    const handler = (e) => {
      const btn = e.target.closest('.btn, .toolbar-btn, .download-link, .map-link, .quick-action');
      if (!btn || btn.disabled) return;
      const rect = btn.getBoundingClientRect();
      const size = Math.max(rect.width, rect.height);
      const ripple = document.createElement('span');
      ripple.className = 'btn-ripple';
      ripple.style.width = `${size}px`;
      ripple.style.height = `${size}px`;
      ripple.style.left = `${e.clientX - rect.left - size / 2}px`;
      ripple.style.top = `${e.clientY - rect.top - size / 2}px`;
      btn.appendChild(ripple);
      ripple.addEventListener('animationend', () => ripple.remove());
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  const [theme, setTheme] = useState(() => localStorage.getItem('rp-theme') || 'system');
  const [systemPrefersDark, setSystemPrefersDark] = useState(
    () => window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false
  );
  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const handler = (e) => setSystemPrefersDark(e.matches);
    mq.addEventListener('change', handler);
    return () => mq.removeEventListener('change', handler);
  }, []);
  useEffect(() => {
    if (theme === 'system') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('rp-theme', theme);
  }, [theme]);
  const isDark = theme === 'system' ? systemPrefersDark : theme === 'dark';
  const toggleTheme = () => setTheme(isDark ? 'light' : 'dark');

  // Each sidebar item is now a real, individual page - only one section of
  // the console mounts at a time (see the activeNav gates around the
  // toolbar/boards below), swapped straight away rather than scrolled to.
  // Drivers and Route History stay as overlay panels layered on top, since
  // that's genuinely how they work (a picker/record list you dip into and
  // dismiss), not a page you'd otherwise navigate away from.
  const handleNavClick = (item) => {
    setActiveNav(item.key);
    setMobileNavOpen(false);
    setSoonOverlay(null);
    // A genuine click, as opposed to activeNav merely passing through
    // 'unassigned'/'generate' while scrolling - see navCommandSeq's own
    // comment above for why this distinction is what actually matters.
    if (item.key === 'unassigned' || item.key === 'generate' || item.key === 'live-tracking') {
      setNavCommandSeq((n) => n + 1);
    }
    if (item.key === 'history') {
      openHistory();
    } else if (item.key === 'driver-data') {
      openDriverData();
    } else if (item.anchor === 'top') {
      window.scrollTo({ top: 0, behavior: 'smooth' });
    } else {
      document.getElementById(item.anchor)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  };
  // "Soon" items have no real page content to scroll to, so they open
  // ComingSoonPage as a small overlay instead - same idea as the Drivers
  // panel, just for a page that's honestly not built yet.
  const handleSoonClick = (item) => {
    setActiveNav(item.key);
    setMobileNavOpen(false);
    setSoonOverlay(item.key);
  };

  // Sidebar scroll-spy - the four boards below are one continuous page
  // (see NAV_GROUPS' own comment above), reachable either by clicking a
  // nav item (which smooth-scrolls to it) or just scrolling by hand. Only
  // the click path used to update activeNav, so scrolling manually left
  // whichever item was clicked last highlighted no matter what was
  // actually on screen. Tracks the same anchors handleNavClick already
  // scrolls to, so the two stay in sync automatically.
  //
  // Computed directly from each section's current position on every
  // observer firing, rather than accumulated from separate enter/leave
  // events - an earlier version tracked a Set built up from those events,
  // which had a transient gap while crossing from one section into the
  // next (the outgoing section's "leave" landing before the incoming
  // one's "enter" registered): the Set went briefly empty, activeNav fell
  // back to 'dashboard', then immediately flipped back once the next
  // section registered - visible as the sidebar highlight flickering to
  // Dashboard and back on every section boundary. Recomputing "the last
  // section whose top has crossed the line" from scratch each time has no
  // such gap state to flicker through.
  useEffect(() => {
    if (routeDetailOpen) return undefined;
    const sectionKeys = ['generate', 'unassigned', 'failed', 'drivers'];
    const sections = sectionKeys
      .map((key) => ({ key, el: document.getElementById(findNavItem(key).anchor) }))
      .filter((s) => s.el);
    if (sections.length === 0) return undefined;

    // Same line the old rootMargin top offset used - just past the
    // sticky topbar/tabs strip.
    const LINE_PX = 112;
    const updateActive = () => {
      // A History/Driver Data/Soon overlay owns the sidebar highlight
      // while it's open - don't let a section scrolling into view behind
      // it steal that back.
      if (historyOpen || driverDataOpen || soonOverlay) return;
      let current = 'dashboard';
      sections.forEach(({ key, el }) => {
        if (el.getBoundingClientRect().top <= LINE_PX) current = key;
      });
      setActiveNav((prev) => (prev === current ? prev : current));
    };

    updateActive();
    const observer = new IntersectionObserver(updateActive, {
      rootMargin: `-${LINE_PX}px 0px -65% 0px`,
      threshold: [0, 1],
    });
    sections.forEach((s) => observer.observe(s.el));
    return () => observer.disconnect();
  }, [routeDetailOpen, historyOpen, driverDataOpen, soonOverlay]);

  // Client-side search across orders and routes - never mutates state,
  // just narrows what the three boards render.
  const searchQuery = searchTerm.trim().toLowerCase();
  const matchesSearch = (order) => {
    if (!searchQuery) return true;
    return (
      String(order.order_id ?? '').toLowerCase().includes(searchQuery)
      || (order.customer_name || '').toLowerCase().includes(searchQuery)
      || (order.address || '').toLowerCase().includes(searchQuery)
    );
  };

  const apiFetch = async (endpoint, options = {}) => {
    // Same-origin by default: the Vite dev server proxies /api, /health,
    // /upload-excel and /generate-routes to the backend on 127.0.0.1:8000
    // (see vite.config.js). This is what makes the app work both at
    // localhost:3000 and through a Cloudflare tunnel URL - a hardcoded
    // "http://<host>:8000" would silently fail on the tunnel host, since
    // that port was never tunneled.
    //
    // On Render, frontend and backend are separate services with different
    // origins, so there's no dev-proxy to rely on - VITE_API_BASE_URL (set
    // at build time to the backend's Render URL) is prefixed instead. Unset
    // (the local-dev/tunnel case), it's '' and behavior is unchanged.
    return fetch(`${API_BASE_URL}${endpoint}`, options);
  };

  // The authoritative Unassigned Orders list - GET /api/orders/unassigned,
  // scoped server-side to the current session, includes previous-route
  // history and a precise per-delivery map link that the plan-snapshot
  // dicts elsewhere don't carry. Called after every action that can change
  // who's unassigned (upload, generate/regenerate, retry-geocode, and every
  // manual add/remove already reconciles pendingOrders itself).
  const refreshUnassignedOrders = async (search) => {
    try {
      const params = search ? `?search=${encodeURIComponent(search)}` : '';
      const res = await apiFetch(`/api/orders/unassigned${params}`);
      if (!res.ok) return;
      const data = await res.json();
      setPendingOrders(data.orders || []);
    } catch (err) {
      console.error('Could not refresh Unassigned Orders:', err);
    }
  };

  // On first load, ask the backend for whatever the last session left
  // behind - a refresh, a backend restart, or reopening the browser should
  // never lose an upload, its routes, its pending pool, or its failed
  // addresses. If there's nothing saved yet, this just seeds the fleet
  // defaults and leaves the normal "Ready to upload Excel" empty state.
  //
  // Render's backend spins down when idle and takes 20-50s to wake back up;
  // a request that lands mid-boot gets a connection error (not a slow
  // response) and used to fail silently on the very first try, which is why
  // the restored session ("testing.xlsx · 36 orders …") would sometimes just
  // not appear - not a data-loss bug, a single unretried fetch racing a cold
  // start. Retried with backoff below so a cold backend gets a real chance
  // to answer before this gives up.
  useEffect(() => {
    let cancelled = false;
    const delaysMs = [0, 2000, 4000, 8000, 8000, 8000]; // ~30s of coverage
    (async () => {
      for (let attempt = 0; attempt < delaysMs.length; attempt++) {
        if (attempt > 0) {
          setStatus('Reconnecting to the server…');
          await new Promise((resolve) => setTimeout(resolve, delaysMs[attempt]));
        }
        if (cancelled) return;
        try {
          const response = await apiFetch('/api/dashboard');
          if (!response.ok) throw new Error(`dashboard fetch failed: ${response.status}`);
          const data = await response.json();
          if (cancelled) return;

          if (data.settings) {
            setCars(data.settings.default_car_count ?? 1);
            setBikes(data.settings.default_bike_count ?? 2);
          }

          if (data.has_data) {
            setBatchId(data.batch_id ?? null);
            setColumnOrder(data.column_order || []);
            setFileName(data.file_name || '');
            setTotalOrders(data.total_orders || 0);
            setErrors(data.errors || []);
            setIsValid(data.is_valid !== false);
            setOrders(data.orders || []);
            setFailedOrders(data.failed_orders || []);
            setSuccessfulOrders((data.orders || []).filter((o) => o.lat != null && o.lng != null));
            setRoutes(data.routes || []);
            setPendingOrders(data.pending_orders || []);
            setWarnings(data.warnings || []);
            setPlanId(data.plan_id ?? null);
            setIsPlanSaved(data.plan_is_saved ?? false);

            const initialEdits = {};
            (data.failed_orders || []).forEach((order) => {
              initialEdits[order.order_id] = order.address || '';
            });
            setEditingAddresses(initialEdits);

            setStatus('Restored your last session.');
            refreshUnassignedOrders();
          } else {
            setStatus('Ready to upload Excel');
          }
          if (!cancelled) setIsRestoringSession(false);
          return;
        } catch (err) {
          console.error(`Could not restore previous session (attempt ${attempt + 1}/${delaysMs.length}):`, err);
          // fall through and retry
        }
      }
      // Every attempt failed - say so instead of quietly sitting on the
      // blank empty state as if nothing had ever been uploaded.
      if (!cancelled) {
        setStatus("Couldn't reach the server - showing a blank session. Refresh to try again.");
        setIsRestoringSession(false);
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // The driver roster loads independently of the upload/routes session
  // above - it's not part of that snapshot, and the route-detail "Assign
  // Driver" picker needs it even before the Drivers panel is ever opened.
  useEffect(() => {
    refreshDrivers();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Resets the CURRENT tab back to the pre-upload empty state - file name,
  // order count, orders, failed addresses, everything. Used after deleting
  // a route plan (deleting a route must clear the upload info that went
  // with it, not just the routes board) - never deletes anything from the
  // database itself, that already happened via the DELETE call before this
  // runs.
  const resetActiveSessionState = (message) => {
    setBatchId(null);
    setColumnOrder([]);
    setFileName('');
    setTotalOrders(0);
    setErrors([]);
    setIsValid(true);
    setOrders([]);
    setFailedOrders([]);
    setSuccessfulOrders([]);
    setRoutes([]);
    setPendingOrders([]);
    setWarnings([]);
    setEditingAddresses({});
    setRetryFeedback({});
    setEditedRoutes([]);
    setPlanId(null);
    setIsPlanSaved(false);
    setSelectedFailedId(null);
    setCollapsedRoutes({});
    setStatus(message);
  };

  // Promotes the currently-viewed route plan into Route History. Generate/
  // Regenerate/Retry always keep a draft persisted (so refresh never loses
  // work) but Route History only ever lists what was explicitly saved.
  const handleSaveToHistory = async () => {
    if (!planId) return;
    setIsSavingPlan(true);
    try {
      const response = await apiFetch(`/api/routes/${planId}/save`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      if (!response.ok) throw new Error(`Save failed (${response.status})`);
      setIsPlanSaved(true);
      showToast('Saved to Route History.');
    } catch (err) {
      console.error('Save to history failed:', err);
      showToast('Could not save - check the backend connection.');
    } finally {
      setIsSavingPlan(false);
    }
  };

  const deleteRoutePlanById = async (id) => {
    const response = await apiFetch(`/api/routes/${id}`, { method: 'DELETE' });
    return response.ok;
  };

  // Deletes the route plan currently on screen (draft or saved - Delete
  // works on either). Clears the WHOLE session view afterward - status,
  // file name, order count, orders, failed addresses, not just the routes
  // board - since none of that means anything once the route it belongs to
  // is gone.
  const handleDeleteCurrentPlan = async () => {
    if (!planId) return;
    if (!window.confirm('Delete this route plan? This cannot be undone.')) return;
    setIsDeletingPlan(true);
    const ok = await deleteRoutePlanById(planId).catch(() => false);
    setIsDeletingPlan(false);
    if (ok) {
      const deletedId = planId;
      resetActiveSessionState('Route plan deleted.');
      showToast('Route plan deleted.');
      setHistoryPlans((prev) => prev.filter((p) => p.plan_id !== deletedId));
    } else {
      showToast('Could not delete - check the backend connection.');
    }
  };

  // Opens Route History and loads the saved-plan list.
  const openHistory = async () => {
    setHistoryOpen(true);
    setIsLoadingHistory(true);
    setHistoryError(null);
    try {
      const response = await apiFetch('/api/routes/history?limit=50');
      if (!response.ok) throw new Error(`Failed to load history (${response.status})`);
      const data = await response.json();
      setHistoryPlans(data.plans || []);
    } catch (err) {
      console.error('Could not load route history:', err);
      setHistoryError('Could not load Route History - check the backend connection.');
    } finally {
      setIsLoadingHistory(false);
    }
  };

  // Opens Driver Data and loads every driver's past work runs.
  const openDriverData = async () => {
    setDriverDataOpen(true);
    setIsLoadingDriverData(true);
    setDriverDataError(null);
    try {
      const response = await apiFetch('/api/drivers/history?limit=100');
      if (!response.ok) throw new Error(`Failed to load driver history (${response.status})`);
      const data = await response.json();
      setDriverRuns(data.runs || []);
    } catch (err) {
      console.error('Could not load driver history:', err);
      setDriverDataError('Could not load Driver Data - check the backend connection.');
    } finally {
      setIsLoadingDriverData(false);
    }
  };

  // Deletes one run's record. Only finished runs can be deleted from here
  // (the backend refuses an in_progress one - see RouteStillActiveError) -
  // an active, currently-tracked run belongs to Live Tracking, not this log.
  const handleDeleteDriverRun = async (run) => {
    if (!window.confirm(`Delete this run record for ${run.driver_name} (${run.route_name})? This cannot be undone.`)) return;
    setDeletingRunId(run.route_id);
    try {
      const response = await apiFetch(`/api/drivers/history/${run.route_id}`, { method: 'DELETE' });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || `Failed to delete (${response.status})`);
      }
      setDriverRuns((prev) => prev.filter((r) => r.route_id !== run.route_id));
      showToast('Run record deleted.');
    } catch (err) {
      console.error('Could not delete run record:', err);
      showToast(err.message || 'Could not delete this record.');
    } finally {
      setDeletingRunId(null);
    }
  };

  // Driver roster - independent of the upload/routes session above, so it's
  // loaded once on mount (see the effect near the top) and refreshed after
  // every create/edit/status/reassignment call, whether or not the Drivers
  // panel itself is open (the route-detail "Assign Driver" picker reads
  // this same list).
  const refreshDrivers = async () => {
    setIsLoadingDrivers(true);
    setDriversError(null);
    try {
      const response = await apiFetch('/api/drivers');
      if (!response.ok) throw new Error(`Failed to load drivers (${response.status})`);
      const data = await response.json();
      setDrivers(data.drivers || []);
    } catch (err) {
      console.error('Could not load drivers:', err);
      setDriversError('Could not load drivers - check the backend connection.');
    } finally {
      setIsLoadingDrivers(false);
    }
  };

  const handleSaveDriver = async (formValues) => {
    setIsSavingDriver(true);
    try {
      let response;
      if (editingDriver) {
        response = await apiFetch(`/api/drivers/${editingDriver.id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: formValues.name,
            mobile: formValues.mobile || null,
            vehicle_number: formValues.vehicle_number || null,
            notes: formValues.notes || null,
          }),
        });
      } else {
        response = await apiFetch('/api/drivers', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: formValues.name,
            username: formValues.username,
            password: formValues.password,
            mobile: formValues.mobile || null,
            vehicle_number: formValues.vehicle_number || null,
            notes: formValues.notes || null,
          }),
        });
      }
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.detail || `Could not save driver (${response.status})`);
      }
      showToast(editingDriver ? 'Driver updated.' : `Driver created - login "${formValues.username}".`);
      await refreshDrivers();
      // Modal stays open on success - DriverFormModal shows the one-time
      // credential reveal (create) or closes itself directly (edit), then
      // calls onClose itself. Closing it here first would skip that screen.
      return true;
    } catch (err) {
      showToast(err.message || 'Could not save driver.');
      return false;
    } finally {
      setIsSavingDriver(false);
    }
  };

  // Deactivating revokes every session the driver is currently logged in
  // with on their end (server-side, on the next status check) - worth
  // saying plainly in the confirm, not just doing it silently.
  const handleToggleDriverStatus = async (driver) => {
    const nextStatus = driver.status === 'active' ? 'inactive' : 'active';
    if (nextStatus === 'inactive') {
      const warn = driver.assigned_route_name
        ? ` They'll stay assigned to ${driver.assigned_route_name} until you reassign it.`
        : '';
      if (!window.confirm(`Deactivate ${driver.name}? This signs them out of the Driver App immediately.${warn}`)) return;
    }
    setIsTogglingDriverStatus(driver.id);
    try {
      const response = await apiFetch(`/api/drivers/${driver.id}/status`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: nextStatus }),
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.detail || `Could not update status (${response.status})`);
      }
      showToast(`${driver.name} ${nextStatus === 'active' ? 'activated' : 'deactivated'}.`);
      await refreshDrivers();
    } catch (err) {
      showToast(err.message || 'Could not update driver status.');
    } finally {
      setIsTogglingDriverStatus(null);
    }
  };

  // Permanent - unlike deactivate, this can't be undone. Un-assigns their
  // route (if any) rather than touching it, and drops their session/GPS
  // history for good; see crud_driver.delete_driver on the backend.
  const handleDeleteDriver = async (driver) => {
    const routeWarning = driver.assigned_route_name
      ? ` They'll be removed from ${driver.assigned_route_name} first - the route itself is untouched.`
      : '';
    if (!window.confirm(`Permanently delete ${driver.name} (${driver.driver_code})? This can't be undone - their login and location history are removed for good.${routeWarning}`)) return;
    setIsDeletingDriver(driver.id);
    try {
      const response = await apiFetch(`/api/drivers/${driver.id}`, { method: 'DELETE' });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.detail || `Could not delete driver (${response.status})`);
      }
      showToast(`${driver.name} deleted.`);
      await refreshDrivers();
    } catch (err) {
      showToast(err.message || 'Could not delete driver.');
    } finally {
      setIsDeletingDriver(null);
    }
  };

  const handleResetPassword = async (newPassword) => {
    if (!resetPasswordDriver) return false;
    setIsResettingPassword(true);
    try {
      const response = await apiFetch(`/api/drivers/${resetPasswordDriver.id}/reset-password`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ new_password: newPassword }),
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.detail || `Could not reset password (${response.status})`);
      }
      showToast(`Password reset for ${resetPasswordDriver.name}.`);
      // Modal stays open here too - ResetPasswordModal shows the same
      // one-time credential reveal, then calls onClose itself on "Done".
      return true;
    } catch (err) {
      showToast(err.message || 'Could not reset password.');
      return false;
    } finally {
      setIsResettingPassword(false);
    }
  };

  // Assignment - called from a route's detail view (RouteWorkspace), not
  // from the Drivers panel itself. Returns the raw response body so the
  // caller can act on a `conflict: true` reply (driver's already elsewhere)
  // without this function guessing what "confirm and retry with force"
  // should look like in that UI.
  const handleAssignDriver = async (routeId, driverId, force = false) => {
    const response = await apiFetch(`/api/routes/${routeId}/driver`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ driver_id: driverId, force }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || `Could not assign driver (${response.status})`);
    if (!data.conflict) {
      showToast(`${data.driver.name} assigned to ${data.route.route_name}.`);
      refreshDrivers();
    }
    return data;
  };

  const handleUnassignDriver = async (routeId) => {
    const response = await apiFetch(`/api/routes/${routeId}/driver`, { method: 'DELETE' });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || `Could not unassign driver (${response.status})`);
    }
    showToast('Driver unassigned from this route.');
    refreshDrivers();
    return response.json();
  };

  const fetchRouteTracking = async (routeId) => {
    const response = await apiFetch(`/api/routes/${routeId}/tracking`);
    if (!response.ok) throw new Error(`Could not load tracking (${response.status})`);
    return response.json();
  };

  // The planned route's road-following shape (depot -> every stop, in
  // order) for the live map - fetched once when it opens, not on every
  // tracking poll, since the stop sequence doesn't change mid-route.
  const fetchRoutePlannedPath = async (routeId) => {
    const response = await apiFetch(`/api/routes/${routeId}/route-path`);
    if (!response.ok) throw new Error(`Could not load the planned route (${response.status})`);
    return response.json();
  };

  // Global "a driver just started their route" notification - fires as a
  // toast from anywhere in the app, not only while that route's detail
  // view happens to be open (DriverTrackingCard's own polling is scoped to
  // its one route and doesn't help here). Polls every route currently
  // loaded rather than filtering by routes[].driver_id first, since that
  // field is only ever set at load time and goes stale the moment a
  // driver is assigned mid-session - /tracking itself is always current.
  const routeRunStatusRef = useRef({});
  useEffect(() => {
    if (routes.length === 0) return undefined;
    let cancelled = false;
    const poll = async () => {
      for (const r of routes) {
        try {
          const data = await fetchRouteTracking(r.route_id);
          if (cancelled) return;
          const prev = routeRunStatusRef.current[r.route_id];
          if (prev && prev !== 'in_progress' && data.route_run_status === 'in_progress') {
            showToast(`🚗 ${data.driver?.name || 'A driver'} started ${r.route_name}`);
          }
          routeRunStatusRef.current[r.route_id] = data.route_run_status;
        } catch {
          // A transient failure on one route this cycle isn't worth
          // surfacing - the next poll tries again.
        }
      }
    };
    poll();
    const interval = setInterval(poll, 25000);
    return () => { cancelled = true; clearInterval(interval); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routes.map((r) => r.route_id).join(',')]);

  // Switches the whole working view over to a saved plan from history -
  // its routes/pending/fleet, and (if it's tied to an upload) that
  // upload's orders and failed addresses too.
  const handleOpenSavedPlan = async (savedPlanId) => {
    try {
      const planResponse = await apiFetch(`/api/routes/history/${savedPlanId}`);
      if (!planResponse.ok) throw new Error(`Plan not found (${planResponse.status})`);
      const planDetail = await planResponse.json();

      setRoutes(planDetail.routes || []);
      setPendingOrders(planDetail.pending_orders || []);
      setWarnings(planDetail.warnings || []);
      setPlanId(planDetail.plan_id ?? savedPlanId);
      setIsPlanSaved(true);
      setCars(planDetail.available_cars ?? cars);
      setBikes(planDetail.available_bikes ?? bikes);
      setEditedRoutes([]);

      if (planDetail.batch_id) {
        const batchResponse = await apiFetch(`/api/history/${planDetail.batch_id}`);
        if (batchResponse.ok) {
          const batchDetail = await batchResponse.json();
          setBatchId(batchDetail.id);
          setColumnOrder(batchDetail.column_order || []);
          setFileName(batchDetail.file_name || '');
          setTotalOrders(batchDetail.total_orders || 0);
          setOrders(batchDetail.orders || []);
          const batchOrders = batchDetail.orders || [];
          setSuccessfulOrders(batchOrders.filter((o) => o.lat != null && o.lng != null));
          const failed = batchOrders.filter((o) => o.status === 'failed');
          setFailedOrders(failed);
          const initialEdits = {};
          failed.forEach((order) => { initialEdits[order.order_id] = order.address || ''; });
          setEditingAddresses(initialEdits);
        }
      } else {
        setBatchId(null);
        setColumnOrder([]);
      }

      setHistoryOpen(false);
      setStatus(`Opened "${planDetail.label || 'saved route plan'}" from history.`);
      window.scrollTo({ top: 0, behavior: 'smooth' });
    } catch (err) {
      console.error('Could not open saved plan:', err);
      showToast('Could not open that route plan.');
    }
  };

  const handleDeleteSavedPlan = async (savedPlanId) => {
    if (!window.confirm('Delete this saved route plan? This cannot be undone.')) return;
    const ok = await deleteRoutePlanById(savedPlanId).catch(() => false);
    if (ok) {
      setHistoryPlans((prev) => prev.filter((p) => p.plan_id !== savedPlanId));
      if (planId === savedPlanId) {
        resetActiveSessionState('Route plan deleted.');
      }
      showToast('Deleted from history.');
    } else {
      showToast('Could not delete - check the backend connection.');
    }
  };

  // Takes the File directly rather than an input change event, so both
  // the file input's onChange and the dropzone's onDrop can feed it the
  // same way instead of duplicating everything below.
  const handleUpload = async (file) => {
    if (!file) return;

    setFileName(file.name);
    setIsProcessing(true);
    const formData = new FormData();
    formData.append('file', file);

    try {
      const response = await apiFetch('/api/orders/upload', {
        method: 'POST',
        body: formData,
      });

      const data = await response.json();
      setStatus(data.message);
      setTotalOrders(data.total_orders);
      setErrors(data.errors || []);
      setIsValid(data.is_valid);
      setOrders(data.orders || []);
      setBatchId(data.batch_id ?? null);
      setColumnOrder(data.column_order || []);

      const failed = data.failed_orders || [];
      setFailedOrders(failed);

      // Pre-fill editable addresses for failed orders
      const initialEdits = {};
      failed.forEach((order) => {
        initialEdits[order.order_id] = order.address || '';
      });
      setEditingAddresses(initialEdits);

      const routeOrders = data.successful_orders || (data.orders || []).filter(
        (order) => order.lat != null && order.lng != null
      );
      setSuccessfulOrders(routeOrders);
      setEditedRoutes([]);

      if (routeOrders.length === 0) {
        setRoutes([]);
        setPendingOrders([]);
        setWarnings([]);
        setPlanId(null);
        setIsPlanSaved(false);
      } else if (!hasVehicles) {
        // Don't even call the backend with a vehicle pool that's guaranteed
        // to produce zero routes - every order would silently land in
        // "pending" with nothing shown in the routes panel.
        setRoutes([]);
        setPendingOrders(routeOrders);
        setWarnings(['No vehicles configured - set at least one car or bike, then re-upload to generate routes.']);
        setPlanId(null);
        setIsPlanSaved(false);
      } else {
        const routeResponse = await apiFetch('/api/routes/generate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            orders: routeOrders,
            available_cars: cars,
            available_bikes: bikes,
            batch_id: data.batch_id ?? null,
          }),
        });

        const routeData = await routeResponse.json();
        setRoutes(routeData.routes || []);
        setPendingOrders(routeData.pending_orders || []);
        setWarnings(routeData.warnings || []);
        setPlanId(routeData.plan_id ?? null);
        setIsPlanSaved(routeData.is_saved ?? false);
      }
      refreshUnassignedOrders();
    } catch (error) {
      setStatus('Upload failed. Please try again.');
      setErrors(['Unable to reach the backend service.']);
      setIsValid(false);
      console.error(error);
    } finally {
      setIsProcessing(false);
    }
  };

  const handleAddressChange = (orderId, newAddress) => {
    setEditingAddresses((prev) => ({
      ...prev,
      [orderId]: newAddress,
    }));
  };

  const handleRetrySingleOrder = async (orderId) => {
    if (!hasVehicles) {
      setWarnings(['No vehicles configured - set at least one car or bike before retrying, otherwise every order (including already-fixed ones) will drop out of the routes panel.']);
      return;
    }

    setRetryingOrderId(orderId);
    const idStr = String(orderId);
    setRetryFeedback((prev) => {
      const next = { ...prev };
      delete next[idStr];
      return next;
    });
    const updatedAddress = editingAddresses[orderId] || '';

    try {
      const response = await apiFetch('/api/geocode/retry', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          order_id: String(orderId),
          updated_address: updatedAddress,
          orders: orders,
          available_cars: cars,
          available_bikes: bikes,
          batch_id: batchId,
        }),
      });

      const data = await response.json();
      if (data.orders) {
        setOrders(data.orders);
        setSuccessfulOrders(data.orders.filter((o) => o.lat != null && o.lng != null));

        // Clear, honest feedback on exactly this order's outcome - not
        // just "it worked" but what Google actually matched, or why it
        // still didn't.
        const updatedOrder = data.orders.find((o) => String(o.order_id) === idStr);
        if (updatedOrder) {
          if (updatedOrder.lat != null && updatedOrder.lng != null) {
            setRetryFeedback((prev) => ({
              ...prev,
              [idStr]: {
                status: 'success',
                message: 'Matched — this order now has a route.',
                geocodedAddress: updatedOrder.geocoded_address || null,
                confidence: updatedOrder.confidence ?? null,
              },
            }));
          } else {
            setRetryFeedback((prev) => ({
              ...prev,
              [idStr]: {
                status: 'error',
                message: updatedOrder.geocode_error || 'Still could not be geocoded.',
              },
            }));
          }
        }
      }
      if (data.failed_orders) {
        setFailedOrders(data.failed_orders);
        const newEdits = { ...editingAddresses };
        data.failed_orders.forEach((o) => {
          if (!newEdits[o.order_id]) newEdits[o.order_id] = o.address;
        });
        setEditingAddresses(newEdits);
      }
      if (data.routes) setRoutes(data.routes);
      if (data.pending_orders) setPendingOrders(data.pending_orders);
      setWarnings(data.warnings || []);
      setStatus(data.message || 'Retry completed');
      setEditedRoutes([]);
      setPlanId(data.plan_id ?? null);
      setIsPlanSaved(data.is_saved ?? false);
      refreshUnassignedOrders();
    } catch (err) {
      console.error('Retry failed:', err);
      alert('Retry request failed. Please check backend connection.');
    } finally {
      setRetryingOrderId(null);
    }
  };

  // Recompute routes with the current car/bike counts, using the orders
  // already geocoded - no re-upload needed.
  const handleRegenerateRoutes = async () => {
    if (!hasVehicles) {
      setWarnings(['No vehicles configured - set at least one car or bike, then regenerate.']);
      return;
    }
    if (successfulOrders.length === 0) {
      setWarnings(['No geocoded orders to route yet - upload a manifest first.']);
      return;
    }
    // Regenerate re-solves the whole draft from scratch and will discard any
    // manually added/removed/reordered/created routes made since the last
    // generate - warn before silently throwing that work away.
    if (editedRoutes.length > 0 && !window.confirm(
      'Regenerating will discard your manual route edits (moved, reordered, or newly created routes) and rebuild everything from scratch. Continue?'
    )) {
      return;
    }

    setIsRegenerating(true);
    try {
      const response = await apiFetch('/api/routes/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          orders: successfulOrders,
          available_cars: cars,
          available_bikes: bikes,
          batch_id: batchId,
        }),
      });

      const data = await response.json();
      setRoutes(data.routes || []);
      setPendingOrders(data.pending_orders || []);
      setWarnings(data.warnings || []);
      setEditedRoutes([]);
      setPlanId(data.plan_id ?? null);
      setIsPlanSaved(data.is_saved ?? false);
      setStatus('Routes regenerated with the current fleet.');
      refreshUnassignedOrders();
    } catch (err) {
      console.error('Regenerate failed:', err);
      setWarnings(['Unable to reach the backend to regenerate routes.']);
    } finally {
      setIsRegenerating(false);
    }
  };

  const markRoutesEdited = (...routeNames) => {
    setEditedRoutes((prev) => {
      const next = new Set(prev);
      routeNames.forEach((name) => {
        if (name && name !== 'pending' && name !== 'new-car' && name !== 'new-bike') next.add(name);
      });
      return Array.from(next);
    });
  };

  // Every route/order mutation below follows the same pattern: call the
  // backend, wait for the confirmed updated object(s), then patch local
  // state from *that response* - never from an optimistic guess left
  // uncorrected. Nothing is removed from Unassigned Orders, added to a
  // route, etc. in the UI until the backend has actually confirmed it.
  const patchRouteInState = (updatedRoute) => {
    setRoutes((prev) => {
      const exists = prev.some((r) => r.route_name === updatedRoute.route_name);
      return exists
        ? prev.map((r) => (r.route_name === updatedRoute.route_name ? updatedRoute : r))
        : [...prev, updatedRoute];
    });
  };

  // FastAPI error bodies aren't always a plain string - a 422 validation
  // error's `detail` is a list of {msg, loc, ...} objects, not text. Always
  // returns a plain, readable string so a warning never renders as
  // "[object Object]" (React stringifies a non-string child via
  // Object.prototype.toString, not JSON.stringify).
  const describeErrorDetail = (detail, fallback) => {
    if (!detail) return fallback;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
      const messages = detail.map((d) => (typeof d === 'string' ? d : d?.msg)).filter(Boolean);
      return messages.length ? messages.join('; ') : fallback;
    }
    if (typeof detail === 'object') return detail.msg || fallback;
    return fallback;
  };

  const parseErrorDetail = async (response, fallback) => {
    try {
      const body = await response.json();
      return describeErrorDetail(body.detail, fallback);
    } catch {
      return fallback;
    }
  };

  const postJson = (endpoint, method, body) => apiFetch(endpoint, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });

  // Manually move one stop between routes, into Unassigned Orders, or out
  // of Unassigned Orders into an existing / brand-new route. Every branch
  // calls the real backend (app/main.py's manual-editing endpoints) and
  // reconciles from the confirmed response - see brief §5's synchronization
  // rules. On failure, nothing optimistic is left applied.
  const handleReassignOrder = async (orderId, fromKey, toKey) => {
    if (!toKey || fromKey === toKey) return;
    const idStr = String(orderId);

    try {
      if (fromKey !== 'pending') {
        const fromRoute = routes.find((r) => r.route_name === fromKey);
        if (!fromRoute) return;
        const res = await apiFetch(`/api/routes/${fromRoute.route_id}/orders/${idStr}`, { method: 'DELETE' });
        if (!res.ok) throw new Error(await parseErrorDetail(res, 'Unable to remove the delivery from its route. Please try again.'));
        const data = await res.json();
        patchRouteInState(data.route);
        if (data.order) setPendingOrders((prev) => [data.order, ...prev.filter((o) => String(o.order_id) !== idStr)]);
        markRoutesEdited(fromKey);
      }

      if (toKey === 'pending') {
        setStatus(`Moved order #${orderId} to Unassigned Orders.`);
        return;
      }

      if (toKey === 'new-car' || toKey === 'new-bike') {
        const vehicleType = toKey === 'new-car' ? 'car' : 'bike';
        const res = await postJson('/api/routes', 'POST', { vehicle_type: vehicleType, order_ids: [idStr] });
        if (!res.ok) throw new Error(await parseErrorDetail(res, 'Unable to create the new route. Please try again.'));
        const data = await res.json();
        patchRouteInState(data.route);
        setPendingOrders((prev) => prev.filter((o) => String(o.order_id) !== idStr));
        markRoutesEdited(fromKey, data.route.route_name);
        setStatus(`Started ${data.route.route_name} for order #${orderId}.`);
        return;
      }

      const targetRoute = routes.find((r) => r.route_name === toKey);
      if (!targetRoute) return;
      const res = await postJson(`/api/routes/${targetRoute.route_id}/orders`, 'POST', { order_ids: [idStr] });
      if (!res.ok) throw new Error(await parseErrorDetail(res, 'Unable to add the delivery to that route. Please try again.'));
      const data = await res.json();
      patchRouteInState(data.route);
      setPendingOrders((prev) => prev.filter((o) => String(o.order_id) !== idStr));
      markRoutesEdited(fromKey, toKey);
      setStatus(`Moved order #${orderId} to ${toKey}.`);
    } catch (err) {
      console.error('Reassign failed:', err);
      setWarnings([err.message || 'Unable to update the route. Please try again.']);
    }
  };

  // Persists a reorder (drag-and-drop, or the up/down buttons) via
  // PATCH /api/routes/:id/reorder and reconciles from the confirmed
  // response - sequence letters, ETAs, distance/time and the Excel export
  // all derive from this same server-confirmed order.
  const persistReorder = async (route, newOrderIds) => {
    try {
      const res = await postJson(`/api/routes/${route.route_id}/reorder`, 'PATCH', { order_ids: newOrderIds });
      if (!res.ok) throw new Error(await parseErrorDetail(res, 'Unable to reorder this route. Please try again.'));
      const data = await res.json();
      patchRouteInState(data.route);
      markRoutesEdited(route.route_name);
    } catch (err) {
      console.error('Reorder failed:', err);
      setWarnings([err.message || 'Unable to reorder this route. Please try again.']);
    }
  };

  // "Add Address from Another Route" - a move that stays within the
  // destination's normal capacity (6 for a car, 3 for a bike) is
  // unrestricted; a move that would push it past that is the one
  // deliberate admin override - exactly one delivery point, and only once
  // per route (see route_service.MANUAL_OVERRIDE_EXTRA / crud.
  // move_orders_between_routes). Both routes come back already recomputed
  // (distance/duration/ETA/sequence, resequenced to fit the moved stop in
  // properly, not appended) from the backend, so there's no separate
  // "recreate this route" step - the updated route is just what's shown
  // the moment this resolves.
  const [isMovingAddresses, setIsMovingAddresses] = useState(false);
  const handleMoveOrdersBetweenRoutes = async (sourceRouteId, targetRouteId, orderIds) => {
    setIsMovingAddresses(true);
    try {
      const res = await postJson(`/api/routes/${targetRouteId}/orders/move`, 'POST', {
        source_route_id: sourceRouteId,
        order_ids: orderIds,
      });
      if (!res.ok) throw new Error(await parseErrorDetail(res, 'Unable to add these addresses to the route. Please try again.'));
      const data = await res.json();
      patchRouteInState(data.source_route);
      patchRouteInState(data.target_route);
      markRoutesEdited(data.source_route.route_name, data.target_route.route_name);
      setStatus(`Added ${orderIds.length} address${orderIds.length === 1 ? '' : 'es'} from ${data.source_route.route_name} to ${data.target_route.route_name}. Route re-organized.`);
      return true;
    } catch (err) {
      console.error('Move addresses failed:', err);
      setWarnings([err.message || 'Unable to add these addresses to the route. Please try again.']);
      return false;
    } finally {
      setIsMovingAddresses(false);
    }
  };

  // Undo for the manual override - removes the one stop that used it back
  // to Unassigned (same as any ordinary stop removal) and frees the
  // override up again on this route (route.manual_extra_order_id comes
  // back null in the confirmed response).
  const [isRemovingManualExtra, setIsRemovingManualExtra] = useState(false);
  const handleRemoveManualExtra = async (routeId) => {
    setIsRemovingManualExtra(true);
    try {
      const res = await apiFetch(`/api/routes/${routeId}/manual-extra`, { method: 'DELETE' });
      if (!res.ok) throw new Error(await parseErrorDetail(res, 'Unable to undo the manually-added delivery. Please try again.'));
      const data = await res.json();
      patchRouteInState(data.route);
      if (data.order) setPendingOrders((prev) => [data.order, ...prev.filter((o) => String(o.order_id) !== String(data.order.order_id))]);
      markRoutesEdited(data.route.route_name);
      setStatus(`Removed the manually-added delivery from ${data.route.route_name} - the override is available again.`);
      return true;
    } catch (err) {
      console.error('Undo manual override failed:', err);
      setWarnings([err.message || 'Unable to undo the manually-added delivery. Please try again.']);
      return false;
    } finally {
      setIsRemovingManualExtra(false);
    }
  };

  // Move a stop earlier/later in its own route's delivery sequence.
  const handleReorderStop = (routeName, orderId, direction) => {
    const route = routes.find((r) => r.route_name === routeName);
    if (!route) return;
    const idStr = String(orderId);
    const idx = route.orders.findIndex((o) => String(o.order_id) === idStr);
    const targetIdx = direction === 'up' ? idx - 1 : idx + 1;
    if (idx === -1 || targetIdx < 0 || targetIdx >= route.orders.length) return;
    const reordered = [...route.orders];
    [reordered[idx], reordered[targetIdx]] = [reordered[targetIdx], reordered[idx]];
    persistReorder(route, reordered.map((o) => o.order_id));
  };

  // Drag-and-drop reorder within a route (native HTML5 DnD - no extra
  // dependency). Cross-route drags aren't handled here; use "Move to…" for
  // that, which goes through handleReassignOrder instead.
  const dragStopRef = useRef(null);
  const handleStopDragStart = (routeName, orderId) => {
    dragStopRef.current = { routeName, orderId: String(orderId) };
  };
  const handleStopDrop = (route, targetOrderId) => {
    const drag = dragStopRef.current;
    dragStopRef.current = null;
    if (!drag || drag.routeName !== route.route_name) return;
    const ids = route.orders.map((o) => String(o.order_id));
    const fromIdx = ids.indexOf(drag.orderId);
    const toIdx = ids.indexOf(String(targetOrderId));
    if (fromIdx === -1 || toIdx === -1 || fromIdx === toIdx) return;
    const reordered = [...route.orders];
    const [moved] = reordered.splice(fromIdx, 1);
    reordered.splice(toIdx, 0, moved);
    persistReorder(route, reordered.map((o) => o.order_id));
  };

  // Manual "Add Route" - spins up an empty route with a chosen vehicle type,
  // for a mid-day isolated order or extra on-demand rider (brief §17).
  const [isCreatingRoute, setIsCreatingRoute] = useState(false);
  const handleCreateRoute = async (vehicleType) => {
    setIsCreatingRoute(true);
    try {
      const res = await postJson('/api/routes', 'POST', { vehicle_type: vehicleType, order_ids: [] });
      if (!res.ok) throw new Error(await parseErrorDetail(res, 'Unable to create a new route. Please try again.'));
      const data = await res.json();
      patchRouteInState(data.route);
      setStatus(`Created ${data.route.route_name} (${vehicleType}). Assign deliveries to it from Unassigned Orders.`);
    } catch (err) {
      console.error('Create route failed:', err);
      setWarnings([err.message || 'Unable to create a new route. Please try again.']);
    } finally {
      setIsCreatingRoute(false);
    }
  };

  // Manual "type an address in by hand" - a phone-in order or a customer
  // added after the day's upload, with no Excel row behind it. The backend
  // geocodes it, then either slots it into an existing route already
  // covering the same area (if one has room) or creates a new route
  // (vehicleType, used only as that fallback) - see crud.add_manual_address.
  const [isAddingManualAddress, setIsAddingManualAddress] = useState(false);
  const handleAddManualAddress = async ({ address, customerName, deliveryTime, vehicleType }) => {
    setIsAddingManualAddress(true);
    try {
      const res = await postJson('/api/routes/manual-address', 'POST', {
        address, customer_name: customerName || null, delivery_time: deliveryTime || null, vehicle_type: vehicleType,
      });
      if (!res.ok) throw new Error(await parseErrorDetail(res, 'Could not add this address. Please try again.'));
      const data = await res.json();
      patchRouteInState(data.route);
      setTotalOrders((t) => t + 1);
      // The new address always lands directly on a route (see
      // crud.add_manual_address) - never in Unassigned - but every other
      // mutation in this file re-syncs pendingOrders from the server after
      // it writes, and this was the one exception. Harmless when nothing
      // changed there; guards against it ever drifting out of sync with
      // what the backend actually did.
      refreshUnassignedOrders();
      setStatus(
        data.created_new_route
          ? `Created ${data.route.route_name} for this address (no existing route with room in that area).`
          : `Added to ${data.route.route_name} - already serving ${data.matched_area || 'this area'}.`
      );
      return true;
    } catch (err) {
      console.error('Add manual address failed:', err);
      setWarnings([err.message || 'Could not add this address. Please try again.']);
      return false;
    } finally {
      setIsAddingManualAddress(false);
    }
  };

  // "Adjust Location" - an admin drags a pin to the customer's real
  // delivery point when the geocoded one looks wrong (Failed Orders ticket
  // panel below). Marks the order manually-verified server-side (crud.
  // set_manual_location) - never silently overwritten by a future
  // auto-geocode again (see app/main.py's retry_single_geocode guard).
  const [adjustLocationOrderId, setAdjustLocationOrderId] = useState(null);
  const [isSettingManualLocation, setIsSettingManualLocation] = useState(false);
  const handleSetManualLocation = async (orderId, lat, lng) => {
    setIsSettingManualLocation(true);
    try {
      const res = await postJson(`/api/orders/${encodeURIComponent(orderId)}/location`, 'PATCH', { lat, lng });
      if (!res.ok) throw new Error(await parseErrorDetail(res, 'Could not save this location. Please try again.'));
      const data = await res.json();
      const updatedOrder = data.order;
      const idStr = String(orderId);

      // Resolved - it now has a real, manually-verified location, so it
      // comes out of the Failed Addresses queue.
      setFailedOrders((prev) => prev.filter((o) => String(o.order_id) !== idStr));
      setOrders((prev) => prev.map((o) => (String(o.order_id) === idStr ? { ...o, ...updatedOrder } : o)));
      setSuccessfulOrders((prev) => {
        const exists = prev.some((o) => String(o.order_id) === idStr);
        return exists
          ? prev.map((o) => (String(o.order_id) === idStr ? { ...o, ...updatedOrder } : o))
          : [...prev, updatedOrder];
      });
      setStatus(`Location for order #${orderId} saved - marked as manually verified.`);
      return true;
    } catch (err) {
      console.error('Set manual location failed:', err);
      setWarnings([err.message || 'Could not save this location. Please try again.']);
      return false;
    } finally {
      setIsSettingManualLocation(false);
    }
  };

  // Toggles a route's vehicle type (car <-> bike). Rejected server-side (and
  // the button disabled client-side) if the route currently carries more
  // stops than the new type's capacity.
  const [isChangingVehicle, setIsChangingVehicle] = useState(null);
  const handleToggleVehicleType = async (route) => {
    const nextType = route.vehicle_type === 'car' ? 'bike' : 'car';
    if (nextType === 'bike' && route.orders.length > BIKE_CAPACITY) {
      setWarnings([`${route.route_name} has ${route.orders.length} deliveries - more than a bike's capacity of ${BIKE_CAPACITY}. Remove some deliveries first.`]);
      return;
    }
    setIsChangingVehicle(route.route_name);
    try {
      const res = await postJson(`/api/routes/${route.route_id}/vehicle`, 'PATCH', { vehicle_type: nextType });
      if (!res.ok) throw new Error(await parseErrorDetail(res, 'Unable to change this route’s vehicle. Please try again.'));
      const data = await res.json();
      patchRouteInState(data.route);
      setStatus(`${data.route.route_name} switched to ${nextType}.`);
    } catch (err) {
      console.error('Vehicle change failed:', err);
      setWarnings([err.message || 'Unable to change this route’s vehicle. Please try again.']);
    } finally {
      setIsChangingVehicle(null);
    }
  };

  // Assign one or more unassigned orders straight to an existing route
  // (single-order picker, or the Unassigned Orders panel's bulk-select).
  // Capacity is validated atomically server-side for the whole batch - see
  // add_orders_to_route in crud.py - so a batch that doesn't fit is
  // rejected as a whole, never partially applied.
  const handleAssignUnassignedOrders = async (orderIds, targetRouteName) => {
    const targetRoute = routes.find((r) => r.route_name === targetRouteName);
    if (!targetRoute) return;
    const ids = orderIds.map(String);
    try {
      const res = await postJson(`/api/routes/${targetRoute.route_id}/orders`, 'POST', { order_ids: ids });
      if (!res.ok) throw new Error(await parseErrorDetail(res, 'Unable to assign these orders. Please try again.'));
      const data = await res.json();
      patchRouteInState(data.route);
      setPendingOrders((prev) => prev.filter((o) => !ids.includes(String(o.order_id))));
      setStatus(ids.length === 1
        ? `Assigned order #${ids[0]} to ${targetRouteName}.`
        : `Assigned ${ids.length} orders to ${targetRouteName}.`);
    } catch (err) {
      console.error('Assign failed:', err);
      setWarnings([err.message || 'Unable to assign these orders. Please try again.']);
    }
  };
  const handleAssignUnassignedOrder = (orderId, targetRouteName) => handleAssignUnassignedOrders([orderId], targetRouteName);

  // Removes one order from a route back to Unassigned Orders - the
  // confirmation prompt lives in the RouteWorkspace UI; this just does the
  // call once confirmed.
  const handleRemoveFromRoute = (orderId, routeName) => handleReassignOrder(orderId, routeName, 'pending');

  // Deletes a route outright (not the whole plan). Every order on it comes
  // back as Unassigned - never deleted - which is exactly what the backend
  // guarantees atomically; this just applies the confirmed response.
  const [isDeletingRoute, setIsDeletingRoute] = useState(null);
  const handleDeleteRoute = async (route) => {
    setIsDeletingRoute(route.route_name);
    try {
      const res = await apiFetch(`/api/route/${route.route_id}`, { method: 'DELETE' });
      if (!res.ok) throw new Error(await parseErrorDetail(res, 'Unable to delete this route. Please try again.'));
      const data = await res.json();
      setRoutes((prev) => prev.filter((r) => r.route_name !== route.route_name));
      if (data.freed_orders?.length) {
        setPendingOrders((prev) => [...data.freed_orders, ...prev]);
      }
      setStatus(`${route.route_name} deleted. ${data.freed_orders?.length || 0} order(s) moved to Unassigned Orders.`);
    } catch (err) {
      console.error('Delete route failed:', err);
      setWarnings([err.message || 'Unable to delete this route. Please try again.']);
    } finally {
      setIsDeletingRoute(null);
    }
  };

  // Mirrors _sanitize_address_for_link in the backend's route_service.py -
  // many uploaded addresses keep literal line breaks on purpose (readable
  // multi-line cells in Excel), but a raw newline inside a Maps query
  // string breaks Google's address parser and produces "we can't find that
  // place" instead of navigating anywhere. Only whitespace is collapsed -
  // not one word of the address changes.
  const sanitizeAddressForLink = (address) => String(address || '').replace(/\s+/g, ' ').trim();

  // Mirrors the backend's build_google_maps_url: a lat/lng pair always
  // resolves in Google Maps (a literal point, no address parsing), so it's
  // preferred whenever the order has coordinates - for a successfully
  // geocoded order that IS the address, just in a form that can't fail
  // with "we can't find that place" the way free-text search sometimes
  // does even after whitespace cleanup. Address text is only the fallback
  // for orders with no coordinates at all. Pure client-side string
  // building either way - no geocoding, no API call, no extra request.
  const buildStopMapsLink = (order) => {
    if (order?.lat != null && order?.lng != null) {
      return `https://www.google.com/maps/dir/?api=1&destination=${order.lat},${order.lng}`;
    }
    const clean = sanitizeAddressForLink(order?.address);
    return clean ? `https://www.google.com/maps/dir/?api=1&destination=${encodeURIComponent(clean)}` : '';
  };

  // A phone number column in the source Excel could be labeled a few
  // different ways ("CONTACT NUMBER" is what real uploads use, but the
  // upload's header text is whatever the business typed) - kept alongside
  // the address/location under its original header in extra_fields, same
  // as crud.resolve_location does for Location on the backend.
  const PHONE_FIELD_NAMES = ['phone', 'contact number', 'contact', 'mobile', 'mobile number', 'phone number'];
  const resolveOrderPhone = (order) => {
    const extra = order?.extra_fields || {};
    for (const label of Object.keys(extra)) {
      if (PHONE_FIELD_NAMES.includes(String(label).trim().toLowerCase())) {
        const value = extra[label];
        if (value !== null && value !== undefined && String(value).trim()) return String(value).trim();
      }
    }
    return '';
  };

  // Palette mirrors App.css's --btn-fill / --ink / --good / --critical so the
  // sheet reads as the same product, not a plain default-Excel export.
  // ARGB (exceljs requires the leading alpha channel - FF = fully opaque).
  const XLSX_VIOLET = 'FF5B7FFF'; // brand accent blue, kept the historical name to avoid touching every call site below
  const XLSX_VIOLET_DARK = 'FF3D5FE0';
  const XLSX_ZEBRA = 'FFF3F1FA';
  const XLSX_WHITE = 'FFFFFFFF';
  const XLSX_INK = 'FF1E1B2E';
  const XLSX_GOOD = 'FF059669';
  const XLSX_CRITICAL = 'FFDC2626';
  const XLSX_SIGNAL = 'FFD97706';
  const XLSX_COL_COUNT = 15;
  const XLSX_THIN_BORDER_SIDE = { style: 'thin', color: { argb: 'FFE2DFF0' } };
  const XLSX_THIN_BORDER = {
    top: XLSX_THIN_BORDER_SIDE, bottom: XLSX_THIN_BORDER_SIDE,
    left: XLSX_THIN_BORDER_SIDE, right: XLSX_THIN_BORDER_SIDE,
  };

  const triggerBrowserDownload = (buffer, filename) => {
    const blob = new Blob([buffer], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  };

  // Adds one route's worksheet (title bar, summary, stop table with Maps
  // hyperlinks) to an existing workbook - shared by the single-route
  // download and the "Download all" workbook so both look identical.
  // usedSheetNames tracks names already taken in this workbook so two
  // same-named routes (rare, but manual routes can collide) don't clobber
  // each other.
  const addRouteWorksheet = (workbook, route, usedSheetNames) => {
    // Sheet names can't hold /\?*[]: or exceed 31 chars.
    let sheetName = route.route_name.replace(/[/\\?*[\]:]/g, ' ').slice(0, 31) || 'Route';
    if (usedSheetNames.has(sheetName)) {
      let suffix = 2;
      while (usedSheetNames.has(`${sheetName.slice(0, 28)} ${suffix}`)) suffix += 1;
      sheetName = `${sheetName.slice(0, 28)} ${suffix}`;
    }
    usedSheetNames.add(sheetName);

    const sheet = workbook.addWorksheet(sheetName, {
      views: [{ showGridLines: false }],
    });

    // Stop # / Order ID / Customer / Phone / Address / Location / Delivery
    // Slot / ETA / Status / Delivered / Route / Vehicle / Latitude / Longitude / Maps.
    sheet.columns = [
      { width: 8 }, { width: 12 }, { width: 20 }, { width: 14 }, { width: 38 },
      { width: 16 }, { width: 14 }, { width: 10 }, { width: 10 }, { width: 11 },
      { width: 12 }, { width: 10 }, { width: 12 }, { width: 12 }, { width: 20 },
    ];

    // --- Title bar ---
    sheet.mergeCells(1, 1, 1, XLSX_COL_COUNT);
    const titleCell = sheet.getCell(1, 1);
    titleCell.value = `${route.route_name} — Delivery Route Sheet`;
    titleCell.font = { bold: true, size: 14, color: { argb: XLSX_WHITE } };
    titleCell.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: XLSX_VIOLET } };
    titleCell.alignment = { horizontal: 'left', vertical: 'middle' };
    sheet.getRow(1).height = 26;
    for (let c = 1; c <= XLSX_COL_COUNT; c++) {
      sheet.getCell(1, c).fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: XLSX_VIOLET } };
    }

    const sectionHeader = (rowNum, label) => {
      sheet.mergeCells(rowNum, 1, rowNum, XLSX_COL_COUNT);
      const cell = sheet.getCell(rowNum, 1);
      cell.value = label;
      cell.font = { bold: true, size: 10, color: { argb: XLSX_VIOLET_DARK } };
      cell.alignment = { horizontal: 'left', vertical: 'middle' };
      cell.border = { bottom: { style: 'medium', color: { argb: XLSX_VIOLET } } };
    };

    // --- Summary section ---
    sectionHeader(3, 'ROUTE SUMMARY');
    const summaryRows = [
      ['Vehicle', route.vehicle_type === 'car' ? 'Car' : 'Bike'],
      ['Road Distance (km)', route.route_distance_km ?? '—'],
      ['Travel Time (min)', route.route_time_minutes ?? '—'],
      ['Finish ETA', route.estimated_finish_time ?? '—'],
      ['Utilization (%)', route.utilization_percent ?? '—'],
    ];
    summaryRows.forEach(([label, value], idx) => {
      const rowNum = 4 + idx;
      const labelCell = sheet.getCell(rowNum, 1);
      labelCell.value = label;
      labelCell.font = { bold: true, color: { argb: XLSX_INK } };
      labelCell.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: XLSX_ZEBRA } };
      const valueCell = sheet.getCell(rowNum, 2);
      valueCell.value = value;
      valueCell.font = { color: { argb: XLSX_INK } };
    });

    const fullRouteRow = 9;
    const fullRouteLabelCell = sheet.getCell(fullRouteRow, 1);
    fullRouteLabelCell.value = 'Full Route (Google Maps)';
    fullRouteLabelCell.font = { bold: true, color: { argb: XLSX_INK } };
    fullRouteLabelCell.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: XLSX_ZEBRA } };
    const fullRouteValueCell = sheet.getCell(fullRouteRow, 2);
    if (route.google_maps_url) {
      fullRouteValueCell.value = { text: 'Open full route →', hyperlink: route.google_maps_url, tooltip: 'Open full route in Google Maps' };
      fullRouteValueCell.font = { color: { argb: XLSX_VIOLET }, underline: true, bold: true };
    } else {
      fullRouteValueCell.value = '—';
      fullRouteValueCell.font = { color: { argb: XLSX_INK } };
    }

    // --- Stops table ---
    sectionHeader(11, 'DELIVERY STOPS');
    const HEADER_ROW = 12;
    const headers = [
      'Stop #', 'Order ID', 'Customer', 'Phone', 'Address', 'Location',
      'Delivery Slot', 'ETA', 'Status', 'Delivered', 'Route', 'Vehicle', 'Latitude', 'Longitude', 'Google Maps',
    ];
    headers.forEach((label, c) => {
      const cell = sheet.getCell(HEADER_ROW, c + 1);
      cell.value = label;
      cell.font = { bold: true, size: 10, color: { argb: XLSX_WHITE } };
      cell.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: XLSX_VIOLET } };
      cell.alignment = { horizontal: 'center', vertical: 'middle' };
      cell.border = XLSX_THIN_BORDER;
    });

    route.orders.forEach((order, idx) => {
      const rowNum = HEADER_ROW + 1 + idx;
      const zebra = idx % 2 === 1 ? XLSX_ZEBRA : XLSX_WHITE;
      const values = [
        idx + 1,
        order.order_id,
        order.customer_name,
        resolveOrderPhone(order) || '—',
        order.address,
        // order.area is the backend's resolved Location - the uploaded
        // file's own LOCATION column when it had one (crud.resolve_location),
        // never a value invented here.
        order.area || '—',
        order.delivery_time,
        order.eta || '—',
        order.is_late ? 'LATE' : 'On time',
        order.is_delivered ? 'Delivered' : 'Pending',
        route.route_name,
        route.vehicle_type === 'car' ? 'Car' : 'Bike',
        order.lat != null ? order.lat : '—',
        order.lng != null ? order.lng : '—',
      ];
      values.forEach((value, c) => {
        const cell = sheet.getCell(rowNum, c + 1);
        cell.value = value;
        cell.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: zebra } };
        cell.alignment = { horizontal: c === 0 || c >= 12 ? 'center' : 'left', vertical: 'middle' };
        cell.border = XLSX_THIN_BORDER;
        cell.font = { color: { argb: XLSX_INK } };
      });
      sheet.getCell(rowNum, 9).font = { bold: true, color: { argb: order.is_late ? XLSX_CRITICAL : XLSX_GOOD } };
      sheet.getCell(rowNum, 10).font = { bold: true, color: { argb: order.is_delivered ? XLSX_GOOD : XLSX_INK } };

      // order.map_link is the backend's precise single-pin link (lat/lng
      // when geocoded - always resolves exactly, no address parsing);
      // buildStopMapsLink is only a client-side fallback for an order that
      // predates that field (e.g. a route plan generated before this was
      // added and not yet re-saved).
      const mapsCell = sheet.getCell(rowNum, 15);
      mapsCell.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: zebra } };
      mapsCell.border = XLSX_THIN_BORDER;
      mapsCell.alignment = { horizontal: 'left', vertical: 'middle' };
      const mapLink = order.map_link || buildStopMapsLink(order);
      if (mapLink) {
        mapsCell.value = {
          text: 'Open in Maps →',
          hyperlink: mapLink,
          tooltip: `Open ${order.customer_name || order.order_id} in Google Maps`,
        };
        mapsCell.font = { color: { argb: XLSX_VIOLET }, underline: true, bold: true };
      } else {
        mapsCell.value = '—';
        mapsCell.font = { color: { argb: XLSX_INK } };
      }
    });

    return sheet;
  };

  const handleDownloadRoute = async (route) => {
    // exceljs is a genuinely large dependency (it bundles JSZip and more)
    // that's only ever needed at the moment someone actually clicks a
    // download button - loading it eagerly at module scope put its full
    // weight in every visitor's very first page load, whether or not they
    // ever download anything. A dynamic import here defers fetching and
    // parsing it until it's actually about to be used, and the browser
    // caches the chunk after the first download so every one after it is
    // instant.
    const { default: ExcelJS } = await import('exceljs');
    const workbook = new ExcelJS.Workbook();
    addRouteWorksheet(workbook, route, new Set());
    const buffer = await workbook.xlsx.writeBuffer();
    const fileSafeName = route.route_name.replace(/[^a-z0-9]+/gi, '_').replace(/^_+|_+$/g, '');
    triggerBrowserDownload(buffer, `${fileSafeName || 'route'}_delivery_order.xlsx`);
  };

  // One workbook, every route as its own sheet (identical layout to a
  // single-route download) plus an Overview sheet listing every stop
  // across every route - and pending orders, if any - in one place. Same
  // idea as the reference dispatch sheet: an overall tab plus one tab per
  // vehicle/route.
  // Every distinct extra_fields key across the given orders, in the order
  // each was first seen - these are whatever columns the uploaded sheet
  // had beyond order_id/customer_name/address/delivery_time (contact
  // number, amount, payment mode, remarks, box counts, ...), captured on
  // upload instead of dropped. Business columns, dynamically discovered -
  // never hardcoded, since every upload can carry a different set.
  // Used when there's no captured column_order (a route plan generated
  // without an upload, or a session from before this app tracked upload
  // column layout at all) - the 4 fields this app requires, nothing else
  // to fall back on.
  const DEFAULT_COLUMN_ORDER = [
    { label: 'Order ID', field: 'order_id' },
    { label: 'Customer Name', field: 'customer_name' },
    { label: 'Address', field: 'address' },
    { label: 'Delivery Slot', field: 'delivery_time' },
  ];

  const ACCOUNTING_NUMBER_FORMAT = '_(* #,##0.00_);_(* (#,##0.00);_(* "-"??_);_(@_)';
  const businessColumnWidth = (label) => {
    const l = label.toLowerCase();
    if (l.includes('address')) return 55;
    if (l.includes('remarks') || l.includes('extra')) return 36;
    if (l.includes('customer') || l.includes('name')) return 28;
    if (l.includes('contact') || l.includes('number') || l.includes('phone')) return 24;
    if (l.includes('payment')) return 22;
    if (l.includes('location')) return 22;
    if (l.includes('amount')) return 18;
    return Math.max(16, label.length + 6);
  };

  const getCellValue = (row, col) => {
    switch (col.field) {
      case 'order_id': return row.order.order_id;
      case 'customer_name': return row.order.customer_name;
      case 'address': return row.order.address;
      case 'delivery_time': return row.order.delivery_time;
      case '__route': return row.routeName ?? '—';
      case '__vehicle': return row.vehicleLabel ?? '—';
      case '__eta': return row.order.eta || '—';
      case '__status': return row.status;
      default: return row.order.extra_fields?.[col.label] ?? '';
    }
  };

  // One "business sheet" - formatted to match the dispatcher's own
  // order-tracking sheet exactly: bold Calibri title in a boxed merged
  // header bar, bold Calibri column headers (thin left/right borders,
  // centered, wrapped), bold Arial data rows (full thin box border,
  // centered, wrapped), accounting number format on any Amount-style
  // column, no fill colors, no Google Maps column - built from the
  // upload's own captured column_order (exact original headers, exact
  // original order), not a layout this app invented. The Overview tab
  // appends Assigned Route/Vehicle after the upload's own columns (needed
  // there since it covers every route at once); per-route tabs add
  // nothing - the sheet name alone says which route it is.
  const addBusinessSheet = (workbook, sheetName, title, rows, columns) => {
    const sheet = workbook.addWorksheet(sheetName);
    const colCount = columns.length;

    sheet.columns = columns.map((col) => ({ width: businessColumnWidth(col.label) }));

    sheet.mergeCells(1, 1, 1, colCount);
    const titleCell = sheet.getCell(1, 1);
    titleCell.value = title;
    titleCell.font = { name: 'Calibri', size: 16, bold: true };
    titleCell.alignment = { horizontal: 'center', vertical: 'middle' };
    const mediumBorder = { style: 'medium' };
    titleCell.border = { top: mediumBorder, bottom: mediumBorder, left: mediumBorder, right: mediumBorder };
    sheet.getRow(1).height = 30;

    const HEADER_ROW = 2;
    const thinBorder = { style: 'thin' };
    sheet.getRow(HEADER_ROW).height = 34;
    columns.forEach((col, c) => {
      const cell = sheet.getCell(HEADER_ROW, c + 1);
      cell.value = col.label;
      cell.font = { name: 'Calibri', size: 11, bold: true };
      cell.alignment = { horizontal: 'center', vertical: 'middle', wrapText: true };
      cell.border = { left: thinBorder, right: thinBorder };
      if (/amount/i.test(col.label)) cell.numFmt = ACCOUNTING_NUMBER_FORMAT;
    });

    rows.forEach((row, idx) => {
      const rowNum = HEADER_ROW + 1 + idx;
      sheet.getRow(rowNum).height = 60;
      columns.forEach((col, c) => {
        const cell = sheet.getCell(rowNum, c + 1);
        cell.value = getCellValue(row, col);
        // The upload's own delivery-slot column keeps Calibri like its
        // header; every other data cell is bold Arial - matches the
        // reference sheet's own (slightly inconsistent, but real) fonts.
        cell.font = { name: col.field === 'delivery_time' ? 'Calibri' : 'Arial', size: 11, bold: true };
        cell.alignment = { horizontal: 'center', vertical: 'middle', wrapText: true };
        cell.border = { top: thinBorder, bottom: thinBorder, left: thinBorder, right: thinBorder };
        if (/amount/i.test(col.label)) cell.numFmt = ACCOUNTING_NUMBER_FORMAT;
        if (col.field === '__status') {
          cell.font = {
            name: 'Arial', size: 11, bold: true,
            color: { argb: row.isLate ? XLSX_CRITICAL : row.status === 'Not assigned' ? XLSX_SIGNAL : XLSX_GOOD },
          };
        }
      });
    });

    return sheet;
  };

  // One workbook, formatted like the dispatcher's own order sheet: an
  // Overview tab with every order (their exact upload columns, in their
  // exact order and wording), plus one tab per route filtered to that
  // route's stops - mirroring "one tab per vehicle" the same way the
  // reference sheet does. No Google Maps column, no route-sheet styling.
  const handleDownloadAllRoutes = async () => {
    const uploadColumns = columnOrder.length > 0 ? columnOrder : DEFAULT_COLUMN_ORDER;
    const overviewColumns = [...uploadColumns, { label: 'Assigned Route', field: '__route' }, { label: 'Vehicle', field: '__vehicle' }];
    const routeColumns = uploadColumns;

    const toRow = (order, routeName, vehicleLabel) => ({
      order,
      routeName,
      vehicleLabel,
      status: routeName ? (order.is_late ? 'LATE' : 'On time') : 'Not assigned',
      isLate: !!order.is_late,
    });

    const { default: ExcelJS } = await import('exceljs'); // see handleDownloadRoute's comment
    const workbook = new ExcelJS.Workbook();
    const usedSheetNames = new Set();
    const today = new Date().toLocaleDateString(undefined, { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });

    // Overview stays in the upload's own serial-number order, not grouped
    // by route - matches the reference sheet's plain top-to-bottom list.
    const uploadOrderIndex = new Map(orders.map((order, idx) => [String(order.order_id), idx]));
    const overviewRows = [
      ...routes.flatMap((route) => route.orders.map((order) => toRow(order, route.route_name, route.vehicle_type === 'car' ? 'Car' : 'Bike'))),
      ...pendingOrders.map((order) => toRow(order, null, null)),
    ].sort((a, b) => {
      const ia = uploadOrderIndex.get(String(a.order.order_id)) ?? Number.MAX_SAFE_INTEGER;
      const ib = uploadOrderIndex.get(String(b.order.order_id)) ?? Number.MAX_SAFE_INTEGER;
      return ia - ib;
    });
    addBusinessSheet(workbook, 'Overview', `Delivery Orders — Overview (${today})`, overviewRows, overviewColumns);
    usedSheetNames.add('Overview');

    routes.forEach((route) => {
      let sheetName = route.route_name.replace(/[/\\?*[\]:]/g, ' ').slice(0, 31) || 'Route';
      if (usedSheetNames.has(sheetName)) {
        let suffix = 2;
        while (usedSheetNames.has(`${sheetName.slice(0, 28)} ${suffix}`)) suffix += 1;
        sheetName = `${sheetName.slice(0, 28)} ${suffix}`;
      }
      usedSheetNames.add(sheetName);
      const vehicleLabel = route.vehicle_type === 'car' ? 'Car' : 'Bike';
      const rows = route.orders.map((order) => toRow(order, route.route_name, vehicleLabel));
      addBusinessSheet(workbook, sheetName, `${route.route_name} (${vehicleLabel}) — ${today}`, rows, routeColumns);
    });

    const buffer = await workbook.xlsx.writeBuffer();
    const dateSlug = new Date().toISOString().slice(0, 10);
    triggerBrowserDownload(buffer, `optiroute_all_routes_${dateSlug}.xlsx`);
  };

  const handleDownloadAllClick = async () => {
    setIsDownloadingAll(true);
    try {
      await handleDownloadAllRoutes();
    } catch (err) {
      console.error('Download all routes failed:', err);
      showToast('Could not build the workbook - please try again.');
    } finally {
      setIsDownloadingAll(false);
    }
  };

  // Still used by the route cards' "Delivery slot" range.
  const parseSlotMinutes = (slot) => {
    const m = String(slot || '').trim().match(/^(\d{1,2}):(\d{2})\s*([AaPp][Mm])?$/);
    if (!m) return null;
    let hour = parseInt(m[1], 10);
    const minute = parseInt(m[2], 10);
    const meridiem = m[3]?.toLowerCase();
    if (meridiem === 'pm' && hour !== 12) hour += 12;
    if (meridiem === 'am' && hour === 12) hour = 0;
    return hour * 60 + minute;
  };

  const displayedFailedOrders = searchQuery ? failedOrders.filter(matchesSearch) : failedOrders;
  const displayedRoutes = searchQuery
    ? routes.filter((r) => r.route_name.toLowerCase().includes(searchQuery) || r.orders.some(matchesSearch))
    : routes;
  const displayedPendingOrders = searchQuery ? pendingOrders.filter(matchesSearch) : pendingOrders;
  const todayLabel = new Date().toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
  const alertCount = errors.length + warnings.length;

  // The header always said "Dashboard" no matter which section the
  // sidebar had scrolled to - no way to confirm where you actually were.
  // Tied to the same activeNav the sidebar highlights, so the two agree.
  const HEADER_CONTENT = {
    dashboard: { title: 'Dashboard', subtitle: 'Delivery route planning & vehicle assignment' },
    generate: { title: 'Routes', subtitle: `${routes.length} route${routes.length === 1 ? '' : 's'} today` },
    unassigned: { title: 'Unassigned Orders', subtitle: `${pendingOrders.length} order${pendingOrders.length === 1 ? '' : 's'} waiting on assignment` },
    failed: { title: 'Failed Addresses', subtitle: `${failedOrders.length} address${failedOrders.length === 1 ? '' : 'es'} needing attention` },
    drivers: { title: 'Drivers', subtitle: `${drivers.length} driver${drivers.length === 1 ? '' : 's'} on the roster` },
    vehicles: { title: 'Vehicles', subtitle: 'Fleet records - coming soon' },
    'live-tracking': { title: 'Live Tracking', subtitle: 'Every active driver, live, on one map' },
    notifications: { title: 'Notifications', subtitle: 'Alert inbox - coming soon' },
    reports: { title: 'Reports', subtitle: 'Coming soon' },
    analytics: { title: 'Analytics', subtitle: 'Coming soon' },
    settings: { title: 'Settings', subtitle: 'Coming soon' },
  };
  const headerContent = HEADER_CONTENT[activeNav] || HEADER_CONTENT.dashboard;

  // The five stat-row KPIs, computed straight from live state - no
  // simulated history, no placeholder numbers.
  const totalDistanceKm = Math.round(routes.reduce((sum, r) => sum + (r.route_distance_km || 0), 0));
  const avgEtaMinutes = routes.length
    ? Math.round(routes.reduce((sum, r) => sum + (r.route_time_minutes || 0), 0) / routes.length)
    : 0;

  const selectedFailedOrder = failedOrders.find((o) => String(o.order_id) === String(selectedFailedId)) || null;
  const adjustLocationOrder = adjustLocationOrderId == null ? null : (
    failedOrders.find((o) => String(o.order_id) === String(adjustLocationOrderId))
    || orders.find((o) => String(o.order_id) === String(adjustLocationOrderId))
    || { order_id: adjustLocationOrderId }
  );
  const selectedFeedback = selectedFailedId != null ? retryFeedback[String(selectedFailedId)] : null;
  useEffect(() => {
    if (displayedFailedOrders.length === 0) {
      if (selectedFailedId !== null) setSelectedFailedId(null);
      return;
    }
    if (!displayedFailedOrders.some((o) => String(o.order_id) === String(selectedFailedId))) {
      setSelectedFailedId(displayedFailedOrders[0].order_id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [failedOrders, searchQuery]);

  // ⌘K palette content - every entry reuses a handler App() already had
  // (dash-links/quick actions/sidebar nav) for the same action; the
  // palette is a faster way to reach it, not new behavior.
  const paletteQueryNorm = paletteQuery.trim().toLowerCase();
  const paletteMatch = (label, sub) => (
    !paletteQueryNorm
    || label.toLowerCase().includes(paletteQueryNorm)
    || (sub || '').toLowerCase().includes(paletteQueryNorm)
  );

  const paletteActionItems = [
    {
      id: 'act-generate',
      label: successfulOrders.length > 0 ? 'Regenerate routes' : 'Generate routes',
      sub: 'Run route optimization with the current fleet',
      icon: IconRoute,
      disabled: isProcessing || isRegenerating || successfulOrders.length === 0,
      onRun: handleRegenerateRoutes,
    },
    {
      id: 'act-upload',
      label: 'Upload manifest',
      sub: '.xlsx import',
      icon: IconUpload,
      disabled: isProcessing,
      onRun: () => document.getElementById('manifest-input')?.click(),
    },
    ...(routes.length > 0 ? [{
      id: 'act-download',
      label: 'Download all routes',
      sub: 'Export one workbook, every route',
      icon: IconDownload,
      disabled: isDownloadingAll,
      onRun: handleDownloadAllClick,
    }] : []),
    ...(planId != null && !isPlanSaved ? [{
      id: 'act-save',
      label: 'Save to Route History',
      sub: 'Keep this plan',
      icon: IconCheck,
      disabled: isSavingPlan,
      onRun: handleSaveToHistory,
    }] : []),
  ].filter((item) => paletteMatch(item.label, item.sub));

  const paletteNavItems = NAV_ITEMS
    .filter((item) => !item.soon)
    .map((item) => ({ id: `nav-${item.key}`, label: item.label, sub: 'Go to section', icon: item.icon, onRun: () => handleNavClick(item) }))
    .filter((item) => paletteMatch(item.label, item.sub));

  const paletteRouteItems = (paletteQueryNorm
    ? routes.filter((r) => r.route_name.toLowerCase().includes(paletteQueryNorm))
    : routes.slice(0, 6)
  ).slice(0, 8).map((r) => ({
    id: `route-${r.route_name}`,
    label: r.route_name,
    sub: `${r.orders.length} stop${r.orders.length === 1 ? '' : 's'} · ${r.vehicle_type === 'car' ? 'Car' : 'Bike'}`,
    icon: IconRoute,
    onRun: () => handleNavClick(findNavItem('generate')),
  }));

  const paletteDriverItems = (paletteQueryNorm
    ? drivers.filter((d) => (d.name || '').toLowerCase().includes(paletteQueryNorm))
    : []
  ).slice(0, 6).map((d) => ({
    id: `driver-${d.id}`,
    label: d.name,
    sub: d.status === 'active' ? 'Active' : 'Inactive',
    icon: IconUsers,
    onRun: () => handleNavClick(findNavItem('drivers')),
  }));

  const paletteGroups = [
    { label: 'Actions', items: paletteActionItems },
    { label: 'Go to', items: paletteNavItems },
    { label: 'Routes', items: paletteRouteItems },
    { label: 'Drivers', items: paletteDriverItems },
  ];

  // Issues panel's "View"/"Assign"/etc jump - the same nav handler the
  // dash-links already use, optionally also selecting one failed order so
  // the Returns board's master/detail split opens on it.
  const handleIssueJump = (navKey, focusOrderId) => {
    if (focusOrderId != null) setSelectedFailedId(focusOrderId);
    handleNavClick(findNavItem(navKey));
  };

  return (
    <div className="app-shell">
      <div className={`sidebar__scrim${mobileNavOpen ? ' is-visible' : ''}`} onClick={() => setMobileNavOpen(false)} />

      <aside className={`sidebar${sidebarCollapsed ? ' sidebar--collapsed' : ''}${mobileNavOpen ? ' sidebar--mobile-open' : ''}`}>
        <div className="sidebar__brand">
          <img src={arzLogo} alt="ARZ Food Ventures" className="brand-logo brand-logo--light" style={{ height: 28 }} />
          <img src={arzLogoDark} alt="ARZ Food Ventures" className="brand-logo brand-logo--dark" style={{ height: 28 }} />
          <div className="sidebar__brand-text">
            <div className="sidebar__brand-title">OptiRoute</div>
            <div className="sidebar__brand-sub" title="ARZ Food Ventures">ARZ Food Ventures</div>
          </div>
          <button
            className="sidebar__toggle"
            onClick={() => setSidebarCollapsed((c) => !c)}
            title={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            aria-label={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          >
            <IconChevron style={{ transform: 'rotate(90deg)' }} width={15} height={15} />
          </button>
        </div>

        <nav className="sidebar__nav">
          {/* Grouped by what you're trying to do (Dispatch, Fleet, Insights,
              System) rather than by build status - a "Soon" item sits next
              to its real siblings (Vehicles under Fleet with Drivers, Live
              Tracking under Dispatch with Routes) instead of being exiled
              to one catch-all section at the bottom. */}
          {NAV_GROUPS.map((group) => (
            <Fragment key={group.label}>
              <div className="sidebar__section-label">{group.label}</div>
              {group.items.map((item) => (
                item.soon ? (
                  <button
                    key={item.key}
                    className={`nav-item nav-item--soon${activeNav === item.key ? ' nav-item--active' : ''}`}
                    onClick={() => handleSoonClick(item)}
                    title={`${item.label} - coming soon`}
                  >
                    <item.icon width={17} height={17} />
                    <span>{item.label}</span>
                    <span className="nav-item__badge">Soon</span>
                  </button>
                ) : (
                  <button
                    key={item.key}
                    className={`nav-item${activeNav === item.key ? ' nav-item--active' : ''}`}
                    onClick={() => handleNavClick(item)}
                    title={item.label}
                  >
                    <item.icon width={17} height={17} />
                    <span>{item.label}</span>
                  </button>
                )
              ))}
            </Fragment>
          ))}
        </nav>

        <div className="sidebar__footer">
          <button
            className="nav-item"
            onClick={toggleTheme}
            title={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
          >
            {isDark ? <IconSun width={17} height={17} /> : <IconMoon width={17} height={17} />}
            <span>{isDark ? 'Light mode' : 'Dark mode'}</span>
          </button>
        </div>
      </aside>

      <div className="shell-main">
        <header className="topbar">
          <div className="topbar__inner">
            <button
              className="topbar__menu-btn"
              onClick={() => setMobileNavOpen((v) => !v)}
              aria-label="Toggle navigation"
            >
              {mobileNavOpen ? <IconX width={18} height={18} /> : <IconMenu width={18} height={18} />}
            </button>

            <div className="topbar__title">
              <h1>{headerContent.title}</h1>
              <p>{headerContent.subtitle}</p>
            </div>

            <div className="topbar__search">
              <IconSearch width={16} height={16} />
              <input
                type="text"
                placeholder="Search orders, customers, routes…"
                value={searchTerm}
                onChange={(e) => setSearchTerm(e.target.value)}
              />
              {searchTerm && (
                <button className="topbar__search-clear" onClick={() => setSearchTerm('')} aria-label="Clear search">
                  <IconX width={12} height={12} />
                </button>
              )}
            </div>

            <button
              type="button"
              className="topbar__palette-btn"
              onClick={() => { setPaletteOpen(true); setPaletteQuery(''); }}
              title="Command palette"
            >
              <kbd>⌘K</kbd>
            </button>

            <div className="topbar__spacer" />
            <span className="topbar__date">{todayLabel}</span>

            <div className="quick-actions" ref={quickActionsRef}>
              <button
                className="quick-action"
                onClick={() => setQuickActionsOpen((v) => !v)}
                aria-expanded={quickActionsOpen}
                title="Quick actions"
              >
                <IconPlus width={15} height={15} />
                Quick actions
              </button>
              {quickActionsOpen && (
                <div className="quick-actions__menu">
                  <button
                    className="quick-actions__item"
                    onClick={() => { setQuickActionsOpen(false); document.getElementById('manifest-input')?.click(); }}
                    disabled={isProcessing}
                  >
                    <IconUpload width={14} height={14} />
                    Upload manifest
                  </button>
                  <button
                    className="quick-actions__item"
                    onClick={() => { setQuickActionsOpen(false); handleRegenerateRoutes(); }}
                    disabled={isRegenerating || isProcessing || successfulOrders.length === 0}
                  >
                    <IconRefresh width={14} height={14} />
                    Regenerate routes
                  </button>
                </div>
              )}
            </div>

            <button
              className="topbar__icon-btn"
              onClick={() => {
                if (alertCount > 0) document.querySelector('.alert')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
                else showToast('No active alerts.');
              }}
              title="Alerts"
              aria-label="Alerts"
            >
              <IconBell width={17} height={17} />
              {alertCount > 0 && <span className="badge-dot">{alertCount}</span>}
            </button>

            <div className="topbar__account" title="Dispatch Admin">
              <span className="topbar__avatar">DA</span>
              <span className="topbar__account-name">Dispatch Admin</span>
              <IconChevron width={13} height={13} className="topbar__account-chevron" />
            </div>
          </div>
        </header>

        {!routeDetailOpen && (
        <div className="session-tabs" role="tablist" aria-label="Sessions">
          {tabs.map((tab) => (
            <button
              key={tab.key}
              type="button"
              role="tab"
              aria-selected={tab.key === activeTabKey}
              className={`session-tab${tab.key === activeTabKey ? ' session-tab--active' : ''}`}
              onClick={() => handleSwitchTab(tab.key)}
              title={tab.label}
            >
              <span className="session-tab__label">{tab.label}</span>
              {tabs.length > 1 && (
                <span
                  className="session-tab__close"
                  role="button"
                  tabIndex={0}
                  aria-label={`Close ${tab.label}`}
                  onClick={(e) => handleCloseTab(tab.key, e)}
                >
                  <IconX width={11} height={11} />
                </span>
              )}
            </button>
          ))}
          <button
            type="button"
            className="session-tab session-tab--new"
            onClick={handleNewTab}
            title="Open a new session tab - this one stays open"
            aria-label="New session tab"
          >
            <IconPlus width={13} height={13} />
          </button>
        </div>
        )}

      {/* Back to one continuous page: the stat row, shortcuts, Generate
          Routes panel, Routes/Unassigned Orders board, Drivers board, and
          Returns board are all always mounted here in sequence - sidebar
          clicks smooth-scroll down to the relevant section instead of
          swapping the page's content out. The one thing that still hides
          this preamble is routeDetailOpen - drilled into a single route's
          detail page is still meant to read as its own focused view, not
          one more thing under the Dashboard's KPI row. */}
      <div className="console">
        {!routeDetailOpen && (
        <>
          {/* One flat row of compact stat cards */}
          <div className="stat-row">
            <KpiTile variant="orders" icon={IconInbox} value={totalOrders} label="Orders today" onClick={() => handleNavClick(findNavItem('generate'))} />
            <KpiTile variant="bikes" icon={IconBike} value={bikes} label="Bikes available" onClick={() => handleNavClick(findNavItem('generate'))} />
            <KpiTile variant="cars" icon={IconCar} value={cars} label="Cars available" onClick={() => handleNavClick(findNavItem('generate'))} />
            <KpiTile variant="routes" icon={IconRoute} value={routes.length} suffix={hasVehicles ? ` / ${cars + bikes}` : ''} label="Routes today" onClick={() => handleNavClick(findNavItem('generate'))} />
            <KpiTile variant="distance" icon={IconGauge} value={totalDistanceKm} suffix=" km" label="Total distance" onClick={() => handleNavClick(findNavItem('generate'))} />
            <KpiTile variant="eta" icon={IconFlag} value={avgEtaMinutes} suffix=" min" label="Average ETA" onClick={() => handleNavClick(findNavItem('generate'))} />
          </div>

          <IssuesPanel
            failedOrders={failedOrders}
            warnings={warnings}
            pendingOrders={pendingOrders}
            isOpen={issuesOpen}
            onToggle={() => setIssuesOpen((v) => !v)}
            onRetry={handleRetrySingleOrder}
            onJump={handleIssueJump}
            describeErrorDetail={describeErrorDetail}
          />

          <div className="dash-links">
            <button type="button" className="dash-link" onClick={() => handleNavClick(findNavItem('generate'))}>
              <IconRoute width={18} height={18} />
              <div><strong>Routes</strong><span>{fileName ? `${fileName} · ${totalOrders} orders` : 'Generate today’s routes'}</span></div>
            </button>
            <button type="button" className="dash-link" onClick={() => handleNavClick(findNavItem('unassigned'))}>
              <IconInbox width={18} height={18} />
              <div><strong>Unassigned Orders</strong><span>{pendingOrders.length} waiting on assignment</span></div>
            </button>
            <button type="button" className="dash-link" onClick={() => handleNavClick(findNavItem('failed'))}>
              <IconAlert width={18} height={18} />
              <div><strong>Failed Addresses</strong><span>{failedOrders.length} needing attention</span></div>
            </button>
            <button type="button" className="dash-link" onClick={() => handleNavClick(findNavItem('drivers'))}>
              <IconUsers width={18} height={18} />
              <div><strong>Drivers</strong><span>{drivers.length} on the roster</span></div>
            </button>
          </div>
        </>
        )}
        {/* Toolbar, loading state, and manifest alerts - always mounted,
            same as everything else on this continuous page. */}
        {!routeDetailOpen && (
        <>
        <div className="toolbar" id="toolbar-section">
          <div className="toolbar__summary">
            <span className="toolbar__summary-title"><IconRoute width={15} height={15} /> Generate Routes</span>
            <span className="toolbar__summary-meta">
              {fileName || 'No manifest loaded'} · {totalOrders} order{totalOrders === 1 ? '' : 's'} · {cars} car{cars === 1 ? '' : 's'} / {bikes} bike{bikes === 1 ? '' : 's'}
            </span>
          </div>

          <div className="toolbar__row">
            <div className="toolbar__group toolbar__group--fleet">
              <div className={`stepper${!hasVehicles ? ' stepper--alarm' : ''}`}>
                <span className="stepper__label">Cars</span>
                <div className="stepper__control">
                  <button type="button" className="stepper__btn" disabled={isProcessing || cars <= 0} onClick={() => setCars((c) => Math.max(0, c - 1))} aria-label="Decrease cars">−</button>
                  <input
                    id="cars-input"
                    className="stepper__input mono-num"
                    type="number"
                    min="0"
                    value={cars}
                    disabled={isProcessing}
                    onChange={(e) => setCars(Math.max(0, Number(e.target.value) || 0))}
                  />
                  <button type="button" className="stepper__btn" disabled={isProcessing} onClick={() => setCars((c) => c + 1)} aria-label="Increase cars">+</button>
                </div>
              </div>
              <div className={`stepper${!hasVehicles ? ' stepper--alarm' : ''}`}>
                <span className="stepper__label">Bikes</span>
                <div className="stepper__control">
                  <button type="button" className="stepper__btn" disabled={isProcessing || bikes <= 0} onClick={() => setBikes((b) => Math.max(0, b - 1))} aria-label="Decrease bikes">−</button>
                  <input
                    id="bikes-input"
                    className="stepper__input mono-num"
                    type="number"
                    min="0"
                    value={bikes}
                    disabled={isProcessing}
                    onChange={(e) => setBikes(Math.max(0, Number(e.target.value) || 0))}
                  />
                  <button type="button" className="stepper__btn" disabled={isProcessing} onClick={() => setBikes((b) => b + 1)} aria-label="Increase bikes">+</button>
                </div>
              </div>
              {!hasVehicles && (
                <span className="fleet-warning">
                  <IconAlert width={15} height={15} />
                  Set at least one car or bike
                </span>
              )}
            </div>

            <div className="toolbar__divider" aria-hidden="true" />

            <div className="toolbar__group toolbar__group--actions">
              <button
                className="toolbar-btn toolbar-btn--primary"
                onClick={handleRegenerateRoutes}
                disabled={isProcessing || isRegenerating || successfulOrders.length === 0}
                title="Recompute routes with the current car/bike counts - no re-upload needed"
              >
                <IconRefresh width={14} height={14} className={isRegenerating ? 'icon-spin' : ''} />
                {isRegenerating ? 'Regenerating…' : 'Regenerate'}
              </button>

              {/* Everything besides Regenerate - the one thing you come to
                  this panel to actually do - grouped behind one overflow
                  trigger instead of competing with it in a row of
                  equal-weight buttons. */}
              {(routes.length > 0 || planId != null) && (
                <div className="overflow-wrap" ref={toolbarOverflowRef}>
                  <button
                    type="button"
                    className="toolbar-btn toolbar-btn--ghost"
                    onClick={() => setToolbarOverflowOpen((v) => !v)}
                    aria-expanded={toolbarOverflowOpen}
                    title="More actions"
                  >
                    More
                    <IconChevron width={12} height={12} style={{ transform: toolbarOverflowOpen ? 'rotate(-90deg)' : 'rotate(90deg)' }} />
                  </button>
                  {toolbarOverflowOpen && (
                    <div className="overflow-menu">
                      {routes.length > 0 && (
                        <button
                          type="button"
                          className="overflow-menu__item"
                          disabled={isDownloadingAll}
                          onClick={() => { setToolbarOverflowOpen(false); handleDownloadAllClick(); }}
                        >
                          {isDownloadingAll ? <span className="spinner" /> : <IconDownload width={14} height={14} />}
                          {isDownloadingAll ? 'Preparing…' : 'Download all routes'}
                        </button>
                      )}
                      {planId != null && (
                        <button
                          type="button"
                          className="overflow-menu__item"
                          disabled={isSavingPlan || isPlanSaved}
                          onClick={() => { setToolbarOverflowOpen(false); handleSaveToHistory(); }}
                        >
                          {isSavingPlan ? <span className="spinner" /> : <IconCheck width={14} height={14} />}
                          {isPlanSaved ? 'Saved to history' : isSavingPlan ? 'Saving…' : 'Save to history'}
                        </button>
                      )}
                      {planId != null && (
                        <button
                          type="button"
                          className="overflow-menu__item overflow-menu__item--danger"
                          disabled={isDeletingPlan}
                          onClick={() => { setToolbarOverflowOpen(false); handleDeleteCurrentPlan(); }}
                        >
                          {isDeletingPlan ? <span className="spinner" /> : <IconX width={14} height={14} />}
                          {isDeletingPlan ? 'Deleting…' : 'Delete this plan'}
                        </button>
                      )}
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>

          {/* Real drag-and-drop, not decorative - onDrop feeds the same
              handleUpload the hidden file input's onChange does. */}
          <div
            className={`dropzone${isDraggingManifest ? ' dropzone--active' : ''}${isProcessing ? ' dropzone--disabled' : ''}`}
            role="button"
            tabIndex={0}
            aria-label="Load order manifest, .xlsx"
            onClick={() => { if (!isProcessing) document.getElementById('manifest-input')?.click(); }}
            onKeyDown={(e) => { if (e.key === 'Enter' && !isProcessing) document.getElementById('manifest-input')?.click(); }}
            onDragOver={(e) => { e.preventDefault(); if (!isProcessing) setIsDraggingManifest(true); }}
            onDragLeave={() => setIsDraggingManifest(false)}
            onDrop={(e) => {
              e.preventDefault();
              setIsDraggingManifest(false);
              if (isProcessing) return;
              const file = e.dataTransfer.files?.[0];
              if (file) handleUpload(file);
            }}
          >
            <div className="dropzone__icon">
              {isProcessing ? <span className="spinner" /> : <IconUpload width={18} height={18} />}
            </div>
            <div className="dropzone__text">
              <span className="dropzone__title">
                {isProcessing
                  ? `Processing ${fileName || 'manifest'}…`
                  : fileName ? `Loaded: ${fileName}` : 'Drag & drop today\'s manifest, or click to browse'}
              </span>
              <span className="dropzone__sub">
                {isProcessing ? 'Uploading, geocoding and building routes - this can take a moment' : '.xlsx · columns validated on drop'}
              </span>
            </div>
            <input
              id="manifest-input"
              className="dropzone__input"
              type="file"
              accept=".xlsx"
              onChange={(e) => handleUpload(e.target.files?.[0])}
              disabled={isProcessing}
              tabIndex={-1}
            />
          </div>

          <div className="toolbar__status-row">
            <div className="status-readout">
              <span><strong>Status</strong> <span className="status-readout__value">{isRestoringSession ? 'Checking for a saved session…' : status}</span></span>
              {fileName && (
                // The native <input type="file"> can never be made to show a
                // previously-picked file again after a refresh - browsers
                // don't allow JS to set that display for security reasons.
                // This is the real, state-backed record of what's loaded.
                <span title="Restored from Upload History"><strong>File</strong> <span className="status-readout__value">{fileName}</span></span>
              )}
              <span><strong>Orders</strong> <span className="status-readout__value">{totalOrders}</span></span>
            </div>
          </div>

          {/* Alerts live inside the toolbar card itself now, right under
              the Status/File/Orders line they're actually about, instead
              of as their own full-bleed banner floating between the
              toolbar and the routes table below. describeErrorDetail
              guards against ever rendering a raw object/array as a
              message (React stringifies those as "[object Object]") -
              every entry here is guaranteed plain text by the time it
              lands in errors/warnings state, but this is the last line
              of defense right at render time too. */}
          {errors.length > 0 && (
            <div className="alert alert--error">
              <IconAlert />
              <div>{errors.map((err, idx) => <p key={idx}>{describeErrorDetail(err, 'Something went wrong.')}</p>)}</div>
            </div>
          )}

          {warnings.length > 0 && (
            <div className="alert alert--warn">
              <IconAlert />
              <div>{warnings.map((warning, idx) => <p key={idx}>{describeErrorDetail(warning, 'Something needs your attention.')}</p>)}</div>
            </div>
          )}

          {/* The overflow menu these three actions live in closes itself
              the instant one is clicked, so its own spinner (above) is
              never actually seen - this is the visible "still working"
              feedback for all three. */}
          {(isDownloadingAll || isSavingPlan || isDeletingPlan) && (
            <div className="busy-overlay">
              <span className="spinner" />
            </div>
          )}
        </div>

        {/* Loading */}
        {isProcessing && (
          <div className="loading-console">
            <div className="rp-scene">
              <span className="rp-dust rp-dust--car"></span>
              <span className="rp-dust rp-dust--car"></span>
              <div className="rp-convoy">
                <div className="rp-car">
                  <div className="rp-car__cabin"></div>
                  <div className="rp-car__body"></div>
                  <div className="rp-car__wheel rp-car__wheel--f"></div>
                  <div className="rp-car__wheel rp-car__wheel--b"></div>
                </div>
                <div className="rp-bike2">
                  <div className="rp-bike2__frame"></div>
                  <div className="rp-bike2__seat"></div>
                  <div className="rp-bike2__rider-body"></div>
                  <div className="rp-bike2__rider-head"></div>
                  <div className="rp-bike2__wheel rp-bike2__wheel--f"></div>
                  <div className="rp-bike2__wheel rp-bike2__wheel--b"></div>
                </div>
              </div>
              <div className="rp-ground"></div>
            </div>
            <p className="loading-console__caption">Zoom zoom! Building the fastest routes…</p>
            <p className="loading-console__sub">{fileName ? `Processing ${fileName}` : 'Hang tight, this only takes a moment'}</p>
            <div className="progress-bar"><span className="progress-bar__fill" /></div>
          </div>
        )}
        </>
        )}

        {/* Boards - all always mounted; sidebar clicks scroll to whichever
            one is relevant instead of swapping the page's content. */}
        <div className="board-grid">

          {/* Routes tab and Unassigned Orders tab share this component's
              own internal tab-switching, driven by requestedTab below. */}
          <div id="unassigned-board">
          <RouteWorkspace
            routes={routes}
            pendingOrders={pendingOrders}
            isProcessing={isProcessing}
            capacityFor={capacityFor}
            maxCapacityFor={maxCapacityFor}
            isRouteFull={isRouteFull}
            isCreatingRoute={isCreatingRoute}
            isAddingManualAddress={isAddingManualAddress}
            isChangingVehicle={isChangingVehicle}
            isDeletingRoute={isDeletingRoute}
            isMovingAddresses={isMovingAddresses}
            isRemovingManualExtra={isRemovingManualExtra}
            onCreateRoute={handleCreateRoute}
            onAddManualAddress={handleAddManualAddress}
            onToggleVehicle={handleToggleVehicleType}
            onDeleteRoute={handleDeleteRoute}
            onReassignOrder={handleReassignOrder}
            onReorderRoute={persistReorder}
            onAssignOrders={handleAssignUnassignedOrders}
            onDownloadRoute={handleDownloadRoute}
            onMoveOrders={handleMoveOrdersBetweenRoutes}
            onRemoveManualExtra={handleRemoveManualExtra}
            onAdjustLocation={setAdjustLocationOrderId}
            requestedTab={activeNav === 'unassigned' ? 'unassigned' : 'routes'}
            requestedView={activeNav === 'live-tracking' ? 'map' : undefined}
            requestedLiveTracking={activeNav === 'live-tracking'}
            navCommandSeq={navCommandSeq}
            drivers={drivers}
            onAssignDriver={handleAssignDriver}
            onUnassignDriver={handleUnassignDriver}
            fetchRouteTracking={fetchRouteTracking}
            fetchRoutePlannedPath={fetchRoutePlannedPath}
            onViewChange={setRouteDetailOpen}
          />
          </div>

          {/* DRIVERS BOARD: roster + status - "Add Driver"
              still opens a small modal (a creation form genuinely is a
              focused, in-and-out task), but the roster and every driver's
              live status sit on this page same as any other page content. */}
          <div className="board board--drivers" id="drivers-board">
            <div className="board__header">
              <div className="board__header-group">
                <h2 className="board__title"><IconUsers width={18} height={18} /> Drivers</h2>
                <span className="board__count mono-num">{drivers.length}</span>
              </div>
              <button type="button" className="btn btn--primary board__header-action" onClick={() => { setEditingDriver(null); setDriverFormOpen(true); }}>
                <IconPlus width={14} height={14} /> Add Driver
              </button>
            </div>
            <p className="board__intro">
              Create driver logins for the Driver App, and assign them to routes from each route's detail view.
              Deactivating a driver signs them out everywhere immediately.
            </p>
            <div className="board__body">
              {isLoadingDrivers ? (
                <div className="history-panel__skeletons">
                  <SkeletonCard /><SkeletonCard /><SkeletonCard />
                </div>
              ) : driversError ? (
                <div className="empty-state">{driversError}</div>
              ) : drivers.length === 0 ? (
                <div className="empty-state">
                  No drivers yet. Click <strong>Add Driver</strong> to create the first login for the Driver App.
                </div>
              ) : (
                <div className="drivers-table-wrap">
                  <table className="drivers-table">
                    <thead>
                      <tr>
                        <th>Driver</th>
                        <th>Login</th>
                        <th>Vehicle</th>
                        <th>Status</th>
                        <th>Assigned Route</th>
                        <th></th>
                      </tr>
                    </thead>
                    <tbody>
                      {drivers.map((driver) => (
                        <tr key={driver.id}>
                          <td>
                            <div className="drivers-table__name">{driver.name}</div>
                            <div className="drivers-table__code mono-num">{driver.driver_code}</div>
                          </td>
                          <td>
                            <div>{driver.username}</div>
                            {driver.mobile && <div className="drivers-table__meta">{driver.mobile}</div>}
                          </td>
                          <td>{driver.vehicle_number || '—'}</td>
                          <td><DriverStatusPill status={driver.status} /></td>
                          <td>{driver.assigned_route_name || <span className="drivers-table__meta">Unassigned</span>}</td>
                          <td>
                            <div className="drivers-table__actions">
                              <button type="button" className="btn btn--ghost btn--compact" onClick={() => { setEditingDriver(driver); setDriverFormOpen(true); }}>Edit</button>
                              <button type="button" className="btn btn--ghost btn--compact" onClick={() => setResetPasswordDriver(driver)}>Reset Password</button>
                              <button
                                type="button"
                                className={`btn btn--compact ${driver.status === 'active' ? 'btn--danger-ghost' : 'btn--outline'}`}
                                disabled={isTogglingDriverStatus === driver.id}
                                onClick={() => handleToggleDriverStatus(driver)}
                              >
                                {isTogglingDriverStatus === driver.id && <span className="spinner" />}
                                {isTogglingDriverStatus === driver.id ? 'Working…' : driver.status === 'active' ? 'Deactivate' : 'Activate'}
                              </button>
                              <button
                                type="button"
                                className="btn btn--compact btn--danger-ghost"
                                disabled={isDeletingDriver === driver.id}
                                onClick={() => handleDeleteDriver(driver)}
                              >
                                {isDeletingDriver === driver.id && <span className="spinner" />}
                                {isDeletingDriver === driver.id ? 'Deleting…' : 'Delete'}
                              </button>
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>

          {/* RETURNS BOARD */}
          <div className="board board--returns" id="returns-board">
            <div className="board__header">
              <h2 className="board__title"><IconAlert width={18} height={18} /> Returns</h2>
              <span className="board__count mono-num">{failedOrders.length}</span>
            </div>
            <div className="board__body">
              {isProcessing && failedOrders.length === 0 ? (
                <>
                  <SkeletonCard />
                  <SkeletonCard />
                </>
              ) : failedOrders.length === 0 ? (
                <div className="empty-state empty-state--ok">
                  <IconCheck width={22} height={22} />
                  All addresses geocoded successfully
                </div>
              ) : displayedFailedOrders.length === 0 ? (
                <div className="empty-state">
                  <IconSearch width={22} height={22} />
                  No failed orders match "{searchTerm}"
                </div>
              ) : (
                <div className="failed-split">
                  <div className="failed-split__list">
                    {displayedFailedOrders.map((order) => {
                      const isActive = String(order.order_id) === String(selectedFailedId);
                      const feedback = retryFeedback[String(order.order_id)];
                      return (
                        <button
                          key={order.order_id}
                          className={`failed-row${isActive ? ' failed-row--active' : ''}`}
                          onClick={() => setSelectedFailedId(order.order_id)}
                        >
                          <span className="stop__id">#{order.order_id}</span>
                          <span className="failed-row__name">{order.customer_name}</span>
                          <span className="failed-row__reason">{order.geocode_error || 'Geocoding failed'}</span>
                          {feedback?.status === 'error' && <span className="failed-row__flag" title="Last retry didn't match either" />}
                        </button>
                      );
                    })}
                  </div>

                  <div className="failed-split__detail">
                    {!selectedFailedOrder ? (
                      <div className="empty-state">
                        <IconPin width={22} height={22} />
                        Select a failed order to review it
                      </div>
                    ) : (
                      <>
                        <div className="detail-head">
                          <div>
                            <span className="stop__id">#{selectedFailedOrder.order_id}</span>
                            <h3>{selectedFailedOrder.customer_name}</h3>
                          </div>
                        </div>

                        {/* The specific reason this address was flagged (see
                            geocode_service._verification_message) - a full
                            sentence now, not a short label, so it needs its
                            own wrapping banner rather than the .tag pill
                            (built for one-word labels like "Delayed" -
                            forcing a whole sentence into that nowrap pill is
                            what pushed the text out past its container). */}
                        <div className="failed-detail__reason">
                          <IconAlert width={13} height={13} />
                          <span>{selectedFailedOrder.geocode_error || 'Geocoding failed'}</span>
                        </div>

                        <div className="detail-map">
                          <iframe
                            title="Address map preview"
                            loading="lazy"
                            src={`https://maps.google.com/maps?q=${encodeURIComponent((editingAddresses[selectedFailedOrder.order_id] ?? selectedFailedOrder.address) || 'Chennai, India')}&z=15&output=embed`}
                          />
                        </div>

                        <label className="ticket__field-label" htmlFor={`addr-detail-${selectedFailedOrder.order_id}`}>Edit address</label>
                        <input
                          id={`addr-detail-${selectedFailedOrder.order_id}`}
                          className="ticket__field"
                          type="text"
                          value={editingAddresses[selectedFailedOrder.order_id] ?? (selectedFailedOrder.address || '')}
                          onChange={(e) => handleAddressChange(selectedFailedOrder.order_id, e.target.value)}
                        />

                        <div className="detail-grid">
                          <div className="detail-field">
                            <span className="ticket__field-label">Google-suggested address</span>
                            <p>{selectedFeedback?.geocodedAddress || 'Retry to see Google’s best match here.'}</p>
                          </div>
                          <div className="detail-field">
                            <span className="ticket__field-label">Address confidence</span>
                            <p>
                              {selectedFeedback?.confidence != null
                                ? `${Math.round(selectedFeedback.confidence * 100)}%`
                                : 'Retry to see a confidence score here.'}
                            </p>
                          </div>
                        </div>

                        {selectedFeedback && (
                          <div className={`retry-feedback retry-feedback--${selectedFeedback.status}`}>
                            {selectedFeedback.status === 'success' ? <IconCheck width={15} height={15} /> : <IconAlert width={15} height={15} />}
                            {selectedFeedback.message}
                          </div>
                        )}

                        <div className="modal__footer" style={{ padding: 0, border: 'none' }}>
                          <button
                            className="btn"
                            onClick={() => handleRetrySingleOrder(selectedFailedOrder.order_id)}
                            disabled={retryingOrderId === selectedFailedOrder.order_id}
                          >
                            {retryingOrderId === selectedFailedOrder.order_id ? (
                              'Retrying…'
                            ) : (
                              <>
                                <IconPin width={14} height={14} />
                                Retry geocode
                              </>
                            )}
                          </button>
                          <button
                            type="button"
                            className="btn btn--ghost"
                            onClick={() => setAdjustLocationOrderId(selectedFailedOrder.order_id)}
                          >
                            Adjust Location
                          </button>
                        </div>
                      </>
                    )}
                  </div>
                </div>
              )}
            </div>
          </div>

        </div>
      </div>

        <footer className="app-footer">
          <span>OptiRoute · ARZ Food Ventures — internal dispatch tool</span>
          <span>OSRM routing · Google geocoding</span>
        </footer>
      </div>

      {historyOpen && (
        <div className="history-overlay" role="dialog" aria-modal="true" aria-label="Route History">
          <div className="history-overlay__scrim" onClick={() => setHistoryOpen(false)} />
          <div className="history-panel">
            <div className="history-panel__head">
              <h2><IconHistory width={18} height={18} /> Route History</h2>
              <button className="topbar__icon-btn" onClick={() => setHistoryOpen(false)} aria-label="Close Route History">
                <IconX width={18} height={18} />
              </button>
            </div>
            <p className="history-panel__sub">
              Route plans you've explicitly saved. Generating or regenerating routes always keeps your
              current work persisted, but only plans saved here stay listed once you move on.
            </p>
            <div className="history-panel__body">
              {isLoadingHistory ? (
                <div className="history-panel__skeletons">
                  <SkeletonCard /><SkeletonCard /><SkeletonCard />
                </div>
              ) : historyError ? (
                <div className="empty-state">{historyError}</div>
              ) : historyPlans.length === 0 ? (
                <div className="empty-state">
                  No saved route plans yet. Generate a route plan, then click <strong>Save to history</strong> to add it here.
                </div>
              ) : (
                <ul className="history-list">
                  {historyPlans.map((plan) => (
                    <li key={plan.plan_id} className={`history-row${plan.plan_id === planId ? ' history-row--active' : ''}`}>
                      <div className="history-row__main">
                        <span className="history-row__label">
                          {plan.label || plan.file_name || `Route plan #${plan.plan_id}`}
                        </span>
                        <span className="history-row__meta">
                          {plan.saved_at ? new Date(plan.saved_at).toLocaleString() : ''}
                          {' · '}{plan.route_count} route{plan.route_count === 1 ? '' : 's'}
                          {' · '}{plan.total_stops} stop{plan.total_stops === 1 ? '' : 's'}
                          {plan.pending_count > 0 && ` · ${plan.pending_count} pending`}
                          {' · '}{plan.available_cars} car{plan.available_cars === 1 ? '' : 's'} / {plan.available_bikes} bike{plan.available_bikes === 1 ? '' : 's'}
                          {typeof plan.total_distance_km === 'number' && plan.total_distance_km > 0 && ` · ${plan.total_distance_km} km`}
                        </span>
                      </div>
                      <div className="history-row__actions">
                        <button className="btn btn--outline" onClick={() => handleOpenSavedPlan(plan.plan_id)}>Open</button>
                        <button className="btn btn--danger" onClick={() => handleDeleteSavedPlan(plan.plan_id)}>Delete</button>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </div>
      )}

      {driverDataOpen && (
        <div className="history-overlay" role="dialog" aria-modal="true" aria-label="Driver Data">
          <div className="history-overlay__scrim" onClick={() => setDriverDataOpen(false)} />
          <div className="history-panel history-panel--xwide">
            <div className="history-panel__head">
              <h2><IconClock width={18} height={18} /> Driver Data</h2>
              <button className="topbar__icon-btn" onClick={() => setDriverDataOpen(false)} aria-label="Close Driver Data">
                <IconX width={18} height={18} />
              </button>
            </div>
            <p className="history-panel__sub">
              Every driver's past work runs - start time, end time, and distance travelled (from their own GPS
              trail, same figure the driver app and Live Tracking show). Only a finished run can be deleted here;
              an in-progress one belongs to Live Tracking until it's ended.
            </p>
            <div className="history-panel__body">
              {isLoadingDriverData ? (
                <div className="history-panel__skeletons">
                  <SkeletonCard /><SkeletonCard /><SkeletonCard />
                </div>
              ) : driverDataError ? (
                <div className="empty-state">{driverDataError}</div>
              ) : driverRuns.length === 0 ? (
                <div className="empty-state">
                  No driver runs recorded yet. Once a driver taps <strong>Start Route</strong> in the Driver App, that run shows up here.
                </div>
              ) : (
                <div className="driver-runs-table-wrap">
                  <table className="driver-runs-table">
                    <thead>
                      <tr>
                        <th>Driver</th>
                        <th>Route</th>
                        <th>Started</th>
                        <th>Ended</th>
                        <th>Duration</th>
                        <th>Distance</th>
                        <th>Delivered</th>
                        <th>Status</th>
                        <th></th>
                      </tr>
                    </thead>
                    <tbody>
                      {driverRuns.map((run) => (
                        <tr key={run.route_id}>
                          <td>
                            <div className="driver-runs-table__name">{run.driver_name || 'Unknown driver'}</div>
                            {run.driver_code && <div className="driver-runs-table__code mono-num">{run.driver_code}</div>}
                          </td>
                          <td>{run.route_name}</td>
                          <td>{run.started_at ? new Date(run.started_at).toLocaleString() : '—'}</td>
                          <td>{run.completed_at ? new Date(run.completed_at).toLocaleString() : '—'}</td>
                          <td className="mono-num">{run.duration_minutes != null ? `${run.duration_minutes} min` : '—'}</td>
                          <td className="mono-num">{run.distance_travelled_km != null ? `${run.distance_travelled_km} km` : '—'}</td>
                          <td className="mono-num">{run.delivered_count}/{run.total_stops}</td>
                          <td>
                            <span className={`run-status-pill run-status-pill--${run.route_run_status}`}>
                              {run.route_run_status === 'in_progress' ? '🟢 In progress' : '⚪ Completed'}
                            </span>
                          </td>
                          <td>
                            <button
                              type="button"
                              className="btn btn--danger-ghost btn--compact"
                              disabled={run.route_run_status === 'in_progress' || deletingRunId === run.route_id}
                              title={run.route_run_status === 'in_progress' ? 'End this route before deleting its record' : 'Delete this run record'}
                              onClick={() => handleDeleteDriverRun(run)}
                            >
                              {deletingRunId === run.route_id ? <span className="spinner" /> : 'Delete'}
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {driverFormOpen && (
        <DriverFormModal
          driver={editingDriver}
          isSubmitting={isSavingDriver}
          onClose={() => { setDriverFormOpen(false); setEditingDriver(null); }}
          onSave={handleSaveDriver}
        />
      )}

      {resetPasswordDriver && (
        <ResetPasswordModal
          driver={resetPasswordDriver}
          isSubmitting={isResettingPassword}
          onClose={() => setResetPasswordDriver(null)}
          onSave={handleResetPassword}
        />
      )}

      {soonOverlay && (
        <div className="modal-backdrop" onClick={() => setSoonOverlay(null)}>
          <div onClick={(e) => e.stopPropagation()}>
            <ComingSoonPage
              label={findNavItem(soonOverlay)?.label}
              icon={findNavItem(soonOverlay)?.icon}
              blurb={SOON_BLURBS[soonOverlay] || "This page isn't wired up to real data yet."}
              onClose={() => setSoonOverlay(null)}
            />
          </div>
        </div>
      )}

      {toast && (
        <div className="toast" role="status">
          <IconBell width={14} height={14} />
          {toast}
        </div>
      )}

      <CommandPalette
        open={paletteOpen}
        query={paletteQuery}
        onQueryChange={setPaletteQuery}
        onClose={() => setPaletteOpen(false)}
        groups={paletteGroups}
      />

      {adjustLocationOrder && (
        <AdjustLocationModal
          order={adjustLocationOrder}
          isSubmitting={isSettingManualLocation}
          onConfirm={(lat, lng) => handleSetManualLocation(adjustLocationOrder.order_id, lat, lng)}
          onClose={() => setAdjustLocationOrderId(null)}
        />
      )}
    </div>
  );
}

export default App;
