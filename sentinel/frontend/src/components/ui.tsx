/** Small shared building blocks. */
import type { ReactNode } from 'react'
import { confidence, severityClass } from '../lib/format'

export function Panel({ title, actions, children, flush, style }: {
  title?: string; actions?: ReactNode; children: ReactNode
  flush?: boolean; style?: React.CSSProperties
}) {
  return (
    <div className="panel" style={style}>
      {title && (
        <div className="panel-head">
          <h2>{title}</h2>
          <div className="spacer" />
          {actions}
        </div>
      )}
      <div className={`panel-body${flush ? ' flush' : ''}`}>{children}</div>
    </div>
  )
}

export function Stat({ label, value, unit, sub, tone = '' }: {
  label: string; value: ReactNode; unit?: string; sub?: ReactNode; tone?: string
}) {
  return (
    <div className={`stat ${tone}`}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}{unit && <small> {unit}</small>}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  )
}

export function Badge({ tone = 'neutral', children }: { tone?: string; children: ReactNode }) {
  return <span className={`badge ${tone}`}>{children}</span>
}

export function SeverityBadge({ severity }: { severity: string }) {
  return <Badge tone={severityClass(severity)}>{severity}</Badge>
}

/** Confidence, never shown as a bare number.
 *
 *  An operator reading "0.62" has no way to know whether to act on it.
 *  The band word is what makes the number actionable.
 */
export function Confidence({ value }: { value: number | null | undefined }) {
  const c = confidence(value)
  return <Badge tone={c.cls}>{c.pct} {c.band}</Badge>
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>
}

export function Loading({ rows = 3 }: { rows?: number }) {
  return (
    <div className="col" style={{ padding: 14 }}>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="skeleton" style={{ height: 34 }} />
      ))}
    </div>
  )
}

/** A standing caveat wherever AI output is presented.
 *
 *  This is a safety control, not decoration. Cross-camera identity is
 *  probabilistic, and an officer acting on a "probable" match must be able
 *  to see that it is probable without digging.
 */
export function Caveat({ children }: { children: ReactNode }) {
  return (
    <div className="caveat">
      <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor">
        <path d="M8 1.5 15 14H1L8 1.5Zm0 4.2a.7.7 0 0 0-.7.75l.2 3.1a.5.5 0 0 0 1 0l.2-3.1A.7.7 0 0 0 8 5.7Zm0 5.3a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Z" />
      </svg>
      <div>{children}</div>
    </div>
  )
}

export function ColourDot({ colour }: { colour: string | null }) {
  if (!colour) return <span className="muted">—</span>
  const swatch: Record<string, string> = {
    white: '#e8edf4', silver: '#b0b2b6', grey: '#787c82', black: '#2d2f33',
    red: '#c0392b', blue: '#2a4a8c', green: '#2c7a44', yellow: '#d2b432',
    brown: '#6e4c32', orange: '#cd6e28',
  }
  return (
    <span className="row" style={{ gap: 6 }}>
      <span style={{
        width: 9, height: 9, borderRadius: 2, flex: 'none',
        background: swatch[colour] || '#6b7a8d',
        border: '1px solid rgba(255,255,255,.18)',
      }} />
      {colour}
    </span>
  )
}

export function Plate({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="muted small">no read</span>
  return <span className="plate">{value}</span>
}
