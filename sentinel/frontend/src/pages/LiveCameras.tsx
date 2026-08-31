import { useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { endpoints, type Camera } from '../lib/api'
import { liveFeed } from '../lib/ws'
import { Panel, Badge, Empty, Loading } from '../components/ui'
import { timeAgo } from '../lib/format'

/** A single camera tile.
 *
 *  Video is negotiated directly with the media server over WHEP (WebRTC),
 *  not proxied through the API. When no media server is reachable -- the
 *  usual case in demo mode -- the tile shows live METADATA rather than a
 *  fake picture: sighting counts and detections are real, and pretending
 *  to show video that does not exist would be the wrong kind of demo.
 */
function CameraTile({ camera, selected, onSelect, liveCount }: {
  camera: Camera; selected: boolean; onSelect: () => void; liveCount: number
}) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const [playing, setPlaying] = useState(false)
  const [tried, setTried] = useState(false)

  useEffect(() => {
    if (!selected || tried || camera.status !== 'ONLINE') return
    setTried(true)
    let pc: RTCPeerConnection | null = null
    ;(async () => {
      try {
        const { whep_url } = await endpoints.cameraStream(camera.camera_id)
        pc = new RTCPeerConnection()
        pc.addTransceiver('video', { direction: 'recvonly' })
        pc.ontrack = (e) => {
          if (videoRef.current) {
            videoRef.current.srcObject = e.streams[0]
            setPlaying(true)
          }
        }
        const offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        const res = await fetch(whep_url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/sdp' },
          body: offer.sdp,
        })
        if (!res.ok) throw new Error('no media server')
        await pc.setRemoteDescription({ type: 'answer', sdp: await res.text() })
      } catch {
        // Expected without a media server. The tile falls back to metadata.
        pc?.close()
      }
    })()
    return () => { pc?.close() }
  }, [selected, tried, camera.camera_id, camera.status])

  return (
    <div className={`cam-tile${selected ? ' selected' : ''}`} onClick={onSelect}>
      <div className="cam-view">
        <video ref={videoRef} autoPlay muted playsInline
               style={{ display: playing ? 'block' : 'none' }} />
        {!playing && (
          <div className="cam-placeholder">
            <div style={{ fontSize: 22, fontFamily: 'var(--mono)', color: 'var(--fg-1)' }}>
              {liveCount}
            </div>
            <div style={{ marginTop: 2 }}>detections · last 60 s</div>
            <div style={{ marginTop: 8, fontSize: 10, opacity: .7 }}>
              {camera.status === 'ONLINE'
                ? 'analytics live · video on demand'
                : camera.status}
            </div>
          </div>
        )}
        <div className="cam-overlay">
          <div className="cam-overlay-top">
            <span className="cam-tag">{camera.camera_id}</span>
            <span className={`dot ${camera.status === 'ONLINE' ? 'live' : 'crit'}`} />
          </div>
          <div className="cam-overlay-bot">
            <span className="cam-tag">{camera.width}×{camera.height}</span>
            {camera.anpr_capable && <span className="cam-tag" style={{ color: 'var(--accent)' }}>ANPR</span>}
          </div>
        </div>
      </div>
      <div className="cam-info">
        <div className="cam-name">{camera.name}</div>
        <div className="cam-meta">
          <span>{camera.zone || '—'}</span>
          <span>·</span>
          <span>{camera.protocol}</span>
          <span>·</span>
          <span>{timeAgo(camera.last_seen)}</span>
        </div>
      </div>
    </div>
  )
}

export default function LiveCameras() {
  const [cameras, setCameras] = useState<Camera[]>([])
  const [loading, setLoading] = useState(true)
  const [query, setQuery] = useState('')
  const [zone, setZone] = useState('')
  const [onlyAnpr, setOnlyAnpr] = useState(false)
  const [counts, setCounts] = useState<Record<string, number>>({})
  const [params, setParams] = useSearchParams()
  const selected = params.get('camera')

  useEffect(() => {
    endpoints.cameras('?limit=500')
      .then((r) => setCameras(r.items))
      .finally(() => setLoading(false))
  }, [])

  // Live detection counts per camera, decayed on a rolling minute. This is
  // what makes the wall feel live without any video at all.
  useEffect(() => {
    liveFeed.subscribe('sightings')
    const off = liveFeed.on((channel, data) => {
      if (channel !== 'sightings') return
      const cam = data.camera_id as string
      setCounts((prev) => ({ ...prev, [cam]: (prev[cam] || 0) + 1 }))
    })
    const decay = window.setInterval(() => setCounts({}), 60_000)
    return () => { off(); liveFeed.unsubscribe('sightings'); window.clearInterval(decay) }
  }, [])

  const zones = useMemo(
    () => [...new Set(cameras.map((c) => c.zone).filter(Boolean))].sort() as string[],
    [cameras])

  const visible = cameras.filter((c) =>
    (!query || c.name.toLowerCase().includes(query.toLowerCase())
             || c.camera_id.toLowerCase().includes(query.toLowerCase())) &&
    (!zone || c.zone === zone) &&
    (!onlyAnpr || c.anpr_capable))

  if (loading) return <Loading rows={6} />

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="toolbar">
        <div className="field" style={{ width: 260 }}>
          <label>Search</label>
          <input type="search" placeholder="Camera name or ID"
                 value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
        <div className="field" style={{ width: 190 }}>
          <label>Zone</label>
          <select value={zone} onChange={(e) => setZone(e.target.value)}>
            <option value="">All zones</option>
            {zones.map((z) => <option key={z} value={z}>{z}</option>)}
          </select>
        </div>
        <button className={`btn${onlyAnpr ? ' primary' : ''}`}
                onClick={() => setOnlyAnpr((v) => !v)}>
          ANPR-capable only
        </button>
        <div className="spacer" />
        <div className="row small muted">
          <Badge tone="ok">{cameras.filter((c) => c.status === 'ONLINE').length} online</Badge>
          <Badge tone="crit">{cameras.filter((c) => c.status !== 'ONLINE').length} offline</Badge>
          <span>{visible.length} shown</span>
        </div>
      </div>

      {visible.length === 0 ? (
        <Panel><Empty>No cameras match these filters.</Empty></Panel>
      ) : (
        <div className="cam-grid">
          {visible.map((c) => (
            <CameraTile key={c.camera_id} camera={c}
                        selected={selected === c.camera_id}
                        liveCount={counts[c.camera_id] || 0}
                        onSelect={() => setParams(
                          selected === c.camera_id ? {} : { camera: c.camera_id })} />
          ))}
        </div>
      )}

      <Panel title="Estate composition" flush>
        <table className="data">
          <thead><tr><th>Protocol</th><th className="num">Cameras</th>
            <th className="num">ANPR-capable</th><th>Vendors</th></tr></thead>
          <tbody>
            {Object.entries(
              cameras.reduce((acc, c) => {
                acc[c.protocol] ??= { n: 0, anpr: 0, vendors: new Set<string>() }
                acc[c.protocol].n++
                if (c.anpr_capable) acc[c.protocol].anpr++
                if (c.vendor) acc[c.protocol].vendors.add(c.vendor)
                return acc
              }, {} as Record<string, { n: number; anpr: number; vendors: Set<string> }>),
            ).sort((a, b) => b[1].n - a[1].n).map(([proto, v]) => (
              <tr key={proto}>
                <td><Badge tone={proto === 'DVR' ? 'warn' : 'neutral'}>{proto}</Badge></td>
                <td className="num">{v.n}</td>
                <td className="num">{v.anpr}</td>
                <td className="muted small">{[...v.vendors].join(', ') || '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
    </div>
  )
}
