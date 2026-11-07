import { useState } from 'react';
import type { Proposal } from '../types';
import { Field, Pill, show } from './Panels';

// Plain-language text for known action codes. Unknown codes are shown as-is.
const ACTION_TEXT: Record<string, string> = {
  STOP_DUPLICATE: 'Cancel the scheduled duplicate payment (STOP_DUPLICATE). This is a cancel, not a replacement.',
};

const MAX_REASON = 200;

interface Props {
  proposal: Proposal;
  busy: string | null;
  writesEnabled: boolean;
  onApprove: (p: Proposal) => void;
  onReject: (p: Proposal, reason: string) => void;
}

export function ProposalCard({ proposal: p, busy, writesEnabled, onApprove, onReject }: Props) {
  const [confirming, setConfirming] = useState(false);
  const [reason, setReason] = useState('');
  const trimmed = reason.trim();
  const locked = busy !== null;
  const approveKey = `approve:${p.id}`;
  const rejectKey = `reject:${p.id}`;

  const canApprove = writesEnabled && p.can_approve && p.binding_hash !== null && !locked;
  const canReject =
    writesEnabled && p.can_reject && trimmed.length >= 1 && trimmed.length <= MAX_REASON && !locked;
  const titleId = `proposal-${p.id}-title`;
  const reasonId = `proposal-${p.id}-reason`;

  return (
    <article className="proposal card-inner" aria-labelledby={titleId}>
      <header className="transfer-head">
        <h4 id={titleId}>{ACTION_TEXT[p.action] ?? p.action}</h4>
        <Pill tone={p.state === 'proposed' ? 'accent' : 'neutral'}>{p.state}</Pill>
      </header>

      <dl className="kv">
        <Field label="Kind">{p.kind}</Field>
        <Field label="Approval">{p.approval.state}</Field>
        <Field label="Remote">
          {show(p.remote_id, 'none')} {p.remote_status ? `(${p.remote_status})` : ''}
        </Field>
        <Field label="Expires">{show(p.expires_at, 'unknown')}</Field>
      </dl>

      <h5 className="subhead">Prerequisites</h5>
      <ul className="prereqs">
        {p.prerequisites.map((c) => (
          <li key={c.name} className={c.ok === true ? 'ok' : c.ok === false ? 'bad' : 'pending'}>
            <span className="prereq-state">{c.ok === true ? 'Met' : c.ok === false ? 'Blocked' : 'Checked at execute'}</span>{' '}
            <code>{c.name}</code> <span className="muted small">{c.detail}</span>
          </li>
        ))}
      </ul>

      {p.blocked_reasons.length > 0 ? (
        <p className="alert notice-error">Blocked: {p.blocked_reasons.join(', ')}</p>
      ) : null}

      <div className="actions">
        {confirming ? (
          <div className="confirm" role="group" aria-label="Confirm approval">
            <p>
              Approve runs this stored action once through the gate, using the terms bound to this proposal. A
              refused or failed approval is not retried.
            </p>
            <button type="button" className="btn primary" disabled={!canApprove} onClick={() => { setConfirming(false); onApprove(p); }}>
              {busy === approveKey ? 'Approving…' : 'Confirm approve'}
            </button>
            <button type="button" className="btn ghost" disabled={locked} onClick={() => setConfirming(false)}>
              Back
            </button>
          </div>
        ) : (
          <button type="button" className="btn primary" disabled={!canApprove} onClick={() => setConfirming(true)}>
            {busy === approveKey ? 'Approving…' : 'Approve…'}
          </button>
        )}
        {!writesEnabled ? <p className="muted small">Writes are disabled in this session.</p> : null}
        {writesEnabled && !p.can_approve && p.blocked_reasons.length === 0 ? (
          <p className="muted small">Approve is not available for this proposal.</p>
        ) : null}
      </div>

      <form
        className="reject"
        onSubmit={(e) => {
          e.preventDefault();
          if (canReject) onReject(p, trimmed);
        }}
      >
        <label htmlFor={reasonId}>Rejection reason (1 to {MAX_REASON} characters)</label>
        <div className="reject-row">
          <input
            id={reasonId}
            type="text"
            value={reason}
            maxLength={MAX_REASON}
            disabled={locked || !writesEnabled || !p.can_reject}
            onChange={(e) => setReason(e.target.value)}
          />
          <button type="submit" className="btn ghost" disabled={!canReject}>
            {busy === rejectKey ? 'Rejecting…' : 'Reject proposal'}
          </button>
        </div>
      </form>
    </article>
  );
}
