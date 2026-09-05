/**
 * API client.
 *
 * Tokens live in memory with a localStorage mirror. Pure localStorage is
 * XSS-readable; httpOnly cookies would be better but need the API and the
 * UI on one origin plus CSRF handling. This is the honest middle ground
 * for a prototype, and it is called out in docs/SECURITY.md rather than
 * left to be discovered.
 *
 * A 401 triggers exactly one refresh attempt, and concurrent requests share
 * it: without that, a dashboard with six live tiles fires six refreshes on
 * every expiry and each rotation invalidates the previous one.
 */

const BASE = import.meta.env.VITE_API_URL || ''
const API = `${BASE}/api/v1`

export interface User {
  id: string
  username: string
  full_name?: string
  role: 'VIEWER' | 'OPERATOR' | 'INVESTIGATOR' | 'ADMIN' | 'SYSTEM'
  department: string | null
  permissions?: string[]
}

let accessToken: string | null = localStorage.getItem('sentinel.access') || null
let refreshToken: string | null = localStorage.getItem('sentinel.refresh') || null
let refreshInFlight: Promise<boolean> | null = null

export function getToken() { return accessToken }
export function isAuthenticated() { return !!accessToken }

function setTokens(access: string | null, refresh: string | null) {
  accessToken = access
  refreshToken = refresh
  if (access) localStorage.setItem('sentinel.access', access)
  else localStorage.removeItem('sentinel.access')
  if (refresh) localStorage.setItem('sentinel.refresh', refresh)
  else localStorage.removeItem('sentinel.refresh')
}

export class ApiError extends Error {
  constructor(public status: number, message: string, public detail?: unknown) {
    super(message)
  }
}

async function attemptRefresh(): Promise<boolean> {
  if (!refreshToken) return false
  if (refreshInFlight) return refreshInFlight
  refreshInFlight = (async () => {
    try {
      const res = await fetch(`${API}/auth/refresh`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refreshToken }),
      })
      if (!res.ok) { setTokens(null, null); return false }
      const body = await res.json()
      setTokens(body.access_token, body.refresh_token)
      return true
    } catch {
      return false
    } finally {
      refreshInFlight = null
    }
  })()
  return refreshInFlight
}

interface RequestOptions extends RequestInit {
  /** DPDP Act purpose statement. Required by endpoints exposing an
   *  identifiable person's movement history. */
  reason?: string
  retried?: boolean
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { reason, retried, ...init } = options
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(init.headers as Record<string, string> | undefined),
  }
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`
  if (reason) headers['X-Reason'] = reason

  const res = await fetch(`${API}${path}`, { ...init, headers })

  if (res.status === 401 && !retried && refreshToken) {
    if (await attemptRefresh()) {
      return request<T>(path, { ...options, retried: true })
    }
    setTokens(null, null)
    window.dispatchEvent(new CustomEvent('sentinel:signed-out'))
    throw new ApiError(401, 'session expired')
  }

  if (!res.ok) {
    let detail: unknown
    let message = res.statusText
    try {
      const body = await res.json()
      detail = body
      message = typeof body.detail === 'string' ? body.detail : message
    } catch { /* non-JSON error body */ }
    throw new ApiError(res.status, message, detail)
  }

  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

export const api = {
  get:  <T>(p: string, o?: RequestOptions) => request<T>(p, { ...o, method: 'GET' }),
  post: <T>(p: string, body?: unknown, o?: RequestOptions) =>
    request<T>(p, { ...o, method: 'POST', body: body ? JSON.stringify(body) : undefined }),
  patch: <T>(p: string, body?: unknown, o?: RequestOptions) =>
    request<T>(p, { ...o, method: 'PATCH', body: body ? JSON.stringify(body) : undefined }),
  del: <T>(p: string, o?: RequestOptions) => request<T>(p, { ...o, method: 'DELETE' }),
}

export async function login(username: string, password: string): Promise<User> {
  const res = await fetch(`${API}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new ApiError(res.status, body.detail || 'sign-in failed')
  }
  const body = await res.json()
  setTokens(body.access_token, body.refresh_token)
  return body.user as User
}

export async function logout(): Promise<void> {
  try { await api.post('/auth/logout') } catch { /* token may already be gone */ }
  setTokens(null, null)
}

export const me = () => api.get<User>('/auth/me')

/* ── typed endpoints ─────────────────────────────────────────────── */

export interface DashboardStats {
  cameras_online: number; cameras_offline: number; cameras_total: number
  active_alerts: number; critical_alerts: number
  vehicles_tracked_1h: number; vehicles_tracked_24h: number
  anpr_events_1h: number; anpr_events_24h: number
  sightings_1h: number; watchlist_active: number
  cross_camera_tracks_24h: number
}

export interface Alert {
  alert_id: string; timestamp: string; alert_type: string
  severity: 'INFO' | 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL'
  state: string; title: string; message: string
  camera_ref: string | null; camera_name: string | null
  vehicle_track_id: string | null; sighting_id: string | null
  plate: string | null; latitude: number | null; longitude: number | null
  confidence: number; evidence: Record<string, unknown>
}

