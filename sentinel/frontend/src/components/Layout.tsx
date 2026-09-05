import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { useEffect, useState } from 'react'
import { liveFeed } from '../lib/ws'
import type { User } from '../lib/api'

const NAV = [
  { section: 'Operations' },
  { to: '/', label: 'Command Centre', end: true },
  { to: '/cameras', label: 'Live Cameras' },
  { to: '/map', label: 'GIS Map' },
  { to: '/alerts', label: 'Alerts' },
  { section: 'Investigation' },
  { to: '/search', label: 'Vehicle Search' },
  { to: '/tracking', label: 'Vehicle Tracking' },
  { section: 'System' },
  { to: '/health', label: 'Camera Health' },
  { to: '/analytics', label: 'System Analytics' },
]

const TITLES: Record<string, string> = {
  '/': 'Command Centre', '/cameras': 'Live Cameras', '/map': 'GIS Map',
  '/alerts': 'Alerts', '/search': 'Vehicle Search', '/tracking': 'Vehicle Tracking',
  '/health': 'Camera Health', '/analytics': 'System Analytics',
}

export default function Layout({ user, onSignOut, openAlerts }:
  { user: User; onSignOut: () => void; openAlerts: number }) {
  const [live, setLive] = useState(liveFeed.connected)
  const [clock, setClock] = useState(new Date())
  const location = useLocation()

  useEffect(() => {
    liveFeed.onStatusChange = setLive
    const t = window.setInterval(() => setClock(new Date()), 1000)
    return () => { window.clearInterval(t); liveFeed.onStatusChange = null }
  }, [])

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none">
              <path d="M12 2 4 5.5v6c0 5 3.4 9.6 8 10.5 4.6-.9 8-5.5 8-10.5v-6L12 2Z"
                    stroke="#4cc9f0" strokeWidth="1.6" strokeLinejoin="round" />
              <circle cx="12" cy="11" r="3" stroke="#4cc9f0" strokeWidth="1.6" />
              <circle cx="12" cy="11" r="1" fill="#4cc9f0" />
            </svg>
            <div>
              <div className="brand-name">SENTINEL</div>
            </div>
          </div>
          <div className="brand-sub">Gujarat Police · Unified VMS</div>
        </div>

        <nav className="nav">
          {NAV.map((item, i) =>
            'section' in item ? (
              <div key={i} className="nav-section">{item.section}</div>
            ) : (
              <NavLink key={item.to} to={item.to!} end={item.end}
                       className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}>
                {item.label}
                {item.to === '/alerts' && openAlerts > 0 && (
                  <span className="badge crit">{openAlerts > 99 ? '99+' : openAlerts}</span>
                )}
              </NavLink>
            ),
          )}
        </nav>

        <div className="sidebar-foot">
          <div className="row" style={{ justifyContent: 'space-between' }}>
            <div>
              <div style={{ color: 'var(--fg-1)', fontSize: 12 }}>{user.username}</div>
              <div style={{ fontSize: 10 }}>{user.role} · {user.department || 'no dept'}</div>
            </div>
            <button className="btn sm" onClick={onSignOut}>Sign out</button>
          </div>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <h1>{TITLES[location.pathname] || 'Sentinel'}</h1>
          <div className="spacer" />
          {/* Feed status is always visible. An operator must never be able to
              believe they are seeing live alerts when the socket is down. */}
          <div className="row small" title={live ? 'Live feed connected' : 'Reconnecting to live feed'}>
            <span className={`dot ${live ? 'live' : 'crit'}`} />
            <span className="muted">{live ? 'LIVE' : 'RECONNECTING'}</span>
          </div>
          <div className="mono muted" style={{ fontSize: 12 }}>
            {clock.toLocaleTimeString('en-IN', { hour12: false })} IST
          </div>
        </header>
        <main className="content">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
