// Spike protocol v1 — RPC envelope.
//
// Architecture: extension is a thin DOM I/O bridge for V1's FlowPort
// protocol (app/worker/flow_port.py). Center owns task lifecycle / state
// machine / strike / scheduler; extension only exposes atomic page
// operations as RPC methods. SW state is therefore minimal — every RPC
// is independent, so SW hibernate just delays the next call (no Veo
// double-charge / no lifecycle recovery to worry about in extension).

export const PROTOCOL_VERSION = 1

// === Center → Extension ===
export type CenterToExtension =
  | RegisterAck
  | PingMsg
  | RpcRequest

export type RegisterAck = {
  type: 'register_ack'
  assigned_ws_id: string
}

export type PingMsg = {
  type: 'ping'
  ts: number
}

/**
 * RPC method names map 1:1 onto FlowPort's surface (subset for spike).
 *
 * Spike phase exposes 8:
 *   - read_page_state      — sanity probe; returns url + body text snippet
 *   - paste_prompt         — write into a textarea (Grammarly/IME-defended)
 *   - take_screenshot      — captureVisibleTab; binary returned as data URL
 *   - trigger_generation   — click first matching selector (multi-lang fallback)
 *   - attach_image         — fetch URL → DataTransfer → file input (1Password-defended)
 *   - scrape_candidates    — snapshot current candidate video src list
 *   - wait_round_complete  — poll for new video src + stability window
 *   - download_video       — chrome.downloads.download to FlowHarvester/output_v2/...
 */
export type RpcMethod =
  | 'read_page_state'
  | 'paste_prompt'
  | 'take_screenshot'
  | 'trigger_generation'
  | 'attach_image'
  | 'scrape_candidates'
  | 'wait_round_complete'
  | 'download_video'

export type RpcRequest = {
  type: 'rpc'
  rpc_id: string
  method: RpcMethod
  // Optional URL the SW should ensure the active flow tab is on before
  // running the call. If absent and there's no active tab, RPC errors.
  target_url?: string
  args: Record<string, unknown>
  /** Server-side soft timeout hint (seconds). SW also caps locally. */
  timeout_sec?: number
}

// === Extension → Center ===
export type ExtensionToCenter =
  | RegisterMsg
  | HeartbeatMsg
  | RpcResult
  | LogMsg

export type RegisterMsg = {
  type: 'register'
  protocol_version: typeof PROTOCOL_VERSION
  workstation_id: string
  extension_version: string
  chrome_version: string
  ts: number
}

export type HeartbeatMsg = {
  type: 'heartbeat'
  ts: number
}

export type RpcResult = {
  type: 'rpc_result'
  rpc_id: string
  ok: boolean
  payload?: unknown
  error?: string
  /** ts when SW finished the call; for round-trip latency measurement. */
  ts: number
}

export type LogMsg = {
  type: 'log'
  level: 'debug' | 'info' | 'warn' | 'error'
  message: string
  ts: number
}

export function nowMs(): number {
  return Date.now()
}
