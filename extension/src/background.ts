import { WsClient } from './lib/ws_client'
import {
  PROTOCOL_VERSION,
  nowMs,
  type ExtensionToCenter,
  type RpcRequest,
} from './lib/protocol'
import {
  pageworldReadPageState,
  pageworldClickFirstMatching,
  pageworldAttachFile,
  pageworldScrapeCandidates,
  pageworldWaitCandidates,
  type SelectorSpec,
} from './content/flow_dom'

// Spike service worker — RPC dispatcher.
//
// Center calls atomic FlowPort-equivalent operations as JSON-RPC over
// WebSocket. Extension is stateless beyond:
//   - ws_id (chrome.storage.local, per-profile)
//   - active_flow_tab_id (in-memory; rebuilt on demand)
//
// SW hibernate is therefore safe — every RPC re-resolves its tab and
// returns a result. No task lifecycle to corrupt.

const STORAGE_KEY_WS_ID = 'ws_id'
const HEARTBEAT_ALARM = 'heartbeat'
const HEARTBEAT_PERIOD_MIN = 0.5
const DEFAULT_RPC_TIMEOUT_MS = 30_000
const TAB_LOAD_TIMEOUT_MS = 12_000

let ws: WsClient | null = null
let currentWsId: string | null = null
let activeFlowTabId: number | null = null

function chromeVersion(): string {
  const m = navigator.userAgent.match(/Chrome\/([\d.]+)/)
  return m ? m[1] : 'unknown'
}

async function getOrCreateWsId(): Promise<string> {
  const stored = await chrome.storage.local.get(STORAGE_KEY_WS_ID)
  const existing = stored[STORAGE_KEY_WS_ID]
  if (typeof existing === 'string' && existing.length > 0) return existing
  const rand = crypto.randomUUID().slice(0, 8)
  const fresh = `WS-${rand}`
  await chrome.storage.local.set({ [STORAGE_KEY_WS_ID]: fresh })
  return fresh
}

function sendRegister(): void {
  if (!ws || !currentWsId) return
  const msg: ExtensionToCenter = {
    type: 'register',
    protocol_version: PROTOCOL_VERSION,
    workstation_id: currentWsId,
    extension_version: chrome.runtime.getManifest().version,
    chrome_version: chromeVersion(),
    ts: nowMs(),
  }
  console.log('[bg] sendRegister', currentWsId)
  ws.send(msg)
}

/* --------------------------------------------------------------------- */
/* Tab resolution                                                         */
/* --------------------------------------------------------------------- */

async function ensureFlowTab(targetUrl: string | undefined): Promise<chrome.tabs.Tab> {
  // Try to re-use our remembered tab.
  if (activeFlowTabId !== null) {
    try {
      const tab = await chrome.tabs.get(activeFlowTabId)
      if (targetUrl && tab.url !== targetUrl) {
        return await navigateAndWait(tab.id!, targetUrl)
      }
      return tab
    } catch {
      activeFlowTabId = null
    }
  }
  if (!targetUrl) {
    throw new Error('no active tab and no target_url provided')
  }
  const tab = await chrome.tabs.create({ url: targetUrl, active: true })
  activeFlowTabId = tab.id ?? null
  await waitForTabComplete(tab.id!)
  return await chrome.tabs.get(tab.id!)
}

async function navigateAndWait(tabId: number, url: string): Promise<chrome.tabs.Tab> {
  await chrome.tabs.update(tabId, { url, active: true })
  await waitForTabComplete(tabId)
  return await chrome.tabs.get(tabId)
}

function waitForTabComplete(tabId: number): Promise<void> {
  return new Promise<void>((resolve) => {
    const listener = (id: number, info: chrome.tabs.TabChangeInfo) => {
      if (id === tabId && info.status === 'complete') {
        chrome.tabs.onUpdated.removeListener(listener)
        resolve()
      }
    }
    chrome.tabs.onUpdated.addListener(listener)
    setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener)
      resolve() // resolve anyway; RPC handler will probe ready state
    }, TAB_LOAD_TIMEOUT_MS)
  })
}

/* --------------------------------------------------------------------- */
/* RPC handlers                                                           */
/* --------------------------------------------------------------------- */

