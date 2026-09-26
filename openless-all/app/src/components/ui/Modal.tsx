// Modal — centered dialog: backdrop + card. Marketplace details / upload / my
// listings / GitHub login all share this popup logic instead of each writing its own.
//
// Animation lives on overlays.css's .ol-dialog-overlay / .ol-dialog-card (pure
// opacity + transform, no blur). Exit uses dedicated *-out keyframes: reversing an
// already-finished animation doesn't replay it — it jumps to the start frame and
// vanishes with a pop.

import { useEffect, useRef, type CSSProperties, type ReactNode } from 'react';
import { createPortal } from 'react-dom';

interface ModalProps {
  children: ReactNode;
  onClose: () => void;
  /** Default 50; pass a larger value when stacking (e.g. login above "my listings"). */
  zIndex?: number;
  /** Card width, default 'min(560px, 100%)'. */
  width?: string;
  /** Dialogs with a fixed title/footer can let the inner content area scroll. */
  style?: CSSProperties;
  /** true plays the exit animation; the caller gates unmount timing with
   *  useExitMount, unmounting after the animation finishes. */
  closing?: boolean;
  /** Lets the host window customize overlay/card look (e.g. a rounded floating window needs a rounded overlay). */
  overlayClassName?: string;
  labelledBy?: string;
}

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), select:not([disabled]), input:not([disabled]):not([type="hidden"]), [tabindex]:not([tabindex="-1"])';

// Only the top-most dialog keeps Tab cycling inside itself.
const openModals: symbol[] = [];

function focusableWithin(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (element) => element.getClientRects().length > 0,
  );
}

export function Modal({
  children,
  onClose,
  zIndex = 50,
  width = 'min(560px, 100%)',
  style,
  closing = false,
  overlayClassName,
  labelledBy,
}: ModalProps) {
  const cardRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const id = Symbol('modal');
    openModals.push(id);
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const card = cardRef.current;
    if (card && !card.contains(document.activeElement)) card.focus({ preventScroll: true });
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Tab' || !card || openModals[openModals.length - 1] !== id) return;
      const items = focusableWithin(card);
      const active = document.activeElement;
      if (items.length === 0) {
        event.preventDefault();
        card.focus({ preventScroll: true });
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey) {
        if (active === first || active === card || !card.contains(active)) {
          event.preventDefault();
          last.focus();
        }
      } else if (active === last || !card.contains(active)) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      const index = openModals.indexOf(id);
      if (index >= 0) openModals.splice(index, 1);
      if (previous?.isConnected) previous.focus({ preventScroll: true });
    };
  }, []);

  // Portal to document.body: dialogs often trigger from inside panels (settings /
  // marketplace), and the window chrome (WindowChrome) and page containers carry a
  // persistent `will-change: transform`, creating a containing block — rendered in
  // place, the backdrop's `position: fixed` would anchor to that ancestor instead of
  // the viewport, covering only the triggering panel (e.g. GitHub login floating over
  // a still-bright settings page). Portaled out, fixed anchors to the viewport and
  // the overlay covers the whole window. Same approach as Tooltip / SelectLite.
  return createPortal(
    <div
      className={`ol-dialog-overlay${closing ? ' is-closing' : ''}${overlayClassName ? ` ${overlayClassName}` : ''}`}
      onClick={onClose}
      style={{
        position: 'fixed',
        inset: 0,
        background: 'var(--ol-dialog-backdrop)',
        display: 'grid',
        placeItems: 'center',
        zIndex,
        padding: 20,
        pointerEvents: closing ? 'none' : undefined,
      }}
    >
      <div
        ref={cardRef}
        className="ol-dialog-card"
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelledBy}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        style={{
          width,
          maxHeight: '85vh',
          overflow: 'auto',
          borderRadius: 'var(--ol-dialog-radius)',
          background: 'var(--ol-surface)',
          border: '1px solid var(--ol-dialog-border)',
          boxShadow: 'var(--ol-dialog-shadow)',
          padding: 24,
          outline: 'none',
          ...style,
        }}
      >
        {children}
      </div>
    </div>,
    document.body,
  );
}
