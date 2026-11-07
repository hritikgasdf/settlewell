import type { IncidentList } from '../types';
import { Empty, Pill } from './Panels';

interface Props {
  list: IncidentList | null;
  error: string | null;
  loading: boolean;
  selectedId: number | null;
  onSelect: (id: number) => void;
  onRetry: () => void;
}

export function IncidentListPanel({ list, error, loading, selectedId, onSelect, onRetry }: Props) {
  return (
    <section className="card" aria-labelledby="incidents-title">
      <h2 id="incidents-title" className="card-title">
        Incidents
      </h2>
      <div className="card-body">
        {loading && list === null ? <p className="muted" role="status">Loading incidents…</p> : null}

        {error ? (
          <div className="alert notice-error" role="alert">
            <p>Could not load incidents: {error}</p>
            <button type="button" className="btn" onClick={onRetry}>
              Retry
            </button>
          </div>
        ) : null}

        {list !== null && list.incidents.length === 0 ? (
          <Empty>No incidents yet. Stop and restart the backend against an empty database to seed the demo.</Empty>
        ) : null}

        {list !== null && list.incidents.length > 0 ? (
          <ul className="incident-list" aria-label="Incidents">
            {list.incidents.map((i) => {
              const selected = i.id === selectedId;
              return (
                <li key={i.id}>
                  <button
                    type="button"
                    className={selected ? 'incident-item selected' : 'incident-item'}
                    aria-current={selected ? 'true' : undefined}
                    onClick={() => onSelect(i.id)}
                  >
                    <span className="incident-top">
                      <strong>{i.invoice_ref}</strong>
                      <span className="money">
                        {i.amount} {i.currency}
                      </span>
                    </span>
                    <span className="muted small">{i.supplier}</span>
                    <span className="incident-top">
                      <Pill>{i.state}</Pill>
                      {i.pending_proposals > 0 ? (
                        <Pill tone="accent">
                          {i.pending_proposals} pending {i.pending_proposals === 1 ? 'proposal' : 'proposals'}
                        </Pill>
                      ) : null}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        ) : null}
      </div>
    </section>
  );
}
