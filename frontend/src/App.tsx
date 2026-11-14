import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ApiError,
  approveProposal,
  cancelTransfer,
  describeError,
  getIncident,
  listIncidents,
  loadSession,
  rejectProposal,
  resetDemo,
} from './api';
import { IncidentListPanel } from './components/IncidentList';
import { IncidentDetailPanel } from './components/IncidentDetail';
import type { IncidentDetail, IncidentList, Proposal, Session, Transfer } from './types';

interface Notice {
  tone: 'ok' | 'error' | 'unknown';
  text: string;
}

/** Plain-language note under the banner for the API mode it reports. */
function modeNote(mode: string | null): string {
  switch (mode) {
    case 'MOCK':
      return 'Synthetic data. No live money moves. Writes go to the local MOCK ledger only.';
    case 'SANDBOX':
      return 'Airwallex sandbox: virtual funds only. Not production and not a live bank.';
    case 'READ_ONLY':
      return 'Read only. The API has writes disabled, so actions are unavailable.';
    default:
      return 'API mode not known yet. Actions stay disabled until the API reports its mode.';
  }
}

export default function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [sessionError, setSessionError] = useState<string | null>(null);

  const [list, setList] = useState<IncidentList | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [listLoading, setListLoading] = useState(true);

  const [selectedId, setSelectedId] = useState<number | null>(null);
  const selectedRef = useRef<number | null>(null);
  selectedRef.current = selectedId;

  const [detail, setDetail] = useState<IncidentDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);

  const refreshList = useCallback(async (): Promise<void> => {
    setListLoading(true);
    try {
      const l = await listIncidents();
      setList(l);
      setListError(null);
    } catch (e) {
      setListError(describeError(e));
    } finally {
      setListLoading(false);
    }
  }, []);

  const refreshDetail = useCallback(async (id: number): Promise<void> => {
    setDetailLoading(true);
    try {
      const d = await getIncident(id);
      // Ignore responses for an incident the operator has already navigated away from.
      if (selectedRef.current === id) {
        setDetail(d);
        setDetailError(null);
      }
    } catch (e) {
      if (selectedRef.current === id) {
        setDetail(null);
        setDetailError(describeError(e));
      }
    } finally {
      setDetailLoading(false);
    }
  }, []);

  // Initial load: per-page CSRF session plus the incident list.
  useEffect(() => {
    loadSession()
      .then(setSession)
      .catch((e: unknown) => setSessionError(describeError(e)));
    listIncidents()
      .then((l) => {
        setList(l);
        setListError(null);
        setSelectedId((cur) => cur ?? (l.incidents.length > 0 ? l.incidents[0].id : null));
      })
      .catch((e: unknown) => setListError(describeError(e)))
      .finally(() => setListLoading(false));
  }, []);

  useEffect(() => {
    if (selectedId === null) {
      setDetail(null);
      return;
    }
    setDetail(null);
    setDetailError(null);
    void refreshDetail(selectedId);
  }, [selectedId, refreshDetail]);

  // Every mutation refetches the detail and the list, on success or failure. It never retries.
  const runAction = async (incidentId: number, key: string, action: () => Promise<string>): Promise<void> => {
    setBusy(key);
    setNotice(null);
    try {
      const text = await action();
      setNotice({ tone: 'ok', text });
    } catch (e) {
      const unknown = e instanceof ApiError && e.outcomeUnknown;
      setNotice(
        unknown
          ? {
              tone: 'unknown',
              text: `Outcome unknown: ${describeError(e)}. Nothing was retried. The page was refreshed; check the status below before doing anything else.`,
            }
          : { tone: 'error', text: `Not applied: ${describeError(e)}` },
      );
    } finally {
      await Promise.all([refreshList(), refreshDetail(incidentId)]);
      setBusy(null);
    }
  };

  const handleApprove = (incidentId: number, p: Proposal): void => {
    void runAction(incidentId, `approve:${p.id}`, async () => {
      if (!p.binding_hash) throw new Error('This proposal has no binding hash. Reload and check its state.');
      const r = await approveProposal(incidentId, p.id, p.binding_hash);
      const extra = r.result.funding_note ? ` ${r.result.funding_note}` : '';
      const remote = r.result.remote_id ? ` (remote ${r.result.remote_id})` : '';
      return `Approved and executed: ${r.result.kind} returned ${r.result.status ?? 'no status'}${remote}.${extra}`;
    });
  };

  const handleReject = (incidentId: number, p: Proposal, reason: string): void => {
    void runAction(incidentId, `reject:${p.id}`, async () => {
      if (!p.binding_hash) throw new Error('This proposal has no binding hash. Reload and check its state.');
      await rejectProposal(incidentId, p.id, p.binding_hash, reason);
      return 'Proposal rejected. Nothing was sent to the bank.';
    });
  };

  const handleCancel = (incidentId: number, t: Transfer): void => {
    void runAction(incidentId, `cancel:${t.id}`, async () => {
      const r = await cancelTransfer(incidentId, t.id);
      const remote = r.result.remote_id ? ` (remote ${r.result.remote_id})` : '';
      return `Cancel executed: result ${r.result.status ?? 'no status'}${remote}.`;
    });
  };

  const handleReset = (): void => {
    if (!window.confirm('Reset the demo to its starting state? This clears every action taken so far.')) return;
    setBusy('reset');
    setNotice(null);
    resetDemo()
      .then(async (r) => {
        setNotice({
          tone: 'ok',
          text:
            r.state === 'running'
              ? 'Sandbox rerun started in the background. Refresh in a few minutes to see the new run.'
              : 'Demo reset: INV-2291 is back to its starting state.',
        });
        const l = await listIncidents();
        setList(l);
        const first = l.incidents.length > 0 ? l.incidents[0].id : null;
        setSelectedId(first);
        if (first !== null) await refreshDetail(first);
      })
      .catch((e: unknown) => setNotice({ tone: 'error', text: `Reset not applied: ${describeError(e)}` }))
      .finally(() => setBusy(null));
  };

  // The API decides whether writes are allowed (writes_enabled); the console only mirrors its mode.
  const apiMode = detail?.mode ?? list?.mode ?? session?.mode ?? null;
  const writesEnabled =
    session !== null && session.writes_enabled && (apiMode === 'MOCK' || apiMode === 'SANDBOX');
  const banner = session?.banner ?? list?.banner ?? null;
  const apiDown = sessionError !== null || listError !== null;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-name">Settlewell</span>
          <span className="brand-sub">Operator console</span>
        </div>
        <button
          type="button"
          className="btn btn-reset"
          onClick={handleReset}
          disabled={!writesEnabled || busy !== null}
          title="Put the demo back to its starting state so it can be run again"
        >
          {busy === 'reset' ? 'Resetting…' : 'Reset demo'}
        </button>
        <span className={apiDown ? 'conn conn-down' : 'conn conn-up'} role="status">
          {apiDown ? 'API unreachable' : session ? 'Connected' : 'Connecting…'}
        </span>
      </header>

      <div className="banner" role="note" aria-label="Environment" data-mode={apiMode ?? 'unknown'}>
        <strong>{banner ?? 'API not connected. This console runs only against the local API.'}</strong>
        <span className="banner-note">{modeNote(apiMode)}</span>
      </div>

      <main className="layout">
        <aside className="col-list">
          <IncidentListPanel
            list={list}
            error={listError}
            loading={listLoading}
            selectedId={selectedId}
            onSelect={setSelectedId}
            onRetry={() => {
              void refreshList();
              if (selectedId !== null) void refreshDetail(selectedId);
            }}
          />
        </aside>

        <section className="col-detail" aria-live="polite">
          {notice ? (
            <div className={`alert notice-${notice.tone}`} role={notice.tone === 'ok' ? 'status' : 'alert'}>
              {notice.text}
            </div>
          ) : null}
          <IncidentDetailPanel
            detail={detail}
            loading={detailLoading}
            error={detailError}
            hasSelection={selectedId !== null}
            busy={busy}
            writesEnabled={writesEnabled}
            onApprove={(p) => detail && handleApprove(detail.incident.id, p)}
            onReject={(p, reason) => detail && handleReject(detail.incident.id, p, reason)}
            onCancel={(t) => detail && handleCancel(detail.incident.id, t)}
            onRetry={() => selectedId !== null && void refreshDetail(selectedId)}
          />
          {session?.note ? <p className="muted small footer-note">{session.note}</p> : null}
        </section>
      </main>
    </div>
  );
}
