import { useState } from 'react';
import type { Transfer } from '../types';
import { Field, Pill, show } from './Panels';

interface Props {
  transfer: Transfer;
  busy: string | null;
  writesEnabled: boolean;
  onCancel: (t: Transfer) => void;
}

export function TransferCard({ transfer: t, busy, writesEnabled, onCancel }: Props) {
  const [confirming, setConfirming] = useState(false);
  const cancelKey = `cancel:${t.id}`;
  const locked = busy !== null;
  const canCancel = writesEnabled && t.cancellable && !locked;

  return (
    <article className="transfer card-inner" aria-label={`Payment ${show(t.airwallex_id, 'no Airwallex id')}`}>
      <header className="transfer-head">
        <h4>
          {t.origin} payment · {t.amount} {t.currency}
        </h4>
        <Pill tone={t.status === 'SCHEDULED' ? 'accent' : t.status === 'SENT' ? 'warn' : 'neutral'}>{t.status}</Pill>
      </header>
      <dl className="kv">
        <Field label="Airwallex id">
          <code>{show(t.airwallex_id, 'none recorded')}</code>
        </Field>
        <Field label="Request id">
          <code>{show(t.request_id, 'none')}</code>
        </Field>
        <Field label="Live transfer">{t.is_live ? 'Yes (the obligation’s live transfer)' : 'No'}</Field>
        <Field label="Fee">{show(t.fee, 'unknown')}</Field>
        <Field label="Dispatch date">{show(t.dispatch_date, 'none')}</Field>
        <Field label="Reference">{show(t.reference, 'none')}</Field>
        <Field label="Failure">
          {t.failure_code ? (
            <>
              <code>{t.failure_code}</code> {t.failure_message ?? ''}
            </>
          ) : (
            'none recorded'
          )}
        </Field>
      </dl>

      {t.cancellable ? (
        writesEnabled ? (
          confirming ? (
            <div className="confirm" role="group" aria-label="Confirm cancel">
              <p>
                Cancel this still-scheduled duplicate payment. The server re-reads the transfer first and refuses
                if it is no longer SCHEDULED. It cannot be undone from this console.
              </p>
              <button
                type="button"
                className="btn danger"
                disabled={!canCancel}
                onClick={() => {
                  setConfirming(false);
                  onCancel(t);
                }}
              >
                {busy === cancelKey ? 'Cancelling…' : 'Confirm cancel payment'}
              </button>
              <button type="button" className="btn ghost" disabled={locked} onClick={() => setConfirming(false)}>
                Keep payment
              </button>
            </div>
          ) : (
            <button type="button" className="btn danger-outline" disabled={!canCancel} onClick={() => setConfirming(true)}>
              Cancel scheduled payment…
            </button>
          )
        ) : (
          <p className="muted small">Cancel is available for a scheduled payment, but writes are disabled here.</p>
        )
      ) : (
        <p className="muted small">No cancel action: this payment is not cancellable (status {t.status}).</p>
      )}
    </article>
  );
}
