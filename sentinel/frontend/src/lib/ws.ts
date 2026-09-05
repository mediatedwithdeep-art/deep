/**
 * WebSocket client with exponential backoff.
 *
 * Reconnection matters more than it looks. A control room leaves this open
 * for a twelve-hour shift, and every proxy, VPN and laptop sleep in between
 * will drop it. A client that gives up after one failure means an operator
 * silently stops receiving alerts and has no way to know.
 */

import { getToken } from './api'

type Handler = (channel: string, data: any) => void

const BASE = import.meta.env.VITE_API_URL || ''

export class LiveFeed {
  private socket: WebSocket | null = null
  private handlers = new Set<Handler>()
  private channels = new Set<string>(['alerts'])
  private attempt = 0
  private timer: number | null = null
  private closing = false

  connected = false
  onStatusChange: ((connected: boolean) => void) | null = null

  connect() {
    const token = getToken()
    if (!token || this.socket) return
    this.closing = false

    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const host = BASE ? BASE.replace(/^https?:/, proto) : `${proto}//${window.location.host}`
    this.socket = new WebSocket(`${host}/api/v1/ws?token=${encodeURIComponent(token)}`)

    this.socket.onopen = () => {
      this.attempt = 0
      this.connected = true
      this.onStatusChange?.(true)
      this.send({ action: 'subscribe', channels: [...this.channels] })
    }

    this.socket.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data)
        if (msg.type === 'event') {
          this.handlers.forEach((h) => h(msg.channel, msg.data))
        }
      } catch { /* ignore malformed frames rather than tearing down the feed */ }
    }

    this.socket.onclose = () => {
      this.socket = null
      this.connected = false
      this.onStatusChange?.(false)
      if (!this.closing) this.scheduleReconnect()
    }

    this.socket.onerror = () => this.socket?.close()
  }

  private scheduleReconnect() {
    // Exponential backoff capped at 30 s, with jitter. Without jitter, every
    // wall display in the control room reconnects in lockstep after an API
    // restart and stampedes it.
    const delay = Math.min(30_000, 800 * 2 ** this.attempt) * (0.7 + Math.random() * 0.6)
    this.attempt = Math.min(this.attempt + 1, 6)
    if (this.timer) window.clearTimeout(this.timer)
    this.timer = window.setTimeout(() => this.connect(), delay)
  }

  private send(payload: unknown) {
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify(payload))
    }
  }

  subscribe(...channels: string[]) {
    channels.forEach((c) => this.channels.add(c))
    this.send({ action: 'subscribe', channels })
  }

  unsubscribe(...channels: string[]) {
    channels.forEach((c) => this.channels.delete(c))
    this.send({ action: 'unsubscribe', channels })
  }

  on(handler: Handler) {
    this.handlers.add(handler)
    return () => { this.handlers.delete(handler) }
  }

  close() {
    this.closing = true
    if (this.timer) window.clearTimeout(this.timer)
    this.socket?.close()
    this.socket = null
  }
}

export const liveFeed = new LiveFeed()