export interface Camera {
  camera_id: string; name: string; protocol: string; role: string
  status: 'PENDING' | 'PROBING' | 'ONLINE' | 'DEGRADED' | 'OFFLINE' | 'DISABLED'
  latitude: number; longitude: number; heading_deg: number | null
  fov_deg: number; range_m: number; zone: string | null; district: string | null
  department: string; vendor: string | null; signal_class: string | null
  firmware_risk: string | null; width: number | null; height: number | null
  fps: number | null; anpr_capable: boolean; trust_score: number
  last_seen: string | null; consecutive_failures: number; tags: string[]
  seconds_since_seen: number | null
}

export interface Vehicle {
  vehicle_track_id: string; best_plate: string | null; best_plate_conf: number | null
  vehicle_type: string; vehicle_color: string | null
  first_seen: string; last_seen: string
  sighting_count: number; camera_count: number; plate_read_count: number
  is_watchlisted: boolean; total_distance_m: number | null
  duration_seconds?: number; cameras?: string[]
}

export interface TimelineHop {
  sighting_id: string; timestamp: string; camera_ref: string
  camera_name: string | null; zone: string | null
  latitude: number | null; longitude: number | null
  heading_deg: number | null; speed_kmph: number | null
  vehicle_type: string; vehicle_color: string | null
  plate_normalized: string | null; plate_confidence: number | null
  quality_score: number; detection_count: number
  gap_seconds: number | null
  association: {
    decision: 'AUTO' | 'PROBABLE' | 'SEED' | 'OPERATOR' | 'REJECTED'
    confidence: number
    scores?: Record<string, number>
    travel_expected_s?: number | null
    travel_actual_s?: number | null
    reasons: string[]
    operator_verdict?: string | null
  }
}

export const endpoints = {
  dashboard: () => api.get<{
    stats: DashboardStats; recent_alerts: Alert[]; active_tracks: Vehicle[]
  }>('/dashboard'),

  cameras: (q = '') => api.get<{ items: Camera[]; total: number }>(`/cameras${q}`),
  camerasGeoJSON: () => api.get<GeoJSON.FeatureCollection>('/cameras/geojson'),
  cameraHealth: () => api.get<{ summary: Record<string, number>; cameras: any[] }>('/cameras/health'),
  cameraStream: (id: string) => api.get<{ whep_url: string; llhls_url: string }>(`/cameras/${id}/stream`),

  alerts: (q = '') => api.get<{ items: Alert[]; count: number }>(`/alerts${q}`),
  alertSummary: () => api.get<any>('/alerts/summary'),
  ackAlert: (id: string, state: string, note?: string) =>
    api.post(`/alerts/${id}/ack`, { state, note }),

  searchVehicles: (q: string) => api.get<{
    items: Vehicle[]; count: number
    search?: { query: string; canonical: string; match_type: string; note: string }
  }>(`/vehicles/search${q}`),
  vehicle: (id: string) => api.get<Vehicle>(`/vehicles/${id}`),
  timeline: (id: string, reason: string) => api.get<{
    vehicle_track_id: string; hop_count: number; camera_count: number
    confirmed_hops: number; probable_hops: number; hops: TimelineHop[]
  }>(`/vehicles/${id}/timeline`, { reason }),
  trackGeoJSON: (id: string) => api.get<GeoJSON.FeatureCollection>(`/vehicles/${id}/track.geojson`),
  nextCameras: (id: string) => api.get<any>(`/vehicles/${id}/next-cameras`),
  similar: (sightingId: string, applyGate: boolean, reason: string) =>
    api.post<any>('/vehicles/similar', { sighting_id: sightingId, apply_gate: applyGate }, { reason }),
  pendingLinks: () => api.get<{ items: any[] }>('/vehicles/links/pending'),
  linkVerdict: (id: string, verdict: string, note?: string) =>
    api.post(`/vehicles/links/${id}/verdict`, { verdict, note }),

  liveSightings: (minutes = 5) =>
    api.get<GeoJSON.FeatureCollection>(`/sightings/live.geojson?minutes=${minutes}`),

  watchlist: () => api.get<{ items: any[] }>('/watchlist'),
  addWatchlist: (body: unknown) => api.post<any>('/watchlist', body),
  removeWatchlist: (id: string) => api.del(`/watchlist/${id}`),

  alertRules: () => api.get<{ items: any[] }>('/alert-rules'),
  updateRule: (code: string, body: unknown) => api.patch(`/alert-rules/${code}`, body),

  analyticsTimeline: (hours = 6) => api.get<any>(`/analytics/timeline?hours=${hours}`),
  analyticsCameras: () => api.get<{ items: any[] }>('/analytics/cameras'),
  analyticsMix: () => api.get<any>('/analytics/vehicle-mix'),
  analyticsAnpr: () => api.get<any>('/analytics/anpr'),
  systemStatus: () => api.get<any>('/system/status'),
}
