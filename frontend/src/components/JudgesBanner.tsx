import { useEffect, useState } from 'react';

const SHOW_MS = 12000;

/** Hosted only: a short notice that the passcode was removed for judging. */
export function JudgesBanner() {
  const hosted = window.location.hostname.endsWith('.onrender.com');
  const [open, setOpen] = useState(hosted);

  useEffect(() => {
    if (!open) return;
    const timer = window.setTimeout(() => setOpen(false), SHOW_MS);
    return () => window.clearTimeout(timer);
  }, [open]);

  if (!open) return null;
  return (
    <div className="judges-banner" role="status">
      <span>
        <strong>Open access for judges:</strong> we've removed the passcode so you can try Settlewell
        freely. It was there because this demo makes real Claude API and Airwallex sandbox calls.
      </span>
      <button type="button" className="judges-banner-close" aria-label="Dismiss" onClick={() => setOpen(false)}>
        ×
      </button>
    </div>
  );
}
