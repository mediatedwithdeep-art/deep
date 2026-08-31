import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { endpoints, type Alert, type DashboardStats, type Vehicle } from '../lib/api'
import { liveFeed } from '../lib/ws'
import { Panel, Stat, SeverityBadge, Empty, Loading, Plate, ColourDot } from '../components/ui'
import MapView, { MapLegend } from '../components/MapView'
import { timeAgo, clockTime, distance } from '../lib/format'

export default function CommandCentre() {
  const [stats, setStats] = useState<DashboardStats | null>(null)
  const [alerts, setAlerts] = useState<Alert[]>([])
  const [tracks, setTracks] = useState<Vehicle[]>([])
  const [cameras, setCameras] = useState<GeoJSON.FeatureCollection | null>(null)
  const [sightings, setSightings] = useState<GeoJSON.FeatureCollection | null>(null)
  const [fresh, setFresh] = useState<Set<string>>(new Set())
  const [loading, setLoading] = useState(true)
  const navigate = useNavigate()

  async function refresh() {
    try {
      const [dash, cams, live] = await Promise.all([
        endpoints.dashboard(),
        endpoints.camerasGeoJSON(),
        endpoints.liveSightings(5),
      ])
      setStats(dash.stats)
      // MERGE, never replace. A live alert arrives over the WebSocket a
      // moment before it is visible to this poll, and replacing the list
      // would make it vanish for up to ten seconds -- exactly the alert an
      // operator is reacting to. De-duplicate by alert_id and keep the
      // newest first.
      setAlerts((prev) => {
        const byId = new Map(dash.recent_alerts.map((a) => [a.alert_id, a]))
        for (const a of prev) if (!byId.has(a.alert_id)) byId.set(a.alert_id, a)
        return [...byId.values()]
          .sort((a, b) => b.timestamp.localeCompare(a.timestamp))
          .slice(0, 12)
      })
      setTracks(dash.active_tracks)
      setCameras(cams)
      setSightings(live)
    } catch { /* transient: the poll below retries */ } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    refresh()
    // Poll as a safety net for counters. The alert feed itself arrives over
    // the WebSocket -- polling for alerts would put an operator up to ten
    // seconds behind an incident.
    const timer = window.setInterval(refresh, 10_000)
    const off = liveFeed.on((channel, data) => {
      if (channel !== 'alerts') return
      const alert = data as Alert
      setAlerts((prev) => [alert, ...prev.filter((a) => a.alert_id !== alert.alert_id)].slice(0, 12))
      setFresh((prev) => new Set(prev).add(alert.alert_id))
      window.setTimeout(() => setFresh((prev) => {
        const next = new Set(prev); next.delete(alert.alert_id); return next
      }), 2000)
      setStats((s) => s && { ...s, active_alerts: s.active_alerts + 1 })
    })
    return () => { window.clearInterval(timer); off() }
  }, [])

  if (loading) return <Loading rows={6} />

  const offline = stats?.cameras_offline ?? 0
  const online = stats?.cameras_online ?? 0
  const total = stats?.cameras_total ?? 0

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="grid cols-5">
        <Stat label="Cameras Online" tone={offline > 0 ? 'warn' : 'ok'}
              value={online} unit={`/ ${total}`}
              sub={total ? `${Math.round((online / total) * 100)}% of estate reporting` : '—'} />
        <Stat label="Active Alerts" tone={(stats?.critical_alerts ?? 0) > 0 ? 'crit' : 'info'}
              value={stats?.active_alerts ?? 0}
              sub={`${stats?.critical_alerts ?? 0} critical · last 24 h`} />
        <Stat label="Vehicles Tracked" tone="info"
              value={stats?.vehicles_tracked_1h ?? 0}
              sub={`${stats?.cross_camera_tracks_24h ?? 0} seen at 2+ cameras`} />
        <Stat label="ANPR Events" tone="info"
              value={stats?.anpr_events_1h ?? 0}
              sub={`${stats?.anpr_events_24h ?? 0} in 24 h`} />
        <Stat label="Cameras Offline" tone={offline > 0 ? 'crit' : 'ok'}
              value={offline}
              sub={offline > 0 ? 'requires attention' : 'estate fully reporting'} />
      </div>

      <div className="grid" style={{ gridTemplateColumns: 'minmax(0,1.55fr) minmax(0,1fr)' }}>
        <Panel title="Live Estate Map" flush style={{ height: 460 }}>
          <div style={{ height: 418, position: 'relative' }}>
            <MapView cameras={cameras} sightings={sightings}
                     onCameraClick={(id) => navigate(`/cameras?camera=${id}`)} />
            <MapLegend items={[
              ['#2ea86b', 'Camera online'],
              ['#d98a1a', 'Degraded'],
              ['#e5484d', 'Offline'],
              ['#a3b1c2', 'Sighting (last 5 min)'],
            ]} />
          </div>
        </Panel>

        <Panel title="Alert Feed" flush style={{ height: 460 }}
               actions={<a className="small" href="/alerts">View all →</a>}>
          <div style={{ height: 418, overflowY: 'auto' }}>
            {alerts.length === 0 && <Empty>No alerts in the last 24 hours.</Empty>}
            {alerts.map((a) => (
              <div key={a.alert_id}
                   className={`alert-row ${a.severity}${fresh.has(a.alert_id) ? ' fresh' : ''}`}
                   onClick={() => a.vehicle_track_id && navigate(`/tracking?vehicle=${a.vehicle_track_id}`)}>
                <div className="alert-rail" />
                <div style={{ minWidth: 0 }}>
                  <div className="alert-title">{a.title}</div>
                  <div className="alert-meta">
                    <SeverityBadge severity={a.severity} />
                    {a.camera_name && <span>{a.camera_name}</span>}
                    {a.plate && <Plate value={a.plate} />}
                  </div>
                </div>
                <div className="alert-time">{clockTime(a.timestamp)}</div>
              </div>
            ))}
          </div>
        </Panel>
      </div>

      <Panel title="Vehicles Tracked Across Multiple Cameras" flush>
        {tracks.length === 0 ? (
          <Empty>No cross-camera tracks yet. They appear once a vehicle is
                 confirmed at two or more cameras.</Empty>
        ) : (
          <table className="data">
            <thead>
              <tr>
                <th>Vehicle</th><th>Plate</th><th>Type</th><th>Colour</th>
                <th className="num">Cameras</th><th className="num">Sightings</th>
                <th className="num">Distance</th><th>Last seen</th>
              </tr>
            </thead>
            <tbody>
              {tracks.map((v) => (
                <tr key={v.vehicle_track_id} className="clickable"
                    onClick={() => navigate(`/tracking?vehicle=${v.vehicle_track_id}`)}>
                  <td className="mono">{v.vehicle_track_id}</td>
                  <td><Plate value={v.best_plate} /></td>
                  <td>{v.vehicle_type}</td>
                  <td><ColourDot colour={v.vehicle_color} /></td>
                  <td className="num">{v.camera_count}</td>
                  <td className="num">{v.sighting_count}</td>
                  <td className="num">{distance(v.total_distance_m)}</td>
                  <td className="muted">{timeAgo(v.last_seen)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
    </div>
  )
}
