import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { MoreHorizontal } from 'lucide-react';

// Overflow menu for table rows. Portaled with position:fixed (same as the
// routes table's RowMenu) so a table's overflow:hidden can't clip it.
// items: [{ key, label, icon: Component, onClick, danger, disabled }]
export default function ActionMenu({ items, label = 'More actions' }) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState(null);
  const triggerRef = useRef(null);
  const panelRef = useRef(null);

  const place = () => {
    const rect = triggerRef.current?.getBoundingClientRect();
    if (rect) setPos({ top: rect.bottom + 6, right: window.innerWidth - rect.right });
  };

  useEffect(() => {
    if (!open) return undefined;
    const onDown = (e) => {
      if (triggerRef.current?.contains(e.target) || panelRef.current?.contains(e.target)) return;
      setOpen(false);
    };
    const onKey = (e) => { if (e.key === 'Escape') setOpen(false); };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    window.addEventListener('scroll', place, true);
    window.addEventListener('resize', place);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('scroll', place, true);
      window.removeEventListener('resize', place);
    };
  }, [open]);

  return (
    <>
      <button
        type="button"
        ref={triggerRef}
        className="btn btn--ghost btn--compact action-menu__trigger"
        aria-label={label}
        title={label}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => { if (!open) place(); setOpen((v) => !v); }}
      >
        <MoreHorizontal width={16} height={16} />
      </button>
      {open && pos && createPortal(
        <div className="row-menu__panel" role="menu" ref={panelRef} style={{ top: pos.top, right: pos.right }}>
          {items.map(({ key, label: itemLabel, icon: Icon, onClick, danger, disabled }) => (
            <button
              key={key}
              type="button"
              role="menuitem"
              className={danger ? 'row-menu__danger' : undefined}
              disabled={disabled}
              onClick={() => { setOpen(false); onClick(); }}
            >
              {Icon && <Icon width={13} height={13} />}
              {itemLabel}
            </button>
          ))}
        </div>,
        document.body,
      )}
    </>
  );
}
