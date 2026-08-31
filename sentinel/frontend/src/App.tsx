import { useEffect, useState } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { endpoints, isAuthenticated, logout, me, type User } from './lib/api'
import { liveFeed } from './lib/ws'
import Layout from './components/Layout'
import Login from './pages/Login'
import CommandCentre from './pages/CommandCentre'
import LiveCameras from './pages/LiveCameras'
import GISMap from './pages/GISMap'
import Alerts from './pages/Alerts'
import VehicleSearch from './pages/VehicleSearch'
import VehicleTracking from './pages/VehicleTracking'
import CameraHealth from './pages/CameraHealth'
import Analytics from './pages/Analytics'

export default function App() {
  const [user, setUser] = useState<User | null>(null)
  const [checking, setChecking] = useState(true)
  const [openAlerts, setOpenAlerts] = useState(0)

  // Restore the session on load. A stored token may be expired or revoked,
  // so it is validated against the API rather than trusted.
  useEffect(() => {
    if (!isAuthenticated()) { setChecking(false); return }
    me().then(setUser).catch(() => setUser(null)).finally(() => setChecking(false))
  }, [])

  useEffect(() => {
    const signedOut = () => setUser(null)
    window.addEventListener('sentinel:signed-out', signedOut)
    return () => window.removeEventListener('sentinel:signed-out', signedOut)
  }, [])

  useEffect(() => {
    if (!user) { liveFeed.close(); return }
    liveFeed.connect()
    const off = liveFeed.on((channel) => {
      if (channel === 'alerts') setOpenAlerts((n) => n + 1)
    })
    endpoints.alertSummary()
      .then((s) => setOpenAlerts(s.counts?.open ?? 0))
      .catch(() => {})
    return () => { off() }
  }, [user])

  async function signOut() {
    await logout()
    liveFeed.close()
    setUser(null)
  }

  if (checking) {
    return (
      <div className="login-page">
        <div className="muted">Restoring session…</div>
      </div>
    )
  }

  if (!user) return <Login onSignedIn={setUser} />

  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout user={user} onSignOut={signOut} openAlerts={openAlerts} />}>
          <Route index element={<CommandCentre />} />
          <Route path="cameras" element={<LiveCameras />} />
          <Route path="map" element={<GISMap />} />
          <Route path="alerts" element={<Alerts />} />
          <Route path="search" element={<VehicleSearch />} />
          <Route path="tracking" element={<VehicleTracking />} />
          <Route path="health" element={<CameraHealth />} />
          <Route path="analytics" element={<Analytics />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
