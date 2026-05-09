import type { ExtensionToCenter, CenterToExtension } from './protocol'

// Spike WS client — exponential backoff + jitter (design v0.6 §4.4.5
// C-034). Single connection per service worker; sw hibernate kills it,
// chrome.alarms 30s wakeup recreates.

const WS_URL_BASE = 'ws://127.0.0.1:8080/ws/extension'
const RECONNECT_BASE_SEC = 1
const RECONNECT_CAP_SEC = 30
const JITTER_MAX_SEC = 5

type Listener = (msg: CenterToExtension) => void
type OpenListener = () => void

export class WsClient {
  private ws: WebSocket | null = null
  private wsId: string
  private listeners: Listener[] = []
  private openListeners: OpenListener[] = []
  private retryCount = 0
  private connecting = false
  private intentionalClose = false

  constructor(wsId: string) {
    this.wsId = wsId
  }

  connect(): void {
    if (this.connecting || this.ws?.readyState === WebSocket.OPEN) return
    this.connecting = true
    this.intentionalClose = false

    const url = `${WS_URL_BASE}/${encodeURIComponent(this.wsId)}`
    console.log('[ws_client] connecting', url)
    const ws = new WebSocket(url)
    this.ws = ws

    ws.addEventListener('open', () => {
      console.log('[ws_client] open')
      this.retryCount = 0
      this.connecting = false
      // Fire register / re-sync hooks immediately on the real `open`
      // event. Polling via setTimeout is unreliable in MV3 SW because
      // the timer is dropped if the SW hibernates between scheduling
      // and the deadline — that's why early spike runs never produced
      // a `register` log line on the server side.
      for (const fn of this.openListeners) {
        try {
          fn()
        } catch (e) {
          console.warn('[ws_client] openListener threw', e)
        }
      }
    })

    ws.addEventListener('message', (ev) => {
      try {
        const msg = JSON.parse(ev.data) as CenterToExtension
        for (const l of this.listeners) l(msg)
      } catch (e) {
        console.warn('[ws_client] bad message', ev.data, e)
      }
    })

    ws.addEventListener('close', (ev) => {
      console.warn('[ws_client] close', ev.code, ev.reason)
      this.ws = null
      this.connecting = false
      if (!this.intentionalClose) this.scheduleReconnect()
    })

    ws.addEventListener('error', (ev) => {
      console.warn('[ws_client] error', ev)
    })
  }

  send(msg: ExtensionToCenter): boolean {
    if (this.ws?.readyState !== WebSocket.OPEN) {
      console.warn('[ws_client] send dropped (not open)', msg.type)
      return false
    }
    this.ws.send(JSON.stringify(msg))
    return true
  }

  onMessage(fn: Listener): () => void {
    this.listeners.push(fn)
    return () => {
      const i = this.listeners.indexOf(fn)
      if (i >= 0) this.listeners.splice(i, 1)
    }
  }

  onOpen(fn: OpenListener): () => void {
    this.openListeners.push(fn)
    // If we're already open at attach time, fire once immediately so
    // late subscribers don't miss the boat.
    if (this.ws?.readyState === WebSocket.OPEN) {
      try { fn() } catch { /* swallow */ }
    }
    return () => {
      const i = this.openListeners.indexOf(fn)
      if (i >= 0) this.openListeners.splice(i, 1)
    }
  }

  close(): void {
    this.intentionalClose = true
    this.ws?.close()
    this.ws = null
  }

  isOpen(): boolean {
    return this.ws?.readyState === WebSocket.OPEN
  }

  private scheduleReconnect(): void {
    this.retryCount += 1
    const base = Math.min(RECONNECT_CAP_SEC, RECONNECT_BASE_SEC * Math.pow(2, this.retryCount - 1))
    const jitter = Math.random() * JITTER_MAX_SEC
    const delaySec = base + jitter
    console.log(`[ws_client] reconnect in ${delaySec.toFixed(1)}s (attempt ${this.retryCount})`)
    setTimeout(() => this.connect(), delaySec * 1000)
  }
}
