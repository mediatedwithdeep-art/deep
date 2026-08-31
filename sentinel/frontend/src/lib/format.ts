/** Formatting helpers shared across pages. */

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return '—'
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000)
  if (seconds < 10) return 'now'
  if (seconds < 60) return `${Math.floor(seconds)}s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}

export function clockTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleTimeString('en-IN', {
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  })
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('en-IN', {
    day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
    second: '2-digit', hour12: false,
  })
}

export function duration(seconds: number | null | undefined): string {
  if (seconds == null) return '—'
  if (seconds < 60) return `${Math.round(seconds)}s`
  const m = Math.floor(seconds / 60)
  if (m < 60) return `${m}m ${Math.round(seconds % 60)}s`
  return `${Math.floor(m / 60)}h ${m % 60}m`
}

export function distance(metres: number | null | undefined): string {
  if (metres == null) return '—'
  return metres < 1000 ? `${Math.round(metres)} m` : `${(metres / 1000).toFixed(2)} km`
}

export function severityClass(severity: string): string {
  return ({ CRITICAL: 'crit', HIGH: 'high', MEDIUM: 'warn',
            LOW: 'info', INFO: 'neutral' } as Record<string, string>)[severity] || 'neutral'
}

export function statusClass(status: string): string {
  return ({ ONLINE: 'ok', DEGRADED: 'warn', OFFLINE: 'crit',
            PENDING: 'neutral', PROBING: 'info' } as Record<string, string>)[status] || 'neutral'
}

/** Confidence as a percentage, always with its band.
 *
 *  A bare "0.62" tells an operator nothing about whether to act. Pairing
 *  the number with a word is the difference between a usable reading and
 *  a decoration.
 */
export function confidence(value: number | null | undefined): { pct: string; band: string; cls: string } {
  if (value == null) return { pct: '—', band: 'unknown', cls: 'neutral' }
  const pct = `${Math.round(value * 100)}%`
  if (value >= 0.85) return { pct, band: 'high', cls: 'ok' }
  if (value >= 0.6) return { pct, band: 'moderate', cls: 'warn' }
  return { pct, band: 'low', cls: 'high' }
}

export const VEHICLE_LABEL: Record<string, string> = {
  car: 'Car', motorcycle: 'Motorcycle', auto_rickshaw: 'Auto-rickshaw',
  truck: 'Truck', bus: 'Bus', tractor: 'Tractor', bicycle: 'Bicycle',
  unknown: 'Unknown',
}

export const COLOUR_SWATCH: Record<string, string> = {
  white: '#e8edf4', silver: '#b0b2b6', grey: '#787c82', black: '#2d2f33',
  red: '#c0392b', blue: '#2a4a8c', green: '#2c7a44', yellow: '#d2b432',
  brown: '#6e4c32', orange: '#cd6e28', other: '#6b7a8d',
}
