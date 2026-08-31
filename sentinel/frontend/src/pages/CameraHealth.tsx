import { useEffect, useState } from 'react'
import { endpoints } from '../lib/api'
import { Panel, Stat, Badge, Loading, Empty, Caveat } from '../components/ui'
import { timeAgo, statusClass } from '../lib/format'

export default function CameraHealth() {
  const [data, setData] = useState<{ summary: any; cameras: any[] } | null>(null)
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState('')

  async function load() {
    try { setData(await endpoints.cameraHealth()) } finally { setLoading(false) }
  }
  useEffect(() => {
    load()
    const t = globalThis.setInterval(load, 15_000)
    return () => globalThis.clearInterval(t)
  }, [])

  if (loading) return <Loading rows={6} />
  if (!data) return <Empty>Could not load camera health.</Empty>

  const s = data.summary
  const rows = data.cameras.filter((c) =>
    !filter || c.camera_id.toLowerCase().includes(filter.toLowerCase())
            || c.name.toLowerCase().includes(filter.toLowerCase()))
  const unhealthy = data.cameras.filter((c) => c.status !== 'ONLINE' || c.trust_score < 0.5)

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="grid cols-5">
        <Stat label="Total" value={s.total} sub="excluding disabled" />
        <Stat label="Online" value={s.online} tone="ok"
              sub={`${Math.round((s.online / Math.max(s.total, 1)) * 100)}% of estate`} />
        <Stat label="Offline" value={s.offline} tone={s.offline ? 'crit' : 'ok'} />
        <Stat label="ANPR-capable" value={s.anpr_capable} tone="info"
              sub={`${Math.round((s.anpr_capable / Math.max(s.total, 1)) * 100)}% can read a plate`} />
        <Stat label="Firmware at risk" value={s.firmware_at_risk}
              tone={s.firmware_at_risk ? 'warn' : 'ok'} sub="EOL or known CVE" />
      </div>

      {/* Estate health is a first-class operational view, not a diagnostic
          afterthought. A VMS that hides how much of its estate is broken is
          telling its operators something untrue. */}
      <Caveat>
        Only <b>{s.anpr_capable} of {s.total}</b> cameras can physically resolve
        a number plate. The rest contribute through vehicle appearance, colour
        and type — they are not faulty, they are wide-angle. {s.firmware_at_risk} run
        firmware that is end-of-life or has a known CVE and should be isolated
        behind an edge gateway.
      </Caveat>

      <div className="toolbar">
        <div className="field" style={{ width: 260 }}>
          <label>Filter</label>
          <input type="search" placeholder="Camera name or ID"
                 value={filter} onChange={(e) => setFilter(e.target.value)} />
        </div>
        <div className="spacer" />
        <Badge tone={unhealthy.length ? 'warn' : 'ok'}>
          {unhealthy.length} need attention
        </Badge>
      </div>

      <Panel title={`Cameras (${rows.length}) — lowest trust first`} flush>
        <div style={{ maxHeight: 560, overflowY: 'auto' }}>
          <table className="data">
            <thead>
              <tr>
                <th>Camera</th><th>Zone</th><th>Status</th>
                <th className="num">Trust</th><th className="num">FPS</th>
                <th className="num">Scene change</th><th className="num">Decode errors</th>
                <th className="num">Inference</th><th>Firmware</th><th>Last seen</th><th>Note</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((c) => (
                <tr key={c.camera_id}>
                  <td>
                    <div className="mono small">{c.camera_id}</div>
                    <div className="muted small">{c.name}</div>
                  </td>
                  <td className="muted small">{c.zone || '—'}</td>
                  <td><Badge tone={statusClass(c.status)}>{c.status}</Badge></td>
                  <td className="num">
                    <span style={{ color: c.trust_score >= 0.7 ? 'var(--ok)'
                      : c.trust_score >= 0.4 ? 'var(--warn)' : 'var(--crit)' }}>
                      {c.trust_score?.toFixed(2)}
                    </span>
                  </td>
                  <td className="num">{c.fps_actual?.toFixed(1) ?? '—'}</td>
                  {/* Near-zero scene change on a reachable stream means a
                      frozen picture: the socket is healthy and the image is
                      not. Nothing else detects this. */}
                  <td className="num">
                    {c.scene_change == null ? '—' : (
                      <span style={{ color: c.scene_change < 0.002 ? 'var(--crit)' : undefined }}>
                        {c.scene_change.toFixed(4)}
                      </span>
                    )}
                  </td>
                  <td className="num">{c.decode_errors ?? '—'}</td>
                  <td className="num">{c.inference_ms == null ? '—' : `${c.inference_ms.toFixed(1)} ms`}</td>
                  <td>
                    {c.firmware_risk && c.firmware_risk !== 'OK'
                      ? <Badge tone={c.firmware_risk === 'KNOWN_CVE' ? 'crit' : 'warn'}>
                          {c.firmware_risk}
                        </Badge>
                      : <span className="muted">OK</span>}
                  </td>
                  <td className="muted small nowrap">{timeAgo(c.last_seen)}</td>
                  <td className="muted small">{c.message || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>
  )
}
