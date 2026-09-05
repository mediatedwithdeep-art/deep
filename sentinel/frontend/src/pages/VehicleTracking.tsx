import { useEffect, useState, type FormEvent } from 'react'
import { useSearchParams } from 'react-router-dom'
import { endpoints, type TimelineHop, type Vehicle } from '../lib/api'
import { Panel, Badge, Empty, Plate, ColourDot, Caveat, Confidence, Stat } from '../components/ui'
import MapView, { MapLegend } from '../components/MapView'
import { clockTime, dateTime, duration, distance, VEHICLE_LABEL } from '../lib/format'

/** One hop in the movement history.
 *
 *  The decision band is the most important thing on this row. An AUTO link
 *  was confirmed by a plate read; a PROBABLE link is an appearance match
 *  the system is offering for confirmation. Showing them identically would
 *  turn a lead into an apparent fact.
 */
function Hop({ hop, index }: { hop: TimelineHop; index: number }) {
  const [open, setOpen] = useState(false)
  const a = hop.association
  const scores = a.scores || {}

  return (
    <div className={`hop ${a.decision}`}>
      <div className="hop-head">
        <span className="mono muted small">{String(index + 1).padStart(2, '0')}</span>
        <span className="hop-camera">{hop.camera_name || hop.camera_ref}</span>
        <Badge tone={a.decision === 'AUTO' ? 'ok' : a.decision === 'SEED' ? 'accent' : 'warn'}>
          {a.decision === 'SEED' ? 'first sighting'
            : a.decision === 'AUTO' ? 'confirmed' : 'probable'}
        </Badge>
        {hop.plate_normalized && <Plate value={hop.plate_normalized} />}
        <div className="spacer" />
        <span className="mono muted small">{clockTime(hop.timestamp)}</span>
      </div>

      <div className="hop-detail">
        <span>{hop.camera_ref}</span>
        {hop.zone && <span>{hop.zone}</span>}
        {hop.speed_kmph != null && <span>{Math.round(hop.speed_kmph)} km/h</span>}
        {hop.gap_seconds != null && <span>+{duration(hop.gap_seconds)} since previous</span>}
        <span>{hop.detection_count} detections</span>
      </div>

      {a.decision !== 'SEED' && (
        <>
          <div className={`score-bar${a.decision === 'PROBABLE' ? ' probable' : ''}`}>
            <div style={{ width: `${Math.min(100, a.confidence * 100)}%` }} />
          </div>
          <button className="btn sm" style={{ marginTop: 6 }} onClick={() => setOpen(!open)}>
            {open ? 'Hide' : 'Why this match?'}
          </button>
        </>
      )}

      {open && (
        <div style={{ marginTop: 8, padding: 10, background: 'var(--bg-2)',
                      borderRadius: 'var(--radius)', fontSize: 11.5 }}>
          <div className="row wrap" style={{ marginBottom: 8 }}>
            <Confidence value={a.confidence} />
            {a.travel_expected_s != null && a.travel_actual_s != null && (
              <span className="muted">
                travelled in {Math.round(a.travel_actual_s)}s,
                road network expects ~{Math.round(a.travel_expected_s)}s
              </span>
            )}
          </div>
          <table className="data">
            <tbody>
              {Object.entries(scores).map(([k, v]) => (
                <tr key={k}>
                  <td className="muted" style={{ width: '38%' }}>{k}</td>
                  <td>
                    <div className="score-bar" style={{ maxWidth: 130, marginTop: 0 }}>
                      <div style={{ width: `${Math.min(100, v * 100)}%` }} />
                    </div>
                  </td>
                  <td className="num mono" style={{ width: 52 }}>{v.toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {a.reasons?.length > 0 && (
            <div className="muted small" style={{ marginTop: 8, lineHeight: 1.7 }}>
              {a.reasons.map((r, i) => <div key={i} className="mono">· {r}</div>)}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default function VehicleTracking() {
  const [params, setParams] = useSearchParams()
  const vehicleId = params.get('vehicle') || ''
  const [input, setInput] = useState(vehicleId)
  const [reason, setReason] = useState('')
  const [vehicle, setVehicle] = useState<Vehicle | null>(null)
  const [timeline, setTimeline] = useState<{
    hops: TimelineHop[]; hop_count: number; camera_count: number
    confirmed_hops: number; probable_hops: number
  } | null>(null)
  const [track, setTrack] = useState<GeoJSON.FeatureCollection | null>(null)
  const [cameras, setCameras] = useState<GeoJSON.FeatureCollection | null>(null)
  const [nextCams, setNextCams] = useState<any>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => { endpoints.camerasGeoJSON().then(setCameras).catch(() => {}) }, [])

  async function load(id: string, why: string) {
    if (!id || why.trim().length < 4) {
      setError('A purpose is required before viewing movement history (DPDP Act 2023).')
      return
    }
    setBusy(true); setError(''); setTimeline(null); setTrack(null); setNextCams(null)
    try {
      const [v, t, g] = await Promise.all([
        endpoints.vehicle(id),
        endpoints.timeline(id, why),
        endpoints.trackGeoJSON(id).catch(() => null),
      ])
      setVehicle(v); setTimeline(t); setTrack(g)
      endpoints.nextCameras(id).then(setNextCams).catch(() => {})
      setParams({ vehicle: id })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'could not load movement history')
    } finally { setBusy(false) }
  }

  function submit(e: FormEvent) { e.preventDefault(); load(input.trim(), reason) }

  const highlight = nextCams?.candidates?.map((c: any) => c.camera_ref) || []

  return (
    <div className="col" style={{ gap: 14 }}>
      <Panel title="Vehicle Movement History">
        <form onSubmit={submit} className="col" style={{ gap: 11 }}>
          <div className="toolbar">
            <div className="field" style={{ width: 200 }}>
              <label>Vehicle track ID</label>
              <input type="text" placeholder="V-000123" value={input}
                     onChange={(e) => setInput(e.target.value)} />
            </div>
            <div className="field" style={{ flex: 1, minWidth: 260 }}>
              <label>Purpose of access (recorded in the audit log)</label>
              <input type="text" placeholder="e.g. FIR 0142/2026 vehicle movement enquiry"
                     value={reason} onChange={(e) => setReason(e.target.value)} />
            </div>
            <button className="btn primary" type="submit" disabled={busy}>
              {busy ? 'Loading…' : 'Show history'}
            </button>
          </div>
          {error && <div className="error-box">{error}</div>}
        </form>
      </Panel>

      {timeline && vehicle && (
        <>
          <div className="grid cols-5">
            <Stat label="Cameras" value={timeline.camera_count} tone="info"
                  sub={`${timeline.hop_count} sightings`} />
            <Stat label="Confirmed hops" value={timeline.confirmed_hops} tone="ok"
                  sub="plate-verified or first sighting" />
            <Stat label="Probable hops" value={timeline.probable_hops}
                  tone={timeline.probable_hops > 0 ? 'warn' : 'ok'}
                  sub="appearance match — verify" />
            <Stat label="Distance" value={distance(vehicle.total_distance_m)} tone="info"
                  sub="along observed path" />
            <Stat label="Plate reads" value={vehicle.plate_read_count} tone="info"
                  sub={vehicle.best_plate || 'no plate read'} />
          </div>

          {timeline.probable_hops > 0 && (
            <Caveat>
              {timeline.probable_hops} of {timeline.hop_count} hops were matched
              on <b>appearance</b>, not a plate read. Appearance matching cannot
              distinguish two similar vehicles with certainty — expand each hop
              to see the reasoning, and confirm before treating this path as
              established fact.
            </Caveat>
          )}

          <div className="grid" style={{ gridTemplateColumns: 'minmax(0,1fr) minmax(0,1fr)' }}>
            <Panel title="Route" flush style={{ height: 520 }}>
              <div style={{ height: 478, position: 'relative' }}>
                <MapView cameras={cameras} track={track} fitTo="track"
                         highlightCameras={highlight} showFov={false} />
                <MapLegend items={[
                  ['#4cc9f0', 'Observed path'],
                  ['#2ea86b', 'Camera online'],
                  ['#e5484d', 'Camera offline'],
                ]} />
              </div>
            </Panel>

            <Panel title={`Timeline — ${vehicle.vehicle_track_id}`} style={{ height: 520 }}
                   actions={<>
                     <Plate value={vehicle.best_plate} />
                     <Badge>{VEHICLE_LABEL[vehicle.vehicle_type] || vehicle.vehicle_type}</Badge>
                   </>}>
              <div style={{ height: 452, overflowY: 'auto', paddingRight: 4 }}>
                <div className="muted small" style={{ marginBottom: 12 }}>
                  <ColourDot colour={vehicle.vehicle_color} />
                  <span style={{ marginLeft: 8 }}>
                    first seen {dateTime(vehicle.first_seen)} · last seen {dateTime(vehicle.last_seen)}
                  </span>
                </div>
                <div className="timeline">
                  {timeline.hops.map((h, i) => <Hop key={h.sighting_id} hop={h} index={i} />)}
                </div>
              </div>
            </Panel>
          </div>

          {nextCams?.candidates?.length > 0 && (
            <Panel title="Where to look next" flush
                   actions={<span className="small muted">{nextCams.note}</span>}>
              <table className="data">
                <thead>
                  <tr><th>Camera</th><th>Location</th><th className="num">Road distance</th>
                    <th className="num">Travel time</th><th>Arrival window</th><th>Basis</th></tr>
                </thead>
                <tbody>
                  {nextCams.candidates.slice(0, 8).map((c: any) => (
                    <tr key={c.camera_ref}>
                      <td className="mono">{c.camera_ref}</td>
                      <td>{c.camera_name}</td>
                      <td className="num">{distance(c.road_dist_m)}</td>
                      <td className="num">{duration(c.travel_s)}</td>
                      <td className="mono small">
                        {clockTime(c.window_start)} – {clockTime(c.window_end)}
                      </td>
                      <td>
                        <Badge tone={c.source === 'observed' ? 'ok' : 'neutral'}>
                          {c.source === 'observed' ? 'learned' : 'road network'}
                        </Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Panel>
          )}
        </>
      )}

      {!timeline && !busy && (
        <Panel><Empty>
          Enter a vehicle track ID and a purpose to view its movement history.
          Vehicle IDs come from the Command Centre, Vehicle Search or an alert.
        </Empty></Panel>
      )}
    </div>
  )
}