async function rpcReadPageState(tab: chrome.tabs.Tab): Promise<unknown> {
  if (tab.id === undefined) throw new Error('tab has no id')
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: 'MAIN',
    func: pageworldReadPageState,
  })
  return result
}

async function rpcPastePrompt(
  tab: chrome.tabs.Tab,
  args: Record<string, unknown>,
): Promise<unknown> {
  if (tab.id === undefined) throw new Error('tab has no id')
  const selector = (args.selector as string | undefined) ?? 'textarea'
  const value = args.prompt as string | undefined
  if (typeof value !== 'string') throw new Error('args.prompt (string) required')

  // V1 (flow_playwright.py:994) proves Slate.js rejects synthetic
  // InputEvent / execCommand because they have isTrusted=false. The
  // only reliable path is CDP Input.insertText which yields a trusted
  // event the editor honours. We use chrome.debugger to attach the
  // tab to its own CDP session and drive it; operator sees a yellow
  // "this browser is being debugged" bar on top — that bar must NOT
  // be closed (closing detaches us mid-task).
  const target: chrome.debugger.Debuggee = { tabId: tab.id }

  // Attach (idempotent in spirit — we swallow the "already attached"
  // case so back-to-back paste_prompt calls within one session don't
  // need to detach/reattach every time).
  try {
    await chrome.debugger.attach(target, '1.3')
  } catch (e) {
    const msg = (e as Error).message || String(e)
    if (!/already attached/i.test(msg)) {
      // Try a fresh detach + reattach in case some other client
      // stranded the target. Best-effort.
      try {
        await chrome.debugger.detach(target)
      } catch { /* ignore */ }
      try {
        await chrome.debugger.attach(target, '1.3')
      } catch (e2) {
        return {
          ok: false,
          error: `chrome.debugger.attach failed: ${(e2 as Error).message}`,
        }
      }
    }
  }

  // Step 1: focus the editor in the page world. CDP can dispatch
  // keystrokes globally, but Slate routes the inserted text into
  // whatever has focus, so we make sure that's our prompt input.
  const [focusRes] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: 'MAIN',
    func: (sel: string) => {
      const candidates = sel.split(',').map((s) => s.trim()).filter(Boolean)
      for (const s of candidates) {
        const el = document.querySelector(s) as HTMLElement | null
        if (el) {
          el.focus()
          // Place caret at end of any existing content first.
          const range = document.createRange()
          range.selectNodeContents(el)
          range.collapse(false)
          const sel2 = window.getSelection()
          sel2?.removeAllRanges()
          sel2?.addRange(range)
          return { matched: s, tag: el.tagName }
        }
      }
      return null
    },
    args: [selector],
  })
  if (!focusRes.result) {
    return { ok: false, error: `paste_prompt: no element matched selector ${selector}` }
  }

  const isMac = navigator.userAgent.includes('Macintosh')
  // Modifier bitmask per Input.dispatchKeyEvent: Alt=1 Ctrl=2 Meta=4 Shift=8
  const selectAllModifier = isMac ? 4 : 2

  // Step 2: select all + delete via real key events (Slate honors them).
  await chrome.debugger.sendCommand(target, 'Input.dispatchKeyEvent', {
    type: 'keyDown',
    modifiers: selectAllModifier,
    key: 'a',
    code: 'KeyA',
    windowsVirtualKeyCode: 65,
    nativeVirtualKeyCode: 65,
  })
  await chrome.debugger.sendCommand(target, 'Input.dispatchKeyEvent', {
    type: 'keyUp',
    modifiers: selectAllModifier,
    key: 'a',
    code: 'KeyA',
    windowsVirtualKeyCode: 65,
    nativeVirtualKeyCode: 65,
  })
  await chrome.debugger.sendCommand(target, 'Input.dispatchKeyEvent', {
    type: 'keyDown',
    key: 'Delete',
    code: 'Delete',
    windowsVirtualKeyCode: 46,
    nativeVirtualKeyCode: 46,
  })
  await chrome.debugger.sendCommand(target, 'Input.dispatchKeyEvent', {
    type: 'keyUp',
    key: 'Delete',
    code: 'Delete',
    windowsVirtualKeyCode: 46,
    nativeVirtualKeyCode: 46,
  })

  // Step 3: insert the prompt. Input.insertText simulates an IME-style
  // text-commit which produces a single trusted ``input`` event with
  // inputType='insertText' — exactly what Slate's React handler
  // wants. We prefer this over per-char dispatchKeyEvent because
  // it's atomic (no chance of timing-based interleaving with the
  // generate button enabling/disabling) and trivially handles
  // multi-byte / IME-composed unicode (Chinese, Thai, Khmer …).
  await chrome.debugger.sendCommand(target, 'Input.insertText', { text: value })

  // Step 4: read back what landed.
  const [readRes] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: 'MAIN',
    func: (sel: string) => {
      const candidates = sel.split(',').map((s) => s.trim()).filter(Boolean)
      for (const s of candidates) {
        const el = document.querySelector(s) as HTMLElement | null
        if (el) {
          return {
            matched_selector: s,
            element_tag: el.tagName,
            final_value: el.innerText ?? el.textContent ?? '',
          }
        }
      }
      return null
    },
    args: [selector],
  })

  if (!readRes.result) {
    return { ok: false, error: 'paste_prompt: post-insert read failed' }
  }

  return {
    ok: true,
    data: {
      ...readRes.result,
      path: 'cdp-insertText',
    },
  }
}

