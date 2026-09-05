/**
 * MapLibre map with camera, field-of-view, sighting and track layers.
 *
 * MapLibre rather than Mapbox GL: Mapbox went proprietary at v2 and needs a
 * token plus a network call per session. Sentinel may run air-gapped on a
 * state network, so a token-free renderer with self-hostable tiles is the
 * only defensible choice. The basemap URL is configurable for exactly that
 * reason -- point it at an internal tile server and nothing leaves the WAN.
 */
import { useEffect, useRef } from 'react'
import maplibregl, { Map as MLMap } from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'

export interface MapViewProps {
  cameras?: GeoJSON.FeatureCollection | null
  sightings?: GeoJSON.FeatureCollection | null
  track?: GeoJSON.FeatureCollection | null
  highlightCameras?: string[]
  onCameraClick?: (cameraId: string) => void
  fitTo?: 'cameras' | 'track' | null
  showFov?: boolean
  height?: string | number
}

// A dark raster basemap keeps operator focus on the overlays. Swap the
// tile URL for an internal server (or Bhuvan/ISRO WMTS) in production.
const BASEMAP = import.meta.env.VITE_TILE_URL ||
  'https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png'

const STYLE: any = {
  version: 8,
  sources: {
    base: {
      type: 'raster', tiles: [BASEMAP], tileSize: 256,
      attribution: '© OpenStreetMap contributors © CARTO',
    },
  },
  layers: [
    { id: 'bg', type: 'background', paint: { 'background-color': '#070a0f' } },
    { id: 'base', type: 'raster', source: 'base', paint: { 'raster-opacity': 0.82 } },
  ],
}

const EMPTY: GeoJSON.FeatureCollection = { type: 'FeatureCollection', features: [] }

