import { useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { endpoints, type Vehicle } from '../lib/api'
import { Panel, Empty, Loading, Plate, ColourDot, Badge, Caveat } from '../components/ui'
import { timeAgo, duration, distance, VEHICLE_LABEL } from '../lib/format'

const TYPES = ['', 'car', 'motorcycle', 'auto_rickshaw', 'truck', 'bus', 'bicycle', 'tractor']
const COLOURS = ['', 'white', 'silver', 'grey', 'black', 'red', 'blue', 'green',
                 'yellow', 'brown', 'orange']

export default function VehicleSearch() {
  const [plate, setPlate] = useState('')
  const [type, setType] = useState('')
  const [colour, setColour] = useState('')
  const [hours, setHours] = useState(24)
  const [minCameras, setMinCameras] = useState(1)
  const [results, setResults] = useState<Vehicle[]>([])
  const [meta, setMeta] = useState<{ note: string; canonical: string } | null>(null)
  const [searched, setSearched] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const navigate = useNavigate()

  async function search(e?: FormEvent) {
    e?.preventDefault()
    setBusy(true); setError(''); setMeta(null)
    const params = new URLSearchParams({ hours: String(hours), limit: '200' })
    if (plate.trim()) params.set('plate', plate.trim())
    if (type) params.set('vehicle_type', type)
    if (colour) params.set('color', colour)
    if (minCameras > 1) params.set('min_cameras', String(minCameras))
    try {
      const r = await endpoints.searchVehicles(`?${params}`)
      setResults(r.items)
      if (r.search) setMeta({ note: r.search.note, canonical: r.search.canonical })
      setSearched(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'search failed')
    } finally { setBusy(false) }
  }

  return (
    <div className="col" style={{ gap: 14 }}>
      <Panel title="Search">
        <form onSubmit={search} className="col" style={{ gap: 12 }}>
          <div className="toolbar">
            <div className="field" style={{ width: 220 }}>
              <label>Number plate</label>
              <input type="search" placeholder="e.g. GJ01AB1234"
                     value={plate} onChange={(e) => setPlate(e.target.value.toUpperCase())} />
            </div>
            <div className="field" style={{ width: 160 }}>
              <label>Vehicle type</label>
              <select value={type} onChange={(e) => setType(e.target.value)}>
                {TYPES.map((t) => <option key={t} value={t}>{t ? VEHICLE_LABEL[t] : 'Any type'}</option>)}
              </select>
            </div>
            <div className="field" style={{ width: 140 }}>
              <label>Colour</label>
              <select value={colour} onChange={(e) => setColour(e.target.value)}>
                {COLOURS.map((c) => <option key={c} value={c}>{c || 'Any colour'}</option>)}
              </select>
            </div>
            <div className="field" style={{ width: 120 }}>
              <label>Time window</label>
              <select value={hours} onChange={(e) => setHours(Number(e.target.value))}>
                <option value={1}>Last hour</option>
                <option value={6}>Last 6 hours</option>
                <option value={24}>Last 24 hours</option>
                <option value={168}>Last 7 days</option>
                <option value={720}>Last 30 days</option>
              </select>
            </div>
            <div className="field" style={{ width: 150 }}>
              <label>Seen at</label>
              <select value={minCameras} onChange={(e) => setMinCameras(Number(e.target.value))}>
                <option value={1}>Any camera count</option>
                <option value={2}>2+ cameras</option>
                <option value={3}>3+ cameras</option>
                <option value={5}>5+ cameras</option>
              </select>
            </div>
            <button className="btn primary" type="submit" disabled={busy}>
              {busy ? 'Searching…' : 'Search'}
            </button>
          </div>

          {/* Fuzzy matching is a safety-relevant behaviour, not a feature
              detail: an operator who believes they got an exact read may
              act on a vehicle the system never actually identified. */}
          <Caveat>
            Plate search is <b>fuzzy by design</b>. OCR confuses O/0, I/1, 8/B,
            5/S, 2/Z and 6/G systematically, so results include reads that
            differ only by those characters. Always verify the plate on the
            source image before acting.
          </Caveat>
        </form>
      </Panel>

      {error && <div className="error-box">{error}</div>}

      {busy ? <Loading rows={5} /> : searched && (
        <Panel title={`Results (${results.length})`} flush
               actions={meta && <span className="small muted mono">
                 canonical form: {meta.canonical}</span>}>
          {results.length === 0 ? (
            <Empty>No vehicles matched. Widen the time window, or try a partial
                   plate — fuzzy matching handles missing characters poorly but
                   character confusions well.</Empty>
          ) : (
            <table className="data">
              <thead>
                <tr>
                  <th>Vehicle ID</th><th>Plate</th><th>Type</th><th>Colour</th>
                  <th className="num">Cameras</th><th className="num">Sightings</th>
                  <th className="num">Plate reads</th><th className="num">Distance</th>
                  <th className="num">Duration</th><th>Last seen</th><th></th>
                </tr>
              </thead>
              <tbody>
                {results.map((v) => (
                  <tr key={v.vehicle_track_id} className="clickable"
                      onClick={() => navigate(`/tracking?vehicle=${v.vehicle_track_id}`)}>
                    <td className="mono">{v.vehicle_track_id}</td>
                    <td><Plate value={v.best_plate} /></td>
                    <td>{VEHICLE_LABEL[v.vehicle_type] || v.vehicle_type}</td>
                    <td><ColourDot colour={v.vehicle_color} /></td>
                    <td className="num">
                      {v.camera_count > 1
                        ? <Badge tone="accent">{v.camera_count}</Badge>
                        : v.camera_count}
                    </td>
                    <td className="num">{v.sighting_count}</td>
                    <td className="num">{v.plate_read_count}</td>
                    <td className="num">{distance(v.total_distance_m)}</td>
                    <td className="num">{duration(v.duration_seconds)}</td>
                    <td className="muted nowrap">{timeAgo(v.last_seen)}</td>
                    <td><span className="small" style={{ color: 'var(--accent)' }}>Track →</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
      )}
    </div>
  )
}
