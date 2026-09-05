import { useState, type FormEvent } from 'react'
import { login, type User } from '../lib/api'

export default function Login({ onSignedIn }: { onSignedIn: (u: User) => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true); setError('')
    try {
      onSignedIn(await login(username, password))
    } catch (err) {
      // The API deliberately does not distinguish a wrong password from an
      // unknown user, and the UI must not either.
      setError(err instanceof Error ? err.message : 'sign-in failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-page">
      <form className="login-card" onSubmit={submit}>
        <div className="login-head">
          <svg width="40" height="40" viewBox="0 0 24 24" fill="none">
            <path d="M12 2 4 5.5v6c0 5 3.4 9.6 8 10.5 4.6-.9 8-5.5 8-10.5v-6L12 2Z"
                  stroke="#4cc9f0" strokeWidth="1.5" strokeLinejoin="round" />
            <circle cx="12" cy="11" r="3" stroke="#4cc9f0" strokeWidth="1.5" />
            <circle cx="12" cy="11" r="1" fill="#4cc9f0" />
          </svg>
          <div className="login-title">SENTINEL</div>
          <div className="login-sub">Unified Command Centre</div>
        </div>

        <div className="col" style={{ gap: 13 }}>
          {error && <div className="error-box">{error}</div>}
          <div className="field">
            <label htmlFor="u">Username</label>
            <input id="u" type="text" autoComplete="username" autoFocus
                   value={username} onChange={(e) => setUsername(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="p">Password</label>
            <input id="p" type="password" autoComplete="current-password"
                   value={password} onChange={(e) => setPassword(e.target.value)} />
          </div>
          <button className="btn primary" type="submit" disabled={busy || !username || !password}>
            {busy ? 'Signing in…' : 'Sign in'}
          </button>
        </div>

        <div style={{ marginTop: 16, fontSize: 10.5, color: 'var(--fg-2)', lineHeight: 1.6 }}>
          Access is logged. Viewing vehicle movement history requires a stated
          purpose and is recorded against your account under the DPDP Act 2023.
        </div>
      </form>
    </div>
  )
}
