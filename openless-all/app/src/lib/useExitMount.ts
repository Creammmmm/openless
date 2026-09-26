// Keep overlays mounted until the exit animation finishes after close; reopening cancels the
// pending unmount timer.
// Callers use mounted to control rendering and closing for exit styles; exitMs must match the CSS
// animation duration.

import { useEffect, useState } from 'react';

export function useExitMount(open: boolean, exitMs = 200) {
  const [mounted, setMounted] = useState(open);
  const [closing, setClosing] = useState(false);
  useEffect(() => {
    if (open) {
      setMounted(true);
      setClosing(false);
      return;
    }
    if (!mounted) return;
    setClosing(true);
    const timer = window.setTimeout(() => {
      setMounted(false);
      setClosing(false);
    }, exitMs);
    return () => window.clearTimeout(timer);
  }, [open, mounted, exitMs]);
  return { mounted, closing };
}