async function rpcTriggerGeneration(
  tab: chrome.tabs.Tab,
  args: Record<string, unknown>,
): Promise<unknown> {
  if (tab.id === undefined) throw new Error('tab has no id')
  const rawSelectors = args.selectors
  if (!Array.isArray(rawSelectors) || rawSelectors.length === 0) {
    throw new Error('args.selectors (non-empty array) required')
  }
  const selectors = rawSelectors as SelectorSpec[]
  const opts = {
    ensure_visible: args.ensure_visible !== false,
    ensure_enabled: args.ensure_enabled !== false,
  }
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: 'MAIN',
    func: pageworldClickFirstMatching,
    args: [selectors, opts],
  })
  return result
}

async function rpcTakeScreenshot(tab: chrome.tabs.Tab): Promise<unknown> {
  if (tab.windowId === undefined) throw new Error('tab has no windowId')
  const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, {
    format: 'jpeg',
    quality: 60,
  })
  return {
    tab_id: tab.id,
    url: tab.url,
    data_url: dataUrl,
    bytes: dataUrl.length,
  }
}

async function rpcAttachImage(
  tab: chrome.tabs.Tab,
  args: Record<string, unknown>,
): Promise<unknown> {
  if (tab.id === undefined) throw new Error('tab has no id')
  const imageUrl = args.image_url as string | undefined
  const selector = (args.selector as string | undefined) ?? 'input[type="file"]'
  const filename = (args.filename as string | undefined) ?? 'image.png'
  if (!imageUrl) throw new Error('args.image_url required')

  // SW fetches the image (host_permissions <all_urls> avoids CORS),
  // then hands the bytes to page-world as base64. We can't ship a
  // Blob across chrome.scripting — args must be JSON-serializable.
  const res = await fetch(imageUrl)
  if (!res.ok) throw new Error(`fetch ${imageUrl} → ${res.status} ${res.statusText}`)
  const buf = await res.arrayBuffer()
  const bytes = new Uint8Array(buf)
  // btoa caps at ~64K-ish ASCII chunks before stack issues; chunked.
  let bin = ''
  const CHUNK = 0x8000
  for (let i = 0; i < bytes.length; i += CHUNK) {
    bin += String.fromCharCode.apply(null, Array.from(bytes.subarray(i, i + CHUNK)))
  }
  const base64 = btoa(bin)
  const mime = res.headers.get('Content-Type') || 'image/png'

  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: 'MAIN',
    func: pageworldAttachFile,
    args: [selector, base64, mime, filename],
  })
  return result
}

async function rpcScrapeCandidates(
  tab: chrome.tabs.Tab,
  args: Record<string, unknown>,
): Promise<unknown> {
  if (tab.id === undefined) throw new Error('tab has no id')
  const containerSel =
    (args.container_selector as string | undefined) ??
    'div[data-testid="virtuoso-item-list"]'
  const srcPattern =
    (args.src_pattern as string | undefined) ?? 'media\\.getMediaUrlRedirect'

  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: 'MAIN',
    func: pageworldScrapeCandidates,
    args: [containerSel, srcPattern],
  })
  return result
}