export default function MapView({
  cameras, sightings, track, highlightCameras = [], onCameraClick,
  fitTo = 'cameras', showFov = true, height = '100%',
}: MapViewProps) {
  const container = useRef<HTMLDivElement>(null)
  const map = useRef<MLMap | null>(null)
  const ready = useRef(false)
  const didFit = useRef(false)

  useEffect(() => {
    if (!container.current || map.current) return
    const m = new maplibregl.Map({
      container: container.current,
      style: STYLE,
      center: [72.5714, 23.0225],   // Ahmedabad
      zoom: 11,
      attributionControl: false,
    })
    map.current = m
    m.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-left')
    m.addControl(new maplibregl.AttributionControl({ compact: true }), 'bottom-right')

    // A tile CDN is not reachable from an air-gapped network. Log it once
    // and carry on: the dark background layer plus the overlays remain fully
    // usable, which is the correct degraded behaviour for a control room.
    m.on('error', (e: any) => {
      const msg = String(e?.error?.message || e?.error || '')
      if (msg.includes('Failed to fetch') || msg.includes('AJAXError')) return
      console.warn('[map]', msg)
    })

    m.on('load', () => {
      for (const id of ['fov', 'cameras', 'sightings', 'track']) {
        m.addSource(id, { type: 'geojson', data: EMPTY })
      }

      m.addLayer({
        id: 'fov-fill', type: 'fill', source: 'fov',
        filter: ['==', ['get', 'kind'], 'fov'],
        paint: {
          'fill-color': ['match', ['get', 'status'],
            'ONLINE', '#4cc9f0', 'DEGRADED', '#d98a1a', '#e5484d'],
          'fill-opacity': 0.09,
        },
      })
      m.addLayer({
        id: 'fov-line', type: 'line', source: 'fov',
        filter: ['==', ['get', 'kind'], 'fov'],
        paint: { 'line-color': '#4cc9f0', 'line-width': 0.6, 'line-opacity': 0.28 },
      })

      // Observed path. Drawn solid because every segment joins two real
      // sightings -- nothing here is inferred, and styling an inferred
      // segment identically would misrepresent evidence.
      m.addLayer({
        id: 'track-line', type: 'line', source: 'track',
        filter: ['==', ['geometry-type'], 'LineString'],
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': '#4cc9f0', 'line-width': 3, 'line-opacity': 0.85 },
      })
      m.addLayer({
        id: 'track-points', type: 'circle', source: 'track',
        filter: ['==', ['geometry-type'], 'Point'],
        paint: {
          'circle-radius': 6, 'circle-color': '#4cc9f0',
          'circle-stroke-width': 2, 'circle-stroke-color': '#070a0f',
        },
      })
      // Deliberately NO symbol/text layer. MapLibre's text-field requires a
      // `glyphs` font-server URL, which means an outbound dependency on a
      // font CDN -- unavailable on an air-gapped state network, and it fails
      // by throwing rather than degrading. Hop order is conveyed by the
      // hollow ring on the first point and by the timeline beside the map.
      m.addLayer({
        id: 'track-first', type: 'circle', source: 'track',
        filter: ['all', ['==', ['geometry-type'], 'Point'], ['==', ['get', 'seq'], 1]],
        paint: {
          'circle-radius': 10, 'circle-color': 'transparent',
          'circle-stroke-width': 2, 'circle-stroke-color': '#4cc9f0',
          'circle-stroke-opacity': 0.7,
        },
      })

      m.addLayer({
        id: 'sighting-points', type: 'circle', source: 'sightings',
        paint: {
          'circle-radius': 3, 'circle-color': '#a3b1c2',
          'circle-opacity': 0.55,
        },
      })

      m.addLayer({
        id: 'camera-points', type: 'circle', source: 'cameras',
        filter: ['==', ['get', 'kind'], 'camera'],
        paint: {
          'circle-radius': ['case', ['get', 'highlight'], 8, 5],
          'circle-color': ['match', ['get', 'status'],
            'ONLINE', '#2ea86b', 'DEGRADED', '#d98a1a', '#e5484d'],
          'circle-stroke-width': ['case', ['get', 'highlight'], 3, 1.5],
          'circle-stroke-color': ['case', ['get', 'highlight'], '#4cc9f0', '#070a0f'],
        },
      })

      m.on('click', 'camera-points', (e) => {
        const f = e.features?.[0]
        if (f?.properties?.camera_id) onCameraClick?.(String(f.properties.camera_id))
      })
      m.on('mouseenter', 'camera-points', () => { m.getCanvas().style.cursor = 'pointer' })
      m.on('mouseleave', 'camera-points', () => { m.getCanvas().style.cursor = '' })

      const popup = new maplibregl.Popup({ closeButton: false, offset: 10 })
      m.on('mouseenter', 'camera-points', (e) => {
        const p = e.features?.[0]?.properties
        if (!p) return
        popup.setLngLat(e.lngLat).setHTML(
          `<div style="font:12px system-ui;color:#0d1219">
             <b>${p.name ?? p.camera_id}</b><br/>
             ${p.camera_id} · ${p.status}${p.anpr_capable === true || p.anpr_capable === 'true' ? ' · ANPR' : ''}
           </div>`).addTo(m)
      })
      m.on('mouseleave', 'camera-points', () => popup.remove())

      ready.current = true
      m.fire('sentinel:ready')
    })

    return () => { m.remove(); map.current = null; ready.current = false }
  }, [onCameraClick])

  // Cameras + FOV
  useEffect(() => {
    const m = map.current
    if (!m || !cameras) return
    const apply = () => {
      const highlighted = new Set(highlightCameras)
      const decorated: GeoJSON.FeatureCollection = {
        type: 'FeatureCollection',
        features: cameras.features.map((f) => ({
          ...f,
          properties: {
            ...(f.properties || {}),
            highlight: highlighted.has(String(f.properties?.camera_id)),
          },
        })),
      }
      ;(m.getSource('cameras') as any)?.setData(decorated)
      ;(m.getSource('fov') as any)?.setData(showFov ? decorated : EMPTY)

      if (fitTo === 'cameras' && !didFit.current) {
        const pts = cameras.features.filter((f) => f.geometry?.type === 'Point')
        if (pts.length) {
          const b = new maplibregl.LngLatBounds()
          pts.forEach((f) => b.extend((f.geometry as any).coordinates))
          m.fitBounds(b, { padding: 60, maxZoom: 14, duration: 0 })
          didFit.current = true
        }
      }
    }
    ready.current ? apply() : m.once('sentinel:ready', apply)
  }, [cameras, highlightCameras, showFov, fitTo])

  useEffect(() => {
    const m = map.current
    if (!m) return
    const apply = () => (m.getSource('sightings') as any)?.setData(sightings || EMPTY)
    ready.current ? apply() : m.once('sentinel:ready', apply)
  }, [sightings])

  useEffect(() => {
    const m = map.current
    if (!m) return
    const apply = () => {
      ;(m.getSource('track') as any)?.setData(track || EMPTY)
      const pts = track?.features.filter((f) => f.geometry?.type === 'Point') || []
      if (fitTo === 'track' && pts.length) {
        const b = new maplibregl.LngLatBounds()
        pts.forEach((f) => b.extend((f.geometry as any).coordinates))
        m.fitBounds(b, { padding: 90, maxZoom: 15, duration: 700 })
      }
    }
    ready.current ? apply() : m.once('sentinel:ready', apply)
  }, [track, fitTo])

  return <div ref={container} className="map-wrap" style={{ height }} />
}

export function MapLegend({ items }: { items: [string, string][] }) {
  return (
    <div className="map-legend">
      {items.map(([colour, label]) => (
        <div className="map-legend-row" key={label}>
          <span className="dot" style={{ background: colour }} />
          <span className="muted">{label}</span>
        </div>
      ))}
    </div>
  )
}
