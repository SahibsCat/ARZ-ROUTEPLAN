import { useState, useEffect, useRef } from 'react';
import ExcelJS from 'exceljs';
import './App.css';
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

// A small, fixed palette so each route reads as a distinct color at a
// glance - cycles if there are more routes than colors.
const ROUTE_HUES = 6;

// Sidebar sections. The first group anchors to real, working parts of this
// single-page app (or, for 'history', opens the Route History panel). The
// second group names the rest of what a full dispatch platform would
// eventually cover - deliberately inert (no fabricated screens or fake
// data) until there's a backend behind them.
const NAV_ITEMS = [
  { key: 'dashboard', label: 'Dashboard', icon: IconLayoutGrid, anchor: 'top' },
  { key: 'generate', label: 'Generate Routes', icon: IconRoute, anchor: 'toolbar-section' },
  { key: 'failed', label: 'Failed Addresses', icon: IconAlert, anchor: 'returns-board' },
  { key: 'history', label: 'Route History', icon: IconHistory },
];
const NAV_ITEMS_SOON = [
  { key: 'reports', label: 'Reports', icon: IconFileText },
  { key: 'analytics', label: 'Analytics', icon: IconBarChart },
  { key: 'settings', label: 'Settings', icon: IconSettings },
];

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

// A single-row stacked bar showing real proportions (e.g. assigned vs
// pending vs failed out of today's orders) - never a fabricated trend.
function MiniProportionBar({ segments }) {
  const total = segments.reduce((sum, seg) => sum + seg.value, 0) || 1;
  return (
    <div className="kpi-mini-bar">
      {segments.map((seg, i) => (
        <span
          key={i}
          className="kpi-mini-bar__seg"
          style={{ width: `${(seg.value / total) * 100}%`, background: seg.color }}
        />
      ))}
    </div>
  );
}

