import { useEffect, useState } from 'react';
import { describeError, getAdvisory } from '../api';
import type { Advisory } from '../types';
import { Card, Pill } from './Panels';

function SourcePill({ source }: { source: string }) {
  if (source === 'claude') return <Pill tone="accent">Claude (Haiku)</Pill>;
  if (source === 'rules') return <Pill>Rules engine</Pill>;
  return <Pill>Template (no model)</Pill>;
}

/** Advisory-only AI. Nothing here is wired to approve, execute, send or move money. */
export function AdvisoryPanel({ incidentId }: { incidentId: number }) {
  const [data, setData] = useState<Advisory | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState('');

  useEffect(() => {
    let live = true;
    setData(null);
    setError(null);
    getAdvisory(incidentId)
      .then((a) => {
        if (!live) return;
        setData(a);
        setDraft(a.reply_draft.text);
      })
      .catch((e) => live && setError(describeError(e)));
    return () => {
      live = false;
    };
  }, [incidentId]);

  return (
    <Card id="advisory-title" title="AI explanation (advisory)">
      <p className="small muted">
        AI for understanding, rules for money. This text cannot approve, execute or send anything; the rules engine
        already decided and every money step still needs a bound human approval.
      </p>
      {error ? <p className="small">Advisory text unavailable: {error}</p> : null}
      {!data && !error ? <p className="small muted">Loading advisory text…</p> : null}
      {data ? (
        <>
          <h4>
            Why the rules chose this <SourcePill source={data.explanation.source} />
          </h4>
          <p>{data.explanation.text}</p>

          <h4>
            Unknown-incident triage <SourcePill source={data.triage.source} />
          </h4>
          {data.triage.needed ? (
            <p>
              Suggestion only, not executed: <strong>{data.triage.playbook}</strong>
              {data.triage.playbook_label ? ` (${data.triage.playbook_label})` : ''}
              {data.triage.confidence !== null ? `, confidence ${Math.round(data.triage.confidence * 100)}%` : ''}.{' '}
              {data.triage.reason}
            </p>
          ) : (
            <p className="small">{data.triage.reason}</p>
          )}

          <h4>
            Supplier reply draft <SourcePill source={data.reply_draft.source} />
          </h4>
          <label className="small muted" htmlFor="advisory-reply">
            Editable draft. Not sent: replies stay queued in the outbox until a person approves them.
          </label>
          <textarea
            id="advisory-reply"
            className="advisory-draft"
            rows={8}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
          />
          {data.model ? <p className="small muted">Model: {data.model}</p> : null}
        </>
      ) : null}
    </Card>
  );
}
