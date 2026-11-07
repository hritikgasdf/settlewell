import type { ReactNode } from 'react';

export function Card({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section className="card" aria-labelledby={id}>
      <h3 id={id} className="card-title">
        {title}
      </h3>
      <div className="card-body">{children}</div>
    </section>
  );
}

export type Tone = 'neutral' | 'accent' | 'warn' | 'bad';

export function Pill({ children, tone = 'neutral' }: { children: ReactNode; tone?: Tone }) {
  return <span className={`pill pill-${tone}`}>{children}</span>;
}

/** Renders a value. Missing values show as "unknown" so absent evidence is never shown as success. */
export function show(v: string | number | boolean | null | undefined, empty = 'unknown'): string {
  if (v === null || v === undefined || v === '') return empty;
  return String(v);
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="muted empty">{children}</p>;
}