// A tiny bar-per-route sparkline (e.g. today's route distances) - a real
// breakdown of today's routes, not a simulated history.
function MiniSparkBars({ values, color }) {
  const max = Math.max(1, ...values);
  return (
    <div className="kpi-spark">
      {values.length === 0
        ? <span className="kpi-spark__empty">No routes yet</span>
        : values.map((v, i) => (
          <span key={i} className="kpi-spark__bar" style={{ height: `${Math.max(10, (v / max) * 100)}%`, background: color }} />
        ))}
    </div>
  );
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

function KpiTile({ variant, icon: Icon, value, label, suffix, graphic }) {
  const display = useCountUp(value);
  return (
    <div className={`kpi-tile kpi-tile--${variant}`}>
      <div className="kpi-tile__top">
        <span className="kpi-tile__icon"><Icon width={18} height={18} /></span>
        <div>
          <span className="kpi-tile__value mono-num">
            {display}{suffix && <span className="kpi-tile__suffix">{suffix}</span>}
          </span>
          <span className="kpi-tile__label">{label}</span>
        </div>
      </div>
      {graphic}
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

  const handleNavClick = (item) => {
    setActiveNav(item.key);
    setMobileNavOpen(false);
    if (item.key === 'history') {
      openHistory();
    } else if (item.anchor === 'top') {
      window.scrollTo({ top: 0, behavior: 'smooth' });
    } else {
      document.getElementById(item.anchor)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  };
  const handleSoonClick = (item) => {
    showToast(`${item.label} isn't built yet - no backend data behind it.`);
  };

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
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const response = await apiFetch('/api/dashboard');
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
        }
      } catch (err) {
        console.error('Could not restore previous session:', err);
      } finally {
        if (!cancelled) setIsRestoringSession(false);
      }
    })();
    return () => { cancelled = true; };
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

  const handleUpload = async (event) => {
    const file = event.target.files?.[0];
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

  // Assign one unassigned order straight to an existing route from the
  // Unassigned Orders board.
  const handleAssignUnassignedOrder = async (orderId, targetRouteName) => {
    const targetRoute = routes.find((r) => r.route_name === targetRouteName);
    if (!targetRoute) return;
    const idStr = String(orderId);
    try {
      const res = await postJson(`/api/routes/${targetRoute.route_id}/orders`, 'POST', { order_ids: [idStr] });
      if (!res.ok) throw new Error(await parseErrorDetail(res, 'Unable to assign this order. Please try again.'));
      const data = await res.json();
      patchRouteInState(data.route);
      setPendingOrders((prev) => prev.filter((o) => String(o.order_id) !== idStr));
      setStatus(`Assigned order #${orderId} to ${targetRouteName}.`);
    } catch (err) {
      console.error('Assign failed:', err);
      setWarnings([err.message || 'Unable to assign this order. Please try again.']);
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

  // Palette mirrors App.css's --btn-fill / --ink / --good / --critical so the
  // sheet reads as the same product, not a plain default-Excel export.
  // ARGB (exceljs requires the leading alpha channel - FF = fully opaque).
  const XLSX_VIOLET = 'FF7C3AED';
  const XLSX_VIOLET_DARK = 'FF6D28D9';
  const XLSX_ZEBRA = 'FFF3F1FA';
  const XLSX_WHITE = 'FFFFFFFF';
  const XLSX_INK = 'FF1E1B2E';
  const XLSX_GOOD = 'FF059669';
  const XLSX_CRITICAL = 'FFDC2626';
  const XLSX_SIGNAL = 'FFD97706';
  const XLSX_COL_COUNT = 8;
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

    sheet.columns = [
      { width: 8 }, { width: 12 }, { width: 20 }, { width: 38 },
      { width: 14 }, { width: 10 }, { width: 10 }, { width: 20 },
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
    const headers = ['Stop #', 'Order ID', 'Customer', 'Address', 'Delivery Slot', 'ETA', 'Status', 'Google Maps'];
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
        order.address,
        order.delivery_time,
        order.eta || '—',
        order.is_late ? 'LATE' : 'On time',
      ];
      values.forEach((value, c) => {
        const cell = sheet.getCell(rowNum, c + 1);
        cell.value = value;
        cell.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: zebra } };
        cell.alignment = { horizontal: c === 0 ? 'center' : 'left', vertical: 'middle' };
        cell.border = XLSX_THIN_BORDER;
        cell.font = { color: { argb: XLSX_INK } };
      });
      sheet.getCell(rowNum, 7).font = { bold: true, color: { argb: order.is_late ? XLSX_CRITICAL : XLSX_GOOD } };

      const mapsCell = sheet.getCell(rowNum, 8);
      mapsCell.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: zebra } };
      mapsCell.border = XLSX_THIN_BORDER;
      mapsCell.alignment = { horizontal: 'left', vertical: 'middle' };
      if (order.address || (order.lat != null && order.lng != null)) {
        mapsCell.value = {
          text: 'Open in Maps →',
          hyperlink: buildStopMapsLink(order),
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

  // All nine KPIs below are computed straight from live state - no
  // simulated history, no placeholder numbers.
  const assignedOrdersCount = routes.reduce((sum, r) => sum + (r.orders?.length || 0), 0);
  const totalDistanceKm = Math.round(routes.reduce((sum, r) => sum + (r.route_distance_km || 0), 0));
  const avgEtaMinutes = routes.length
    ? Math.round(routes.reduce((sum, r) => sum + (r.route_time_minutes || 0), 0) / routes.length)
    : 0;
  const carRoutesCount = routes.filter((r) => r.vehicle_type === 'car').length;
  const bikeRoutesCount = routes.filter((r) => r.vehicle_type === 'bike').length;

  const selectedFailedOrder = failedOrders.find((o) => String(o.order_id) === String(selectedFailedId)) || null;
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

  return (
    <div className="app-shell">
      <div className={`sidebar__scrim${mobileNavOpen ? ' is-visible' : ''}`} onClick={() => setMobileNavOpen(false)} />

      <aside className={`sidebar${sidebarCollapsed ? ' sidebar--collapsed' : ''}${mobileNavOpen ? ' sidebar--mobile-open' : ''}`}>
        <div className="sidebar__brand">
          <img src={arzLogo} alt="ARZ Food Ventures" className="brand-logo brand-logo--light" style={{ height: 28 }} />
          <img src={arzLogoDark} alt="ARZ Food Ventures" className="brand-logo brand-logo--dark" style={{ height: 28 }} />
          <div className="sidebar__brand-text">
            <div className="sidebar__brand-title">OptiRoute</div>
            <div className="sidebar__brand-sub">ARZ Food Ventures</div>
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
          <div className="sidebar__section-label">Workspace</div>
          {NAV_ITEMS.map((item) => (
            <button
              key={item.key}
              className={`nav-item${activeNav === item.key ? ' nav-item--active' : ''}`}
              onClick={() => handleNavClick(item)}
              title={item.label}
            >
              <item.icon width={17} height={17} />
              <span>{item.label}</span>
            </button>
          ))}

          <div className="sidebar__section-label">Platform</div>
          {NAV_ITEMS_SOON.map((item) => (
            <button
              key={item.key}
              className="nav-item nav-item--soon"
              onClick={() => handleSoonClick(item)}
              title={`${item.label} - coming soon`}
            >
              <item.icon width={17} height={17} />
              <span>{item.label}</span>
              <span className="nav-item__badge">Soon</span>
            </button>
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
              <h1>Dashboard</h1>
              <p>Delivery route planning &amp; vehicle assignment</p>
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

            <div className="topbar__avatar" title="Dispatch Admin">DA</div>
          </div>
        </header>

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

      <div className="kpi-strip">
          <div className="kpi-strip__inner">
            <KpiTile
              variant="orders" icon={IconInbox} value={totalOrders} label="Today's orders"
              graphic={<MiniProportionBar segments={[
                { value: assignedOrdersCount, color: 'var(--primary)' },
                { value: pendingOrders.length, color: 'var(--signal)' },
                { value: failedOrders.length, color: 'var(--critical)' },
              ]} />}
            />
            <KpiTile
              variant="assigned" icon={IconCheck} value={assignedOrdersCount} label="Assigned orders"
              graphic={<MiniProportionBar segments={[
                { value: assignedOrdersCount, color: 'var(--good)' },
                { value: Math.max(0, totalOrders - assignedOrdersCount), color: 'var(--rule)' },
              ]} />}
            />
            <KpiTile
              variant="pending" icon={IconClock} value={pendingOrders.length} label="Pending orders"
              graphic={<MiniProportionBar segments={[
                { value: pendingOrders.length, color: 'var(--signal)' },
                { value: Math.max(0, totalOrders - pendingOrders.length), color: 'var(--rule)' },
              ]} />}
            />
            <KpiTile
              variant="failed" icon={IconAlert} value={failedOrders.length} label="Failed addresses"
              graphic={<MiniProportionBar segments={[
                { value: failedOrders.length, color: 'var(--critical)' },
                { value: Math.max(0, totalOrders - failedOrders.length), color: 'var(--rule)' },
              ]} />}
            />
            <KpiTile
              variant="bikes" icon={IconBike} value={bikes} label="Available bikes"
              graphic={<MiniProportionBar segments={[
                { value: bikes, color: 'var(--good)' },
                { value: cars, color: 'var(--rule)' },
              ]} />}
            />
            <KpiTile
              variant="cars" icon={IconCar} value={cars} label="Available cars"
              graphic={<MiniProportionBar segments={[
                { value: cars, color: 'var(--good)' },
                { value: bikes, color: 'var(--rule)' },
              ]} />}
            />
            <KpiTile
              variant="distance" icon={IconRoute} value={totalDistanceKm} suffix=" km" label="Total distance"
              graphic={<MiniSparkBars values={routes.map((r) => r.route_distance_km || 0)} color="var(--primary)" />}
            />
            <KpiTile
              variant="eta" icon={IconFlag} value={avgEtaMinutes} suffix=" min" label="Average ETA"
              graphic={<MiniSparkBars values={routes.map((r) => r.route_time_minutes || 0)} color="var(--signal)" />}
            />
            <KpiTile
              variant="routes" icon={IconRoute} value={routes.length} label="Today's routes"
              graphic={<MiniProportionBar segments={[
                { value: carRoutesCount, color: 'var(--primary)' },
                { value: bikeRoutesCount, color: 'var(--good)' },
              ]} />}
            />
          </div>
      </div>

      <div className="console">
        {/* Toolbar */}
        <div className="toolbar" id="toolbar-section">
          <div className="toolbar__row">
            <div className="toolbar__group toolbar__group--fleet">
              <div className={`fleet-dial${!hasVehicles ? ' fleet-dial--alarm' : ''}`}>
                <label className="fleet-dial__label" htmlFor="cars-input">Cars</label>
                <input
                  id="cars-input"
                  className="fleet-dial__input mono-num"
                  type="number"
                  min="0"
                  value={cars}
                  disabled={isProcessing}
                  onChange={(e) => setCars(Math.max(0, Number(e.target.value) || 0))}
                />
              </div>
              <div className={`fleet-dial${!hasVehicles ? ' fleet-dial--alarm' : ''}`}>
                <label className="fleet-dial__label" htmlFor="bikes-input">Bikes</label>
                <input
                  id="bikes-input"
                  className="fleet-dial__input mono-num"
                  type="number"
                  min="0"
                  value={bikes}
                  disabled={isProcessing}
                  onChange={(e) => setBikes(Math.max(0, Number(e.target.value) || 0))}
                />
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
                className="toolbar-btn"
                onClick={handleRegenerateRoutes}
                disabled={isProcessing || isRegenerating || successfulOrders.length === 0}
                title="Recompute routes with the current car/bike counts - no re-upload needed"
              >
                <IconRefresh width={14} height={14} className={isRegenerating ? 'icon-spin' : ''} />
                {isRegenerating ? 'Regenerating…' : 'Regenerate'}
              </button>

              {routes.length > 0 && (
                <button
                  className="toolbar-btn toolbar-btn--ghost"
                  onClick={handleDownloadAllClick}
                  disabled={isDownloadingAll}
                  title="Download every route as one Excel workbook - an Overview sheet plus one sheet per route"
                >
                  <IconDownload width={14} height={14} />
                  {isDownloadingAll ? 'Preparing…' : 'Download all'}
                </button>
              )}

              {planId != null && (
                <button
                  className="toolbar-btn toolbar-btn--save"
                  onClick={handleSaveToHistory}
                  disabled={isSavingPlan || isPlanSaved}
                  title={isPlanSaved ? 'Already in Route History' : 'Save this route plan to Route History'}
                >
                  <IconCheck width={14} height={14} />
                  {isPlanSaved ? 'Saved' : isSavingPlan ? 'Saving…' : 'Save'}
                </button>
              )}

              {planId != null && (
                <button
                  className="toolbar-btn toolbar-btn--danger"
                  onClick={handleDeleteCurrentPlan}
                  disabled={isDeletingPlan}
                  title="Delete this route plan"
                >
                  <IconX width={14} height={14} />
                  {isDeletingPlan ? 'Deleting…' : 'Delete'}
                </button>
              )}
            </div>

            <div className="toolbar__divider" aria-hidden="true" />

            <div className="toolbar__group toolbar__group--upload">
              <div className="intake">
                <label className="intake__label" htmlFor="manifest-input">Load order manifest (.xlsx)</label>
                <input
                  id="manifest-input"
                  className="intake__input"
                  type="file"
                  accept=".xlsx"
                  onChange={handleUpload}
                  disabled={isProcessing}
                />
              </div>
            </div>
          </div>

          <div className="toolbar__status-row">
            <div className="status-readout">
              <span><strong>Status</strong> {isRestoringSession ? 'Checking for a saved session…' : status}</span>
              {fileName && (
                // The native <input type="file"> can never be made to show a
                // previously-picked file again after a refresh - browsers
                // don't allow JS to set that display for security reasons.
                // This is the real, state-backed record of what's loaded.
                <span title="Restored from Upload History"><strong>File</strong> {fileName}</span>
              )}
              <span><strong>Orders</strong> {totalOrders}</span>
            </div>
          </div>
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

        {/* Alerts. describeErrorDetail guards against ever rendering a raw
            object/array as a message (React stringifies those as
            "[object Object]") - every entry here is guaranteed plain text
            by the time it lands in errors/warnings state, but this is the
            last line of defense right at render time too. */}
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

        {/* Boards */}
        <div className="board-grid">

          {/* RETURNS BOARD: failed orders, master/detail */}
          <div className="board board--returns" id="returns-board">
            <div className="board__header">
              <h2 className="board__title">Returns</h2>
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
                          <span className="tag tag--late">
                            <IconAlert width={11} height={11} />
                            {selectedFailedOrder.geocode_error || 'Geocoding failed'}
                          </span>
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
                                : 'Not reported by the active provider (Google).'}
                            </p>
                          </div>
                        </div>

                        {selectedFeedback && (
                          <div className={`retry-feedback retry-feedback--${selectedFeedback.status}`}>
                            {selectedFeedback.status === 'success' ? <IconCheck width={15} height={15} /> : <IconAlert width={15} height={15} />}
                            {selectedFeedback.message}
                          </div>
                        )}

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
                      </>
                    )}
                  </div>
                </div>
              )}
            </div>
          </div>

          {/* UNASSIGNED ORDERS BOARD: newly-imported orders never routed, plus
              orders removed from a route - never deleted, always live here
              until re-assigned. See brief §9. */}
          <div className="board board--unassigned" id="unassigned-board">
            <div className="board__header">
              <h2 className="board__title">Unassigned Orders</h2>
              <span className="board__count mono-num">Total Unassigned: {pendingOrders.length}</span>
            </div>
            <div className="board__body">
              {isProcessing && pendingOrders.length === 0 ? (
                <>
                  <SkeletonCard />
                  <SkeletonCard />
                </>
              ) : pendingOrders.length === 0 ? (
                <div className="empty-state empty-state--ok">
                  <IconCheck width={22} height={22} />
                  No unassigned orders
                </div>
              ) : displayedPendingOrders.length === 0 ? (
                <div className="empty-state">
                  <IconSearch width={22} height={22} />
                  No orders match "{searchTerm}"
                </div>
              ) : (
                <div className="failed-split__list">
                  {displayedPendingOrders.map((order) => (
                    <div key={order.order_id} className="failed-row failed-row--static">
                      <div className="timeline-node__row">
                        <span className="stop__id">#{order.order_id}</span>
                        <span className="failed-row__name">{order.customer_name}</span>
                        <span className={`tag ${order.status === 'unassigned' ? 'tag--edited' : 'tag--auto'}`}>
                          {order.status === 'unassigned' ? 'Removed from route' : 'Never routed'}
                        </span>
                      </div>
                      <div className="timeline-node__meta">
                        <span>{order.address}</span>
                        {order.previous_route_name && (
                          <span>Previously: {order.previous_route_name} ({order.previous_vehicle_type})</span>
                        )}
                        <a className="map-link" href={order.map_link} target="_blank" rel="noopener noreferrer">
                          <IconPin width={11} height={11} />
                          View on map
                        </a>
                      </div>
                      <select
                        className="stop-move"
                        value=""
                        title="Assign this order to a route"
                        onChange={(e) => {
                          const target = e.target.value;
                          if (target) handleAssignUnassignedOrder(order.order_id, target);
                        }}
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
                  ))}
                </div>
              )}
            </div>
          </div>

          {/* DISPATCH BOARD: routes */}
          <div className="board board--dispatch">
            <div className="board__header">
              <h2 className="board__title">Dispatch board</h2>
              <span className="board__count mono-num">{routes.length} routes</span>
              <span className="manifest__actions">
                <button type="button" className="btn btn--ghost" disabled={isCreatingRoute} onClick={() => handleCreateRoute('bike')}>
                  <IconBike width={14} height={14} /> Add Route (bike)
                </button>
                <button type="button" className="btn btn--ghost" disabled={isCreatingRoute} onClick={() => handleCreateRoute('car')}>
                  <IconCar width={14} height={14} /> Add Route (car)
                </button>
              </span>
            </div>
            <div className="board__body">
              {isProcessing && routes.length === 0 ? (
                <>
                  <SkeletonCard />
                  <SkeletonCard />
                </>
              ) : routes.length === 0 ? (
                <div className="empty-state">
                  <IconInbox width={22} height={22} />
                  Upload an Excel manifest above to generate OSRM road routes.
                </div>
              ) : displayedRoutes.length === 0 ? (
                <div className="empty-state">
                  <IconSearch width={22} height={22} />
                  No routes match "{searchTerm}"
                </div>
              ) : (
                <div>
                  {displayedRoutes.map((route, routeIdx) => {
                    const utilPct = route.utilization_percent;
                    const utilClass = utilPct == null ? '' : utilPct >= 70 ? ' stat-good' : utilPct >= 40 ? ' stat-warn' : ' stat-low';
                    const fullCount = route.orders.length;
                    const capacity = capacityFor(route.vehicle_type);
                    const slotMins = route.orders.map((o) => parseSlotMinutes(o.delivery_time)).filter((m) => m != null);
                    const formatSlot = (total) => {
                      let h = Math.floor(total / 60);
                      const m = total % 60;
                      const ap = h >= 12 ? 'PM' : 'AM';
                      h %= 12; if (h === 0) h = 12;
                      return `${h}:${String(m).padStart(2, '0')} ${ap}`;
                    };
                    const slotRange = slotMins.length
                      ? (Math.min(...slotMins) === Math.max(...slotMins)
                        ? formatSlot(slotMins[0])
                        : `${formatSlot(Math.min(...slotMins))} – ${formatSlot(Math.max(...slotMins))}`)
                      : '—';
                    return (
                    <div key={route.route_name} className={`manifest route-hue-${routeIdx % ROUTE_HUES}`}>
                      <div className="manifest__head">
                        <div className="manifest__head-left">
                          <span className="vehicle-chip">
                            {route.vehicle_type === 'car' ? <IconCar width={14} height={14} /> : <IconBike width={14} height={14} />}
                            {route.vehicle_type === 'car' ? 'Car' : 'Bike'}
                          </span>
                          <h3 className="manifest__name">{route.route_name}</h3>
                          <span className="driver-chip" title="No driver roster is tracked yet">
                            <IconUsers width={11} height={11} />
                            Unassigned
                          </span>
                          <span className="tag tag--capacity mono-num">{fullCount}/{capacity}</span>
                          {route.is_auto_created && (
                            <span className="tag tag--auto"><IconPlus width={11} height={11} />Auto-added</span>
                          )}
                          {route.late_deliveries && route.late_deliveries.length > 0 && (
                            <span className="tag tag--late"><IconClock width={11} height={11} />{route.late_deliveries.length} late</span>
                          )}
                          {editedRoutes.includes(route.route_name) && (
                            <span className="tag tag--edited"><IconAlert width={11} height={11} />Manually edited</span>
                          )}
                        </div>

                        <div className="manifest__actions">
                          <button className="download-link" onClick={() => handleDownloadRoute(route)}>
                            <IconDownload width={14} height={14} />
                            Download sheet
                          </button>
                          {route.google_maps_url && (
                            <a className="map-link" href={route.google_maps_url} target="_blank" rel="noopener noreferrer">
                              <IconPin width={14} height={14} />
                              Open in Google Maps
                            </a>
                          )}
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
                          <span className="data-strip__value">{route.number_of_stops}</span>
                        </div>
                        <div className="data-strip__item">
                          <span className="data-strip__label"><IconClock width={11} height={11} />Delivery slot</span>
                          <span className="data-strip__value">{slotRange}</span>
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
                          <span className={`data-strip__value${utilClass}`}>{route.utilization_percent ?? '—'}%</span>
                        </div>
                      </div>

                      <button
                        type="button"
                        className="details-toggle"
                        onClick={() => toggleRouteDetails(route.route_name)}
                        aria-expanded={!collapsedRoutes[route.route_name]}
                      >
                        <IconChevron className={`ticket__chevron${collapsedRoutes[route.route_name] ? '' : ' ticket__chevron--open'}`} width={14} height={14} />
                        {collapsedRoutes[route.route_name]
                          ? `Show route timeline (${fullCount} stop${fullCount === 1 ? '' : 's'})`
                          : 'Hide route timeline'}
                      </button>

                      {!collapsedRoutes[route.route_name] && (
                        <div className="route-timeline">
                          <div className="timeline-node timeline-node--terminal">
                            <span className="timeline-node__dot timeline-node__dot--depot"><IconInbox width={12} height={12} /></span>
                            <span className="timeline-node__terminal-label">Warehouse</span>
                          </div>

                          {route.orders.map((order, stopIdx) => {
                            const seg = route.route_segments?.[stopIdx];
                            return (
                              <div
                                key={order.order_id}
                                className="timeline-node timeline-node--draggable"
                                draggable
                                title="Drag to reorder this delivery within the route"
                                onDragStart={() => handleStopDragStart(route.route_name, order.order_id)}
                                onDragOver={(e) => e.preventDefault()}
                                onDrop={() => handleStopDrop(route, order.order_id)}
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
                                        onClick={() => handleReorderStop(route.route_name, order.order_id, 'up')}
                                      >
                                        <IconArrowUp width={12} height={12} />
                                      </button>
                                      <button
                                        type="button"
                                        className="stop-reorder__btn"
                                        disabled={stopIdx === route.orders.length - 1}
                                        title="Move later in the delivery sequence"
                                        onClick={() => handleReorderStop(route.route_name, order.order_id, 'down')}
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
                                        if (target) handleReassignOrder(order.order_id, route.route_name, target);
                                      }}
                                    >
                                      <option value="">Move to…</option>
                                      {routes.filter((r) => r.route_name !== route.route_name).map((r) => {
                                        const full = isRouteFull(r);
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
                                    <span>{order.address}</span>
                                    <span>Slot {order.delivery_time}</span>
                                    {order.eta && <span>ETA {order.eta}</span>}
                                    {seg && <span>{seg.distance_km ?? 0} km · {seg.time_minutes ?? 0} min from previous stop</span>}
                                    <a
                                      className="map-link"
                                      href={order.map_link || buildStopMapsLink(order)}
                                      target="_blank"
                                      rel="noopener noreferrer"
                                    >
                                      <IconPin width={11} height={11} />
                                      View on map
                                    </a>
                                  </div>
                                </div>
                              </div>
                            );
                          })}

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
                  })}
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

      {toast && (
        <div className="toast" role="status">
          <IconBell width={14} height={14} />
          {toast}
        </div>
      )}
    </div>
  );
}

export default App;
