import { useEffect, useState } from 'react'
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Legend,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { endpoints } from '../lib/api'
import { Panel, Stat, Badge, Loading, Empty, Caveat } from '../components/ui'
import { COLOUR_SWATCH, VEHICLE_LABEL, clockTime } from '../lib/format'

// One categorical ramp used everywhere, so a colour means the same thing on
// every chart in the product.
const SERIES = ['#4cc9f0', '#2ea86b', '#d98a1a', '#e8663d', '#8f7ae5']
const AXIS = { stroke: '#6b7a8d', fontSize: 10 }
const TOOLTIP = {
  contentStyle: {
    background: '#141b25', border: '1px solid #33404f',
    borderRadius: 6, fontSize: 11, color: '#e8edf4',
  },
}

export default function Analytics() {
  const [timeline, setTimeline] = useState<any>(null)
  const [mix, setMix] = useState<any>(null)
  const [anpr, setAnpr] = useState<any>(null)
  const [status, setStatus] = useState<any>(null)
  const [camActivity, setCamActivity] = useState<any[]>([])
  const [hours, setHours] = useState(6)
  const [loading, setLoading] = useState(true)

  async function load() {
    try {
      const [t, m, a, s, c] = await Promise.all([
        endpoints.analyticsTimeline(hours),
        endpoints.analyticsMix(),
        endpoints.analyticsAnpr(),
        endpoints.systemStatus(),
        endpoints.analyticsCameras(),
      ])
      setTimeline(t); setMix(m); setAnpr(a); setStatus(s); setCamActivity(c.items)
    } finally { setLoading(false) }
  }

  useEffect(() => {
    setLoading(true); load()
    const t = globalThis.setInterval(load, 20_000)
    return () => globalThis.clearInterval(t)
  }, [hours])

  if (loading) return <Loading rows={6} />

  const buckets = (timeline?.buckets || []).map((b: any) => ({
    ...b, label: clockTime(b.bucket),
  }))
  const byClass = anpr?.by_camera_class || []
  const capable = byClass.find((r: any) => r.anpr_capable)
  const notCapable = byClass.find((r: any) => !r.anpr_capable)

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="grid cols-5">
        <Stat label="Sightings / min" tone="info"
              value={status?.throughput?.sightings_per_min ?? 0} />
        <Stat label="Plate reads / min" tone="info"
              value={status?.throughput?.plate_reads_per_min ?? 0} />
        <Stat label="Inference latency" tone="ok"
              value={status?.throughput?.mean_inference_ms ?? '—'} unit="ms"
              sub="mean, last 5 min" />
        <Stat label="Cameras reporting" tone="ok"
              value={`${status?.ingestion?.reporting ?? 0}`}
              unit={`/ ${status?.ingestion?.total ?? 0}`} />
        <Stat label="DB partitions" value={status?.partitions ?? 0}
              sub="time-range partitioned tables" />
      </div>

      <div className="toolbar">
        <div className="field" style={{ width: 170 }}>
          <label>Window</label>
          <select value={hours} onChange={(e) => setHours(Number(e.target.value))}>
            <option value={1}>Last hour</option>
            <option value={6}>Last 6 hours</option>
            <option value={24}>Last 24 hours</option>
            <option value={72}>Last 3 days</option>
          </select>
        </div>
        <div className="spacer" />
        <Badge tone={status?.database?.healthy ? 'ok' : 'crit'}>
          database {status?.database?.healthy ? 'healthy' : 'unreachable'}
        </Badge>
        <Badge tone="neutral">{status?.websocket?.clients ?? 0} live clients</Badge>
      </div>

      <Panel title="Throughput">
        <div style={{ height: 210 }}>
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={buckets} margin={{ top: 6, right: 10, left: -18, bottom: 0 }}>
              <defs>
                {SERIES.slice(0, 3).map((c, i) => (
                  <linearGradient key={i} id={`g${i}`} x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={c} stopOpacity={0.45} />
                    <stop offset="100%" stopColor={c} stopOpacity={0.02} />
                  </linearGradient>
                ))}
              </defs>
              <CartesianGrid stroke="#232d3b" vertical={false} />
              <XAxis dataKey="label" tick={AXIS} tickLine={false} axisLine={false}
                     minTickGap={40} />
              <YAxis tick={AXIS} tickLine={false} axisLine={false} width={44} />
              <Tooltip {...TOOLTIP} />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              <Area type="monotone" dataKey="sightings" name="Sightings"
                    stroke={SERIES[0]} fill="url(#g0)" strokeWidth={1.6} />
              <Area type="monotone" dataKey="plate_reads" name="Plate reads"
                    stroke={SERIES[1]} fill="url(#g1)" strokeWidth={1.6} />
              <Area type="monotone" dataKey="alerts" name="Alerts"
                    stroke={SERIES[2]} fill="url(#g2)" strokeWidth={1.6} />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </Panel>

      <div className="grid cols-2">
        <Panel title="ANPR performance by camera class">
          {/* Reported per class deliberately. Blending a dedicated ANPR lane
              with a wide-angle junction camera produces an average that
              describes neither. */}
          <Caveat>
            A wide-angle surveillance camera cannot resolve a plate at any
            settings. Reporting one estate-wide read rate would describe
            nothing, so the two classes are shown separately.
          </Caveat>
          <table className="data" style={{ marginTop: 10 }}>
            <thead><tr><th>Class</th><th className="num">Cameras</th>
              <th className="num">Sightings</th><th className="num">Plate reads</th>
              <th className="num">Read rate</th></tr></thead>
            <tbody>
              {capable && (
                <tr>
                  <td><Badge tone="accent">ANPR-capable</Badge></td>
                  <td className="num">{capable.cameras}</td>
                  <td className="num">{capable.sightings}</td>
                  <td className="num">{capable.plate_reads}</td>
                  <td className="num" style={{ color: 'var(--ok)' }}>{capable.read_rate_pct}%</td>
                </tr>
              )}
              {notCapable && (
                <tr>
                  <td><Badge>Wide-angle surveillance</Badge></td>
                  <td className="num">{notCapable.cameras}</td>
                  <td className="num">{notCapable.sightings}</td>
                  <td className="num">{notCapable.plate_reads}</td>
                  <td className="num muted">{notCapable.read_rate_pct ?? 0}%</td>
                </tr>
              )}
            </tbody>
          </table>
          {anpr?.overall && (
            <div className="row wrap" style={{ marginTop: 12 }}>
              <Badge tone="neutral">{anpr.overall.reads} reads</Badge>
              <Badge tone="ok">{anpr.overall.valid_format} valid format</Badge>
              <Badge tone="warn">{anpr.overall.lexicon_corrected} lexicon-corrected</Badge>
              <Badge tone="neutral">{anpr.overall.distinct_plates} distinct plates</Badge>
            </div>
          )}
        </Panel>

        <Panel title="Vehicle mix">
          <div style={{ height: 220 }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={(mix?.by_type || []).map((r: any) => ({
                          ...r, label: VEHICLE_LABEL[r.vehicle_type] || r.vehicle_type }))}
                        margin={{ top: 6, right: 10, left: -18, bottom: 0 }}>
                <CartesianGrid stroke="#232d3b" vertical={false} />
                <XAxis dataKey="label" tick={AXIS} tickLine={false} axisLine={false} />
                <YAxis tick={AXIS} tickLine={false} axisLine={false} width={44} />
                <Tooltip {...TOOLTIP} cursor={{ fill: '#1c2530' }} />
                <Bar dataKey="n" name="Sightings" fill={SERIES[0]} radius={[3, 3, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
          <div className="row wrap" style={{ marginTop: 10 }}>
            {(mix?.by_color || []).slice(0, 8).map((c: any) => (
              <span key={c.color} className="row small" style={{ gap: 5 }}>
                <span style={{ width: 9, height: 9, borderRadius: 2,
                               background: COLOUR_SWATCH[c.color] || '#6b7a8d',
                               border: '1px solid rgba(255,255,255,.18)' }} />
                <span className="muted">{c.color}</span>
                <span className="mono">{c.n}</span>
              </span>
            ))}
          </div>
        </Panel>
      </div>

      <Panel title="Busiest cameras (last hour)" flush>
        {camActivity.length === 0 ? <Empty>No activity recorded.</Empty> : (
          <div style={{ maxHeight: 340, overflowY: 'auto' }}>
            <table className="data">
              <thead><tr><th>Camera</th><th>Zone</th><th className="num">Sightings</th>
                <th className="num">Unique vehicles</th><th className="num">Plate reads</th>
                <th className="num">Mean quality</th></tr></thead>
              <tbody>
                {camActivity.slice(0, 30).map((c) => (
                  <tr key={c.camera_id}>
                    <td><div className="mono small">{c.camera_id}</div>
                        <div className="muted small">{c.name}</div></td>
                    <td className="muted small">{c.zone || '—'}</td>
                    <td className="num">{c.sightings}</td>
                    <td className="num">{c.unique_vehicles}</td>
                    <td className="num">{c.plate_reads}</td>
                    <td className="num">{c.avg_quality?.toFixed(2) ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  )
}
