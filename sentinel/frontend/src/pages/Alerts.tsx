import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { endpoints, type Alert } from '../lib/api'
import { liveFeed } from '../lib/ws'
import { Panel, SeverityBadge, Badge, Empty, Loading, Plate, Caveat, Confidence } from '../components/ui'
import { dateTime, timeAgo } from '../lib/format'

const SEVERITIES = ['', 'CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'INFO']
const STATES = ['', 'NEW', 'ACKNOWLEDGED', 'RESOLVED', 'FALSE_POSITIVE']

export default function Alerts() {
  const [alerts, setAlerts] = useState<Alert[]>([])
  const [selected, setSelected] = useState<Alert | null>(null)
  const [severity, setSeverity] = useState('')
  const [state, setState] = useState('NEW')
  const [summary, setSummary] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const navigate = useNavigate()

  async function load() {
    const params = new URLSearchParams()
    if (severity) params.set('severity', severity)
    if (state) params.set('state', state)
    params.set('limit', '200')
    try {
      const [list, sum] = await Promise.all([
        endpoints.alerts(`?${params}`),
        endpoints.alertSummary(),
      ])
      setAlerts(list.items)
      setSummary(sum)
    } finally { setLoading(false) }
  }

  useEffect(() => { setLoading(true); load() }, [severity, state])

  useEffect(() => liveFeed.on((channel, data) => {
    if (channel !== 'alerts') return
    const a = data as Alert
    if (severity && a.severity !== severity) return
    if (state && state !== 'NEW') return
    setAlerts((prev) => [a, ...prev.filter((x) => x.alert_id !== a.alert_id)])
  }), [severity, state])

  async function acknowledge(alert: Alert, newState: string) {
    await endpoints.ackAlert(alert.alert_id, newState)
    setAlerts((prev) => prev.filter((a) => a.alert_id !== alert.alert_id))
    setSelected(null)
    load()
  }

  return (
    <div className="col" style={{ gap: 14 }}>
      {summary && (
        <div className="grid cols-4">
          <Panel><div className="stat-label">Open</div>
            <div className="stat-value">{summary.counts.open}</div></Panel>
          <Panel><div className="stat-label">Urgent (High / Critical)</div>
            <div className="stat-value" style={{ color: 'var(--crit)' }}>{summary.counts.urgent}</div></Panel>
          <Panel><div className="stat-label">Acknowledged</div>
            <div className="stat-value">{summary.counts.acknowledged}</div></Panel>
          <Panel>
            <div className="stat-label">False-positive rate</div>
            <div className="stat-value">
              {summary.false_positive_rate == null ? '—'
                : `${Math.round(summary.false_positive_rate * 100)}%`}
            </div>
            {/* Shown deliberately. The false-positive rate is the number that
                decides whether operators keep trusting the system, so hiding
                it would be the wrong kind of polish. */}
            <div className="stat-sub">of resolved alerts</div>
          </Panel>
        </div>
      )}

      <div className="toolbar">
        <div className="field" style={{ width: 170 }}>
          <label>Severity</label>
          <select value={severity} onChange={(e) => setSeverity(e.target.value)}>
            {SEVERITIES.map((s) => <option key={s} value={s}>{s || 'All severities'}</option>)}
          </select>
        </div>
        <div className="field" style={{ width: 170 }}>
          <label>State</label>
          <select value={state} onChange={(e) => setState(e.target.value)}>
            {STATES.map((s) => <option key={s} value={s}>{s || 'All states'}</option>)}
          </select>
        </div>
        <div className="spacer" />
        <button className="btn" onClick={load}>Refresh</button>
      </div>

      <div className="grid" style={{ gridTemplateColumns: selected ? 'minmax(0,1.5fr) minmax(0,1fr)' : '1fr' }}>
        <Panel title={`Alerts (${alerts.length})`} flush>
          {loading ? <Loading rows={8} /> : alerts.length === 0 ? (
            <Empty>No alerts match these filters.</Empty>
          ) : (
            <div style={{ maxHeight: 560, overflowY: 'auto' }}>
              {alerts.map((a) => (
                <div key={a.alert_id}
                     className={`alert-row ${a.severity}`}
                     onClick={() => setSelected(a)}>
                  <div className="alert-rail" />
                  <div style={{ minWidth: 0 }}>
                    <div className="alert-title">{a.title}</div>
                    <div className="alert-meta">
                      <SeverityBadge severity={a.severity} />
                      <Badge>{a.alert_type.replace(/_/g, ' ')}</Badge>
                      {a.camera_name && <span>{a.camera_name}</span>}
                      {a.plate && <Plate value={a.plate} />}
                    </div>
                  </div>
                  <div className="alert-time">{timeAgo(a.timestamp)}</div>
                </div>
              ))}
            </div>
          )}
        </Panel>

        {selected && (
          <Panel title="Alert Detail"
                 actions={<button className="btn sm" onClick={() => setSelected(null)}>Close</button>}>
            <div className="col" style={{ gap: 12 }}>
              <div>
                <div className="row wrap" style={{ marginBottom: 6 }}>
                  <SeverityBadge severity={selected.severity} />
                  <Badge>{selected.alert_type.replace(/_/g, ' ')}</Badge>
                  <Confidence value={selected.confidence} />
                </div>
                <div style={{ fontSize: 13, fontWeight: 600 }}>{selected.title}</div>
                <div className="muted small" style={{ marginTop: 4 }}>{selected.message}</div>
              </div>

              {selected.evidence?.certain === false && (
                <Caveat>
                  This is a <b>probable</b> match, not a confirmed one. The plate
                  was read as <b>{String(selected.evidence.plate_read)}</b> and
                  matched to <b>{String(selected.evidence.plate_target)}</b> after
                  allowing for OCR character confusion. Verify the plate before acting.
                </Caveat>
              )}

              <div>
                <div className="stat-label">Evidence</div>
                <table className="data" style={{ marginTop: 4 }}>
                  <tbody>
                    {Object.entries(selected.evidence || {}).map(([k, v]) => (
                      <tr key={k}>
                        <td className="muted" style={{ width: '45%' }}>{k.replace(/_/g, ' ')}</td>
                        <td className="mono">{Array.isArray(v) ? v.join(', ') : String(v)}</td>
                      </tr>
                    ))}
                    <tr><td className="muted">time</td><td className="mono">{dateTime(selected.timestamp)}</td></tr>
                    {selected.camera_ref && (
                      <tr><td className="muted">camera</td><td className="mono">{selected.camera_ref}</td></tr>
                    )}
                  </tbody>
                </table>
              </div>

              <div className="row wrap">
                {selected.vehicle_track_id && (
                  <button className="btn primary"
                          onClick={() => navigate(`/tracking?vehicle=${selected.vehicle_track_id}`)}>
                    Track this vehicle
                  </button>
                )}
                <button className="btn" onClick={() => acknowledge(selected, 'ACKNOWLEDGED')}>
                  Acknowledge
                </button>
                <button className="btn danger" onClick={() => acknowledge(selected, 'FALSE_POSITIVE')}>
                  False positive
                </button>
              </div>
            </div>
          </Panel>
        )}
      </div>
    </div>
  )
}
