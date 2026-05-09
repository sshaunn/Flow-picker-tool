// Spike popup — read/edit ws_id (per-profile) + ping Co-Pilot for liveness.
// The SW owns the WS connection; we just nudge chrome.storage and let the
// SW's onChanged listener trigger the reconnect.

const COPILOT_HEALTH_URL = 'http://127.0.0.1:8080/healthz'
const STORAGE_KEY_WS_ID = 'ws_id'

const wsIdEl = document.getElementById('ws-id') as HTMLSpanElement
const statusEl = document.getElementById('status') as HTMLSpanElement
const hbEl = document.getElementById('hb') as HTMLSpanElement
const inputEl = document.getElementById('ws-id-input') as HTMLInputElement
const saveBtn = document.getElementById('ws-id-save') as HTMLButtonElement
const savedMsg = document.getElementById('saved-msg') as HTMLDivElement

async function loadWsId(): Promise<void> {
  const stored = await chrome.storage.local.get(STORAGE_KEY_WS_ID)
  const id = stored[STORAGE_KEY_WS_ID]
  if (typeof id === 'string' && id.length) {
    wsIdEl.textContent = id
    if (!inputEl.value) inputEl.value = id
  } else {
    wsIdEl.textContent = '尚未生成（等 service worker 启动）'
  }
}

async function saveWsId(): Promise<void> {
  const v = inputEl.value.trim()
  if (!v) {
    savedMsg.style.color = '#b91c1c'
    savedMsg.textContent = '不能为空'
    return
  }
  saveBtn.disabled = true
  await chrome.storage.local.set({ [STORAGE_KEY_WS_ID]: v })
  wsIdEl.textContent = v
  savedMsg.style.color = '#047857'
  savedMsg.textContent = `已保存：${v}（service worker 正在重连）`
  setTimeout(() => {
    saveBtn.disabled = false
    savedMsg.textContent = ''
  }, 3000)
}

async function refreshHealth(): Promise<void> {
  try {
    const res = await fetch(COPILOT_HEALTH_URL, { cache: 'no-store' })
    if (res.ok) {
      statusEl.textContent = 'online'
      statusEl.className = 'pill online'
    } else {
      statusEl.textContent = `co-pilot ${res.status}`
      statusEl.className = 'pill offline'
    }
  } catch {
    statusEl.textContent = 'co-pilot unreachable'
    statusEl.className = 'pill offline'
  }
  hbEl.textContent = new Date().toLocaleTimeString()
}

// Listen for storage changes so the popup mirrors what the SW writes
// (auto-generated first-run id arriving after popup is already open).
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== 'local') return
  if (changes[STORAGE_KEY_WS_ID]) {
    void loadWsId()
  }
})

saveBtn.addEventListener('click', () => void saveWsId())
inputEl.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') void saveWsId()
})

void loadWsId()
void refreshHealth()
setInterval(refreshHealth, 5000)
