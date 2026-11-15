import { useState } from 'react';
import type { BankRecord, IncidentDetail as Detail, Proposal, Transfer } from '../types';
import { Card, Empty, Field, Pill, show } from './Panels';
import { ProposalCard } from './ProposalCard';
import { TransferCard } from './TransferCard';

interface Props {
  detail: Detail | null;
  loading: boolean;
  error: string | null;
  hasSelection: boolean;
  busy: string | null;
  writesEnabled: boolean;
  onApprove: (p: Proposal) => void;
  onReject: (p: Proposal, reason: string) => void;
  onCancel: (t: Transfer) => void;
  onRetry: () => void;
}

type TabKey = 'evidence' | 'history' | 'decisions' | 'audit';
const TABS: { key: TabKey; label: string }[] = [
  { key: 'evidence', label: 'Evidence' },
  { key: 'history', label: 'Status history' },
  { key: 'decisions', label: 'Decisions' },
  { key: 'audit', label: 'Audit' },
];

const SOURCE_LABEL: Record<string, string> = {
  onboarding: 'Onboarding record',
  verified_callback: 'Verified by call-back',
  email_claim: 'Untrusted email version. Not pinned, not used for payment.',
};

function BankLine({ b, pinnedId }: { b: BankRecord; pinnedId: number | null }) {
  const isPinned = pinnedId !== null && b.id === pinnedId;
  return (
    <li className={isPinned ? 'bank-row pinned' : 'bank-row'}>
      <div>
        <strong>{b.bank_name}</strong> · account ending {b.account_last4}
      </div>
      <div className="small muted">
        {SOURCE_LABEL[b.source] ?? `Source: ${b.source}`}
        {b.verified_by ? ` · verified by ${b.verified_by}` : ''}
        {b.verified_at ? ` at ${b.verified_at}` : ''}
      </div>
      <div className="small">
        <Pill tone={isPinned ? 'accent' : b.source === 'email_claim' ? 'warn' : 'neutral'}>
          {isPinned ? 'Pinned (used for payment)' : 'Not pinned'}
        </Pill>{' '}
        <code className="small">hash {b.details_hash.slice(0, 12)}…</code>
      </div>
    </li>
  );
}

function ClaimView({ claim }: { claim: Record<string, unknown> }) {
  const entries = Object.entries(claim);
  if (entries.length === 0) return <Empty>The claim record is empty.</Empty>;
  return (
    <dl className="kv">
      {entries.map(([k, v]) => (
        <Field key={k} label={k}>
          {v !== null && typeof v === 'object' ? <pre className="pre">{JSON.stringify(v, null, 2)}</pre> : show(v as string)}
        </Field>
      ))}
    </dl>
  );
}

// Only the API's own same-origin proof route. Rejects protocol-relative (//), absolute, javascript:, data: and anything else.
const SAFE_PROOF_URL = /^\/api\/incidents\/\d+\/proof\/\d+\/pdf$/;

function isSafeProofUrl(link: string): boolean {
  return SAFE_PROOF_URL.test(link) && !link.startsWith('//');
}

const REFUSAL_TEXT: Record<string, string> = {
  no_stored_bytes: 'no stored PDF bytes',
  hash_mismatch: 'stored bytes fail the hash check',
  not_live_transfer: 'this is not the live transfer',
  webhook_pending: 'a webhook settle window is still open',
  status_not_eligible: 'the current status does not allow a proof',
  remote_unknown: 'the remote status could not be read',
};

function refusalText(code: string | null, status?: string): string {
  if (code === null) return 'eligible';
  if (code === 'status_not_eligible' && status) {
    const returned = status === 'CANCELLED' || status === 'FAILED';
    return `withheld: the payment it covers is now ${status}, and that status does not allow a proof${
      returned ? ' (a returned or cancelled payment is never shown as paid)' : ''
    }`;
  }
  return `withheld: ${REFUSAL_TEXT[code] ?? code}`;
}

type ParseInfo = { source?: unknown; model?: unknown; rule_id?: unknown; summary?: unknown };

