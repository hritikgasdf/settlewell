// Response types for the local demo API. Source: /workspace/settlewell-ctx/p15-api-contract.md.
// Money is always a decimal string. Nullable fields are typed `| null`; the UI renders null as "unknown" or "none".

export interface Session {
  operator: string;
  csrf_token: string;
  csrf_header: string;
  mode: string;
  banner: string;
  writes_enabled: boolean;
  note: string;
}

export interface IncidentSummary {
  id: number;
  state: string;
  invoice_ref: string;
  amount: string;
  currency: string;
  obligation_status: string;
  supplier: string;
  pending_proposals: number;
}

export interface IncidentList {
  mode: string;
  banner: string;
  incidents: IncidentSummary[];
}

// The claim object shape is not fixed by the contract; the UI renders whatever keys it finds.
export type Claim = Record<string, unknown>;

export interface Obligation {
  id: number;
  invoice_ref: string;
  amount: string;
  currency: string;
  status: string;
  live_transfer_id: number | null;
}

export interface Supplier {
  id: number;
  name: string;
  contact: string | null;
}

export interface BankRecord {
  id: number;
  beneficiary_id: number;
  details_hash: string;
  bank_name: string;
  account_last4: string;
  source: string; // onboarding | email_claim | verified_callback
  verified_by: string | null;
  verified_at: string | null;
}

export interface Transfer {
  id: number;
  airwallex_id: string | null;
  request_id: string | null;
  origin: string;
  amount: string;
  currency: string;
  fee: string | null;
  status: string;
  failure_code: string | null;
  failure_message: string | null;
  dispatch_date: string | null;
  reference: string | null;
  details_hash: string | null;
  is_live: boolean;
  cancellable: boolean;
}

export interface StatusHistoryRow {
  transfer_record_id: number;
  status: string;
  failure_code: string | null;
  source: string;
  seen_at: string;
}

export interface Decision {
  id: number;
  rule_id: string;
  proposed_action: string;
  policy_verdict: string; // "seeded_demo" for the seeded row
  llm_rationale: string | null;
  inputs_hash: string;
}

export interface Prerequisite {
  name: string;
  ok: boolean | null; // null = checked at execute time
  detail: string;
}

export interface Proposal {
  id: number;
  incident_id: number;
  kind: string; // cancel | replacement | fx_conversion | beneficiary
  action: string; // e.g. STOP_DUPLICATE
  state: string; // proposed | executed | refused
  amount: string | null;
  currency: string | null;
  beneficiary_id: number | null;
  details_hash: string | null;
  quote_id: string | null;
  target_id: number | null;
  failure_code: string | null;
  expires_at: string | null;
  created_at: string | null;
  remote_id: string | null;
  remote_status: string | null;
  /** Present only while approval.state is "pending". Send it back on POST. */
  binding_hash: string | null;
  approval: {
    id: number;
    state: string; // pending | consumed | voided
    proposer: string | null;
    approver: string | null;
    used_at: string | null;
  };
  prerequisites: Prerequisite[];
  blocked_reasons: string[];
  can_approve: boolean;
  can_reject: boolean;
}

export interface Payout {
  intent_id: number;
  currency: string;
  amount: string | null;
  fee_budget: string | null;
  reserve_floor: string | null;
  available_before: string | null;
  state: string | null;
  hold_state: string | null;
  after_state: string | null;
}

export interface Balance {
  currency: string;
  available: string;
  label: string;
  taken_at: string;
}

export interface ProofDocument {
  id: number;
  transfer_record_id: number;
  format: string;
  status_at_issue: string;
  sha256: string;
  sent_at: string | null;
  /** Same-origin relative URL, present only when the bytes exist and the document is eligible now. */
  pdf_url: string | null;
  /** Null when linkable; otherwise why the stored document is withheld right now. */
  refusal: string | null;
  /** "mock_synthetic" for seeded demo documents; null when no bytes are stored. */
  origin: string | null;
  /** Label to show beside the link, e.g. "MOCK / SYNTHETIC - not Airwallex proof". */
  label: string | null;
}

export interface AuditRow {
  id: number;
  kind: string;
  detail: string | null;
  created_at: string;
}

export interface IncidentDetail {
  mode: string;
  banner: string;
  incident: {
    id: number;
    state: string;
    claim: Claim | null;
    evidence: { claim_recorded: boolean; claim_matched: boolean };
  };
  obligation: Obligation;
  supplier: Supplier;
  pinned_bank: BankRecord | null;
  bank_versions: BankRecord[];
  transfers: Transfer[];
  status_history: StatusHistoryRow[];
  decisions: Decision[];
  proposals: Proposal[];
  funding: { payouts: Payout[]; balances: Balance[] };
  proof: {
    pdf_link: string | null;
    reason: string;
    documents: ProofDocument[];
  };
  ledger: {
    obligation_amount: string;
    currency: string;
    fees: { transfer_id: number; fee: string }[];
  };
  audit: AuditRow[];
}

export interface ActionResult {
  kind: string;
  remote_id: string | null;
  status: string | null;
  funding_note: string | null;
}

/** Shape of approve and of the transfer cancel route. */
export interface ProposalActionResponse {
  ok: boolean;
  incident_id: number;
  result: ActionResult;
  intent: Proposal;
}

export interface RejectResponse {
  ok: boolean;
  incident_id: number;
  intent: Proposal;
}