async function rpcWaitRoundComplete(
  tab: chrome.tabs.Tab,
  args: Record<string, unknown>,
): Promise<unknown> {
  if (tab.id === undefined) throw new Error('tab has no id')
  const opts = {
    container_selector:
      (args.container_selector as string | undefined) ??
      'div[data-testid="virtuoso-item-list"]',
    src_pattern: (args.src_pattern as string | undefined) ?? 'media\\.getMediaUrlRedirect',
    baseline_srcs: (args.baseline_srcs as string[] | undefined) ?? [],
    expected_count: (args.expected_count as number | undefined) ?? 1,
    timeout_sec: (args.timeout_sec as number | undefined) ?? 300,
    stability_window_sec: (args.stability_window_sec as number | undefined) ?? 30,
    poll_interval_ms: (args.poll_interval_ms as number | undefined) ?? 1000,
  }
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: 'MAIN',
    func: pageworldWaitCandidates,
    args: [opts],
  })
  return result
}

async function rpcDownloadVideo(
  _tab: chrome.tabs.Tab,
  args: Record<string, unknown>,
): Promise<unknown> {
  const url = args.url as string | undefined
  const filename = args.filename as string | undefined
  const conflict = (args.conflict_action as string | undefined) ?? 'uniquify'
  if (!url) throw new Error('args.url required')
  if (!filename) throw new Error('args.filename required')

  const downloadId = await chrome.downloads.download({
    url,
    filename,
    conflictAction: conflict as chrome.downloads.FilenameConflictAction,
    saveAs: false,
  })

  return await new Promise<unknown>((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.downloads.onChanged.removeListener(listener)
      reject(new Error('download timeout (90s)'))
    }, 90_000)
    const listener = (delta: chrome.downloads.DownloadDelta) => {
      if (delta.id !== downloadId) return
      const state = delta.state?.current
      if (state === 'complete') {
        clearTimeout(timer)
        chrome.downloads.onChanged.removeListener(listener)
        chrome.downloads.search({ id: downloadId }, (items) => {
          const item = items[0]
          resolve({
            id: downloadId,
            filename: item?.filename,
            file_size: item?.fileSize,
            url: item?.url ?? url,
            mime: item?.mime,
          })
        })
      } else if (state === 'interrupted') {
        clearTimeout(timer)
        chrome.downloads.onChanged.removeListener(listener)
        reject(new Error(`download interrupted: ${delta.error?.current ?? 'unknown'}`))
      }
    }
    chrome.downloads.onChanged.addListener(listener)
  })
}

async function dispatchRpc(req: RpcRequest): Promise<void> {
  const startedAt = nowMs()
  const sendResult = (ok: boolean, payload?: unknown, error?: string) => {
    ws?.send({
      type: 'rpc_result',
      rpc_id: req.rpc_id,
      ok,
      payload,
      error,
      ts: nowMs(),
    })
  }
  const timeoutMs = (req.timeout_sec ?? DEFAULT_RPC_TIMEOUT_MS / 1000) * 1000

  let timer: ReturnType<typeof setTimeout> | null = null
  const timeoutPromise = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(() => reject(new Error(`rpc timeout after ${timeoutMs}ms`)), timeoutMs)
  })

  try {
    const tab = await ensureFlowTab(req.target_url)
    let payload: unknown
    if (req.method === 'read_page_state') {
      payload = await Promise.race([rpcReadPageState(tab), timeoutPromise])
    } else if (req.method === 'paste_prompt') {
      payload = await Promise.race([rpcPastePrompt(tab, req.args), timeoutPromise])
    } else if (req.method === 'take_screenshot') {
      payload = await Promise.race([rpcTakeScreenshot(tab), timeoutPromise])
    } else if (req.method === 'trigger_generation') {
      payload = await Promise.race([rpcTriggerGeneration(tab, req.args), timeoutPromise])
    } else if (req.method === 'attach_image') {
      payload = await Promise.race([rpcAttachImage(tab, req.args), timeoutPromise])
    } else if (req.method === 'scrape_candidates') {
      payload = await Promise.race([rpcScrapeCandidates(tab, req.args), timeoutPromise])
    } else if (req.method === 'wait_round_complete') {
      payload = await Promise.race([rpcWaitRoundComplete(tab, req.args), timeoutPromise])
    } else if (req.method === 'download_video') {
      payload = await Promise.race([rpcDownloadVideo(tab, req.args), timeoutPromise])
    } else {
      sendResult(false, undefined, `unknown rpc method: ${(req as RpcRequest).method}`)
      return
    }
    const elapsed = nowMs() - startedAt
    console.log(`[bg] rpc ${req.method} ok in ${elapsed}ms`, req.rpc_id)
    sendResult(true, payload)
  } catch (e) {
    console.warn('[bg] rpc failed', req.method, e)
    sendResult(false, undefined, (e as Error).message)
  } finally {
    if (timer !== null) clearTimeout(timer)
  }
}