function ParseSource({ parse }: { parse: ParseInfo | undefined }) {
  if (!parse || typeof parse !== 'object') {
    return <p className="small muted">Parse source: not recorded for this claim.</p>;
  }
  const label =
    parse.source === 'claude'
      ? `Claude${typeof parse.model === 'string' && parse.model ? ` (${parse.model})` : ''}`
      : 'Non-model parser (no Claude call)';
  return (
    <p className="small">
      <strong>Parsed by {label}</strong>
      {typeof parse.rule_id === 'string' ? ` · rule ${parse.rule_id}` : ''}
      {typeof parse.summary === 'string' ? <span className="muted"> · {parse.summary}</span> : null}
    </p>
  );
}

export function IncidentDetailPanel(props: Props) {
  const { detail: d, loading, error, hasSelection, busy, writesEnabled } = props;
  const [tab, setTab] = useState<TabKey>('evidence');

  if (!hasSelection) {
    return (
      <section className="card">
        <Empty>Select an incident on the left.</Empty>
      </section>
    );
  }
  if (error) {
    return (
      <div className="alert notice-error" role="alert">
        <p>Could not load this incident: {error}</p>
        <button type="button" className="btn" onClick={props.onRetry}>
          Retry
        </button>
      </div>
    );
  }
  if (d === null) {
    return <p className="muted" role="status">{loading ? 'Loading incident…' : 'No incident loaded.'}</p>;
  }

  const pinnedId = d.pinned_bank ? d.pinned_bank.id : null;
  const hasFunding = d.funding.payouts.length > 0 || d.funding.balances.length > 0;
  const pdfLink = d.proof.pdf_link;
  const linkedDoc = pdfLink !== null ? d.proof.documents.find((doc) => doc.pdf_url === pdfLink) ?? null : null;
  const live = d.mode === 'SANDBOX';
  const claimFields = d.incident.claim
    ? Object.fromEntries(Object.entries(d.incident.claim).filter(([k]) => k !== 'parse' && k !== 'matched'))
    : null;
  const parseInfo = d.incident.claim ? (d.incident.claim.parse as ParseInfo | undefined) : undefined;
  const statusOf = (recordId: number) => d.transfers.find((t) => t.id === recordId)?.status;
  const mockProof = d.proof.documents.some((doc) => doc.origin === 'mock_synthetic') || (!live && d.proof.documents.length === 0);
  const letterProof = d.proof.documents.some((doc) => doc.origin === 'airwallex_confirmation_letter');

  return (
    <div className="detail">
      {loading ? <p className="muted small" role="status">Refreshing…</p> : null}

      <section className="hero card" aria-labelledby="hero-title">
        <div className="hero-top">
          <div>
            <p className="eyebrow">Incident {d.incident.id}</p>
            <h2 id="hero-title">
              {d.obligation.invoice_ref} · {d.obligation.amount} {d.obligation.currency}
            </h2>
            <p className="muted">
              {d.supplier.name}
              {d.supplier.contact ? ` · ${d.supplier.contact}` : ''}
            </p>
          </div>
          <div className="pills">
            <Pill>Incident {d.incident.state}</Pill>
            <Pill>Obligation {d.obligation.status}</Pill>
          </div>
        </div>
        {live ? (
          <p className="demo-note small">
            Live Airwallex sandbox run (virtual funds): the payments and events below are recorded from Airwallex sandbox
            calls, not seeded demo data.
          </p>
        ) : (
          <p className="demo-note small">
            Seeded demo data. The demo scenario is a STOP_DUPLICATE cancel of a scheduled duplicate, not a replacement.
          </p>
        )}
      </section>

      <Card id="claim-title" title="Claim">
        {d.incident.claim !== null ? (
          <>
            {d.incident.evidence.claim_matched ? (
              <p className="small">
                <Pill tone="accent">Matched pinned claim</Pill>
              </p>
            ) : null}
            <ParseSource parse={parseInfo} />
            <ClaimView claim={claimFields ?? {}} />
          </>
        ) : (
          <Empty>
            No claim is recorded for this incident (claim parser not run). Evidence:{' '}
            {d.incident.evidence.claim_recorded ? 'claim_recorded true' : 'claim_recorded false'}.
          </Empty>
        )}
      </Card>

      <Card id="bank-title" title="Bank details">
        {d.pinned_bank ? (
          <p className="small">
            <strong>Pinned bank</strong>
            {d.pinned_bank.beneficiary_id
              ? ` (beneficiary ${d.pinned_bank.beneficiary_id})`
              : d.pinned_bank.bank_name
                ? ` (${d.pinned_bank.bank_name})`
                : ''}{' '}
            is the only version used for payment.
          </p>
        ) : (
          <p className="alert notice-warn">No pinned bank is recorded for this supplier. Nothing can be paid to a new account.</p>
        )}
        {d.bank_versions.length > 0 ? (
          <ul className="bank-list" aria-label="Bank versions">
            {d.bank_versions.map((b) => (
              <BankLine key={b.id} b={b} pinnedId={pinnedId} />
            ))}
          </ul>
        ) : (
          <Empty>No bank versions recorded.</Empty>
        )}
      </Card>

      <Card id="payments-title" title="Payments">
        {d.transfers.length > 0 ? (
          d.transfers.map((t) => (
            <TransferCard key={t.id} transfer={t} busy={busy} writesEnabled={writesEnabled} onCancel={props.onCancel} />
          ))
        ) : (
          <Empty>No payments recorded.</Empty>
        )}
      </Card>

      <Card id="funding-title" title="Funding check">
        {hasFunding ? (
          <>
            {d.funding.payouts.length > 0 ? (
              <table className="table">
                <thead>
                  <tr>
                    <th scope="col">Intent</th>
                    <th scope="col">Amount</th>
                    <th scope="col">Available before</th>
                    <th scope="col">Hold</th>
                    <th scope="col">State</th>
                    <th scope="col">After</th>
                  </tr>
                </thead>
                <tbody>
                  {d.funding.payouts.map((p) => (
                    <tr key={p.intent_id}>
                      <td>#{p.intent_id}</td>
                      <td className="money">
                        {show(p.amount)} {p.currency}
                      </td>
                      <td className="money">{show(p.available_before)}</td>
                      <td>{show(p.hold_state)}</td>
                      <td>{show(p.state)}</td>
                      <td>{show(p.after_state)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <Empty>No payout holds recorded.</Empty>
            )}
            {d.funding.balances.length > 0 ? (
              <ul className="balances">
                {d.funding.balances.map((b) => (
                  <li key={`${b.currency}-${b.taken_at}`}>
                    <span className="money">
                      {b.available} {b.currency}
                    </span>{' '}
                    available · {b.label} · taken {b.taken_at}
                  </li>
                ))}
              </ul>
            ) : (
              <Empty>No balance snapshots recorded. Balance is unknown.</Empty>
            )}
          </>
        ) : (
          <Empty>
            No funding snapshots recorded. Funding is not verified, so nothing here is shown as funded.
          </Empty>
        )}
        <p className="small muted">
          Ledger: obligation {d.ledger.obligation_amount} {d.ledger.currency}
          {d.ledger.fees.length > 0
            ? `; fees ${d.ledger.fees.map((f) => `${f.fee} on transfer ${f.transfer_id}`).join(', ')}`
            : '; no fees recorded'}
          . Unknown values stay unknown.
        </p>
      </Card>

      <Card id="proof-title" title="Proof">
        {pdfLink !== null && isSafeProofUrl(pdfLink) ? (
          <p>
            <a href={pdfLink} target="_blank" rel="noopener noreferrer">
              Open proof PDF
            </a>{' '}
            <span className="small muted">{linkedDoc?.label ?? 'Label unavailable. Do not treat as Airwallex proof.'}</span>
          </p>
        ) : (
          <p className="small">No proof PDF is available. {d.proof.reason ?? 'No stored document is currently eligible.'}</p>
        )}
        {d.proof.documents.length > 0 ? (
          <ul className="plain-list small">
            {d.proof.documents.map((doc) => (
              <li key={doc.id}>
                Document {doc.id} ({doc.format}, status at issue {doc.status_at_issue}): {refusalText(doc.refusal, statusOf(doc.transfer_record_id))}
              </li>
            ))}
          </ul>
        ) : null}
        <p className="small muted">
          {mockProof
            ? 'Stored proof is a MOCK / SYNTHETIC demo document. It is not Airwallex proof and not bank or network confirmation. '
            : letterProof
              ? 'Stored proof is an Airwallex sandbox confirmation letter (virtual funds), not production bank confirmation. '
              : ''}
          If the status changed after this page loaded, the link can return an error; reload the incident.
        </p>
      </Card>

      <Card id="proposals-title" title="Proposals">
        {d.proposals.length > 0 ? (
          d.proposals.map((p) => (
            <ProposalCard
              key={p.id}
              proposal={p}
              busy={busy}
              writesEnabled={writesEnabled}
              onApprove={props.onApprove}
              onReject={props.onReject}
            />
          ))
        ) : (
          <Empty>No proposals for this incident.</Empty>
        )}
      </Card>

      <section className="card" aria-labelledby="records-title">
        <h3 id="records-title" className="card-title">
          Records
        </h3>
        <div className="card-body">
          <div className="tabs" role="tablist" aria-label="Record sections">
            {TABS.map((t) => (
              <button
                key={t.key}
                type="button"
                role="tab"
                id={`tab-${t.key}`}
                aria-selected={tab === t.key}
                aria-controls="tab-panel"
                className={tab === t.key ? 'tab active' : 'tab'}
                onClick={() => setTab(t.key)}
              >
                {t.label}
              </button>
            ))}
          </div>
          <div role="tabpanel" id="tab-panel" aria-labelledby={`tab-${tab}`} className="tab-panel">
            {tab === 'evidence' ? (
              d.proof.documents.length > 0 ? (
                <ul className="plain-list">
                  {d.proof.documents.map((doc) => (
                    <li key={doc.id}>
                      Proof document {doc.id}: {doc.format}, status at issue {doc.status_at_issue}, sent {show(doc.sent_at, 'not sent')}, sha256{' '}
                      <code>{doc.sha256.slice(0, 16)}…</code>
                    </li>
                  ))}
                </ul>
              ) : (
                <Empty>No proof documents recorded.</Empty>
              )
            ) : null}

            {tab === 'history' ? (
              d.status_history.length > 0 ? (
                <table className="table">
                  <thead>
                    <tr>
                      <th scope="col">Transfer record</th>
                      <th scope="col">Status</th>
                      <th scope="col">Failure</th>
                      <th scope="col">Source</th>
                      <th scope="col">Seen</th>
                    </tr>
                  </thead>
                  <tbody>
                    {d.status_history.map((h, i) => (
                      <tr key={`${h.transfer_record_id}-${h.seen_at}-${i}`}>
                        <td>#{h.transfer_record_id}</td>
                        <td>{h.status}</td>
                        <td>{show(h.failure_code, 'none')}</td>
                        <td>{h.source}</td>
                        <td>{h.seen_at}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <Empty>No status history recorded.</Empty>
              )
            ) : null}

            {tab === 'decisions' ? (
              d.decisions.length > 0 ? (
                <ul className="plain-list">
                  {d.decisions.map((x) => (
                    <li key={x.id}>
                      <strong>{x.rule_id}</strong> → {x.proposed_action} · verdict{' '}
                      {x.policy_verdict === 'seeded_demo' ? 'seeded_demo (demo row, not a policy run)' : x.policy_verdict}
                      <div className="small muted">
                        Rationale: {show(x.llm_rationale, 'none recorded')} · inputs <code>{x.inputs_hash.slice(0, 12)}…</code>
                      </div>
                    </li>
                  ))}
                </ul>
              ) : (
                <Empty>
                  {d.proposals.length > 0
                    ? `No policy-engine decisions were recorded for this incident. Its proposals are listed above (${
                        d.proposals.length
                      }) and its recorded steps are in the Audit tab (${d.audit.length} entries).`
                    : 'No decisions recorded.'}
                </Empty>
              )
            ) : null}

            {tab === 'audit' ? (
              d.audit.length > 0 ? (
                <ul className="plain-list">
                  {d.audit.map((a) => (
                    <li key={a.id}>
                      <code>{a.kind}</code> · {a.created_at}
                      {a.detail ? <div className="small muted">{a.detail}</div> : null}
                    </li>
                  ))}
                </ul>
              ) : (
                <Empty>No audit rows.</Empty>
              )
            ) : null}
          </div>
        </div>
      </section>
    </div>
  );
}
