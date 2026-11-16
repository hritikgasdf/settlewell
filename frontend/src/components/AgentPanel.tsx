import type { Autonomy } from '../types';
import { Card, Empty, Pill } from './Panels';

interface Props {
  autonomy: Autonomy | undefined;
  writesEnabled: boolean;
  busy: string | null;
  onRunAgent: () => void;
}

const STATE_TEXT: Record<string, string> = {
  done: 'done',
  scheduled: 'recheck scheduled',
  auto_eligible: 'auto-eligible',
  waiting: 'waiting for a human',
  approved: 'approved',
  refused: 'refused',
};

function seconds(a: string | null, b: string | null): string | null {
  if (!a || !b) return null;
  const ms = Date.parse(b) - Date.parse(a);
  if (!Number.isFinite(ms) || ms < 0) return null;
  const s = Math.round(ms / 1000);
  return s < 120 ? `${s}s` : `${Math.round(s / 60)} min`;
}

export function AgentPanel({ autonomy: a, writesEnabled, busy, onRunAgent }: Props) {
  if (!a) return null;
  const eligible = a.steps.some((s) => s.state === 'auto_eligible');
  const elapsed = seconds(a.first_at, a.last_at);
  return (
    <Card id="agent-title" title="Agent handled it">
      <p className="agent-counter" data-testid="agent-counter">
        <strong>
          {a.auto} of {a.total} steps handled automatically
        </strong>
        , {a.human} needed a human
        {a.human_reasons.length > 0 ? ` (${a.human_reasons.join('; ')})` : ''}.
        {elapsed ? <span className="muted small"> Recorded span: {elapsed}.</span> : null}
      </p>
      <p className="small muted">
        {a.enabled
          ? `Autonomy on. The agent acts on its own when risk is low and asks a human only when money is at risk. Auto limit ${a.threshold_currency} ${a.auto_max_amount}. Rules come from a fixed, code-defined policy table; the AI cannot change them.`
          : 'Autonomy is off (SETTLEWELL_AUTONOMY=0): every step needs a person, exactly as before.'}
      </p>
      {a.steps.length === 0 ? (
        <Empty>No steps recorded yet.</Empty>
      ) : (
        <ol className="agent-timeline">
          {a.steps.map((s, i) => (
            <li key={`${s.kind}-${i}`} className={`agent-step ${s.tier}`} data-tier={s.tier}>
              <Pill tone={s.tier === 'auto' ? 'accent' : 'warn'}>{s.tier === 'auto' ? 'Agent' : 'Human'}</Pill>{' '}
              <strong>{s.title}</strong> <span className="muted small">· {STATE_TEXT[s.state] ?? s.state}</span>
              <div className="small muted">
                rule <code>{s.rule_id}</code>
                {s.actor && s.actor !== 'agent' && s.actor !== 'human' ? (
                  <>
                    {' '}
                    · approver <code>{s.actor}</code>
                  </>
                ) : null}
                {s.at ? ` · ${s.at}` : ''}
                {s.detail ? ` · ${s.detail}` : ''}
              </div>
            </li>
          ))}
        </ol>
      )}
      {a.enabled && eligible ? (
        <button
          type="button"
          className="btn primary"
          disabled={!writesEnabled || busy !== null}
          onClick={onRunAgent}
        >
          {busy === 'agent' ? 'Agent working…' : 'Let the agent handle low-risk steps'}
        </button>
      ) : null}
      <details className="small">
        <summary>Policy table (auto vs human)</summary>
        <table className="table">
          <thead>
            <tr>
              <th>Action</th>
              <th>Who</th>
              <th>Rule</th>
              <th>Why</th>
            </tr>
          </thead>
          <tbody>
            {a.policy.map((r) => (
              <tr key={r.action}>
                <td>{r.action}</td>
                <td>{r.tier === 'auto' ? 'Agent' : 'Human'}</td>
                <td>
                  <code>{r.rule_id}</code>
                </td>
                <td>{r.summary}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </Card>
  );
}