/* --------------------------------------------------------------------- */
/* WS lifecycle                                                           */
/* --------------------------------------------------------------------- */

function attachWsHandlers(client: WsClient): void {
  // Register on every (re)connect — the real WebSocket `open` event is
  // the trigger, not a setTimeout poll. MV3 SWs drop pending timeouts on
  // hibernate, so the old polling path could never reliably finish a
  // register handshake before the next cold_boot blew the connection
  // away (visible in spike-extension.log as accept → replaced →
  // disconnected loops with no register logs in between).
  client.onOpen(() => sendRegister())

  client.onMessage((msg) => {
    if (msg.type === 'register_ack') {
      console.log('[bg] register_ack', msg.assigned_ws_id)
      return
    }
    if (msg.type === 'ping') {
      client.send({ type: 'heartbeat', ts: nowMs() })
      return
    }
    if (msg.type === 'rpc') {
      void dispatchRpc(msg)
      return
    }
  })
}

// MV3 SW frequently fires cold_boot + onInstalled / onStartup nearly
// simultaneously. Without a lock both invocations race past the
// ``ws?.isOpen()`` guard (the WebSocket hasn't reached OPEN state yet),
// each tear down the other's freshly-created ws, and you see two
// ``accepted → disconnected`` pairs in spike-extension.log within a
// couple of milliseconds.
let bootInFlight: Promise<void> | null = null

async function bootOrRebind(reason: string): Promise<void> {
  if (bootInFlight) {
    console.log(`[bg] bootOrRebind (${reason}) waiting on in-flight boot`)
    await bootInFlight
    return
  }
  const promise = (async () => {
    const newId = await getOrCreateWsId()
    if (currentWsId === newId && ws?.isOpen()) return
    console.log(`[bg] bootOrRebind (${reason}) → ws_id=${newId}`)
    if (ws) ws.close()
    currentWsId = newId
    ws = new WsClient(newId)
    attachWsHandlers(ws)
    ws.connect()
  })()
  bootInFlight = promise
  try {
    await promise
  } finally {
    if (bootInFlight === promise) bootInFlight = null
  }
}

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== 'local') return
  const change = changes[STORAGE_KEY_WS_ID]
  if (!change) return
  const next = change.newValue
  if (typeof next === 'string' && next.length > 0 && next !== currentWsId) {
    void bootOrRebind('storage_changed')
  }
})

chrome.tabs.onRemoved.addListener((tabId) => {
  if (tabId === activeFlowTabId) {
    console.log('[bg] active flow tab closed', tabId)
    activeFlowTabId = null
  }
})

chrome.alarms.create(HEARTBEAT_ALARM, { periodInMinutes: HEARTBEAT_PERIOD_MIN })
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== HEARTBEAT_ALARM) return
  if (!ws || !ws.isOpen()) {
    void bootOrRebind('alarm_revive')
    return
  }
  ws.send({ type: 'heartbeat', ts: nowMs() })
})

void bootOrRebind('cold_boot')
chrome.runtime.onStartup.addListener(() => void bootOrRebind('runtime_onStartup'))
chrome.runtime.onInstalled.addListener(() => void bootOrRebind('runtime_onInstalled'))
