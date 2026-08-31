import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { endpoints, type Alert } from '../lib/api'
import { liveFeed } from '../lib/ws'
import MapView, { MapLegend } from '../components/MapView'
import { Badge, SeverityBadge, Plate, Empty } from '../components/ui'
import { clockTime } from '../lib/format'

export default function GISMap() {
  const [cameras, setCameras] = useState<GeoJSON.FeatureCollection | null>(null)
  const [sightings, setSightings] = useState<GeoJSON.FeatureCollection | null>(null)
  const [alerts, setAlerts] = useState<Alert[]>([])
  const [showFov, setShowFov] = useState(true)
  const [window_, setWindow] = useState(5)
  const navigate = useNavigate()

  useEffect(() => { endpoints.camerasGeoJSON().then(setCameras).catch(() => {}) }, [])

  useEffect(() => {
    const load = () => endpoints.liveSightings(window_).then(setSightings).catch(() => {})
    load()
    const t = globalThis.setInterval(load, 8000)
    return () => globalThis.clearInterval(t)
  }, [window_])

  useEffect(() => liveFeed.on((channel, data) => {
    if (channel === 'alerts') setAlerts((prev) => [data as Alert, ...prev].slice(0, 20))
  }), [])

  const alertCameras = alerts.map((a) => a.camera_ref).filter(Boolean) as string[]

  return (
    <div className="col" style={{ gap: 12, height: 'calc(100vh - 100px)' }}>
      <div className="toolbar">
        <button className={`btn${showFov ? ' primary' : ''}`} onClick={() => setShowFov((v) => !v)}>
          Field of view
        </button>
        <div className="field" style={{ width: 170 }}>
          <label>Sighting window</label>
          <select value={window_} onChange={(e) => setWindow(Number(e.target.value))}>
            <option value={1}>Last minute</option>
            <option value={5}>Last 5 minutes</option>
            <option value={15}>Last 15 minutes</option>
            <option value={60}>Last hour</option>
          </select>
        </div>
        <div className="spacer" />
        <Badge tone="neutral">{sightings?.features.length ?? 0} sightings plotted</Badge>
        <Badge tone="neutral">{cameras?.features.filter(
          (f) => f.properties?.kind === 'camera').length ?? 0} cameras</Badge>
      </div>

      <div style={{ flex: 1, position: 'relative', minHeight: 400 }}>
        <MapView cameras={cameras} sightings={sightings} showFov={showFov}
                 highlightCameras={alertCameras}
                 onCameraClick={(id) => navigate(`/cameras?camera=${id}`)} />
        <MapLegend items={[
          ['#2ea86b', 'Camera online'],
          ['#d98a1a', 'Degraded'],
          ['#e5484d', 'Offline'],
          ['#a3b1c2', 'Vehicle sighting'],
          ['#4cc9f0', 'Recent alert'],
        ]} />
        <div className="map-overlay-panel">
          <div className="panel-head"><h2>Live Alerts</h2></div>
          {alerts.length === 0 ? (
            <Empty>Waiting for alerts…</Empty>
          ) : alerts.map((a) => (
            <div key={a.alert_id} className={`alert-row ${a.severity}`}
                 onClick={() => a.vehicle_track_id && navigate(`/tracking?vehicle=${a.vehicle_track_id}`)}>
              <div className="alert-rail" />
              <div style={{ minWidth: 0 }}>
                <div className="alert-title" style={{ fontSize: 11.5 }}>{a.title}</div>
                <div className="alert-meta">
                  <SeverityBadge severity={a.severity} />
                  {a.plate && <Plate value={a.plate} />}
                </div>
              </div>
              <div className="alert-time">{clockTime(a.timestamp)}</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
