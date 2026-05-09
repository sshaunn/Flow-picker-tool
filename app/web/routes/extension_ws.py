"""V2 extension spike — WebSocket endpoint + minimal dashboard page.

Mounted only when ``FLOW_HARVESTER_SPIKE_EXTENSION=1``; without that,
``server.py`` does not include this router and V1 production behaviour
is untouched.

Architecture (design v0.6 §4.5 simplified per operator constraint):

* Extension is a thin DOM I/O bridge implementing FlowPort-equivalent
  RPC methods (``read_page_state`` / ``paste_prompt`` / ``take_screenshot``).
  See ``extension/src/lib/protocol.ts``.
* Center owns task lifecycle; this module only proxies RPC requests
  from operator → extension and tracks pending replies via per-rpc
  asyncio Futures.
* No persistence. State is wiped on process restart.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import APIRouter, Form, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse


router = APIRouter(tags=["spike-extension"], include_in_schema=False)

log = logging.getLogger("flow_harvester.spike.extension_ws")

# Hard cap so a stuck extension can't deadlock the dashboard. Per-RPC
# soft timeouts can shorten this; this is the floor.
RPC_DEFAULT_TIMEOUT_SEC = 60.0
RPC_MAX_TIMEOUT_SEC = 180.0

# Server-driven keepalive. MV3 service workers are killed after 30 s
# without inbound activity, regardless of how many outbound messages or
# chrome.alarms the extension itself fires. The only reliable way to
# keep the SW alive is for the server to push a message at a steady
# cadence < 30 s — every received frame resets the SW idle timer.
KEEPALIVE_INTERVAL_SEC = 20.0

VALID_RPC_METHODS = {
    "read_page_state",
    "paste_prompt",
    "take_screenshot",
    "trigger_generation",
    "attach_image",
    "scrape_candidates",
    "wait_round_complete",
    "download_video",
}

# V1 candidate detection (config/flow-selectors.yaml). chrome can use
# these directly — the container CSS is already locale-free, and the
# tRPC redirect URL pattern is what V1's PlaywrightFlowPort filters on.
SPIKE_VIRTUOSO_CONTAINER = 'div[data-testid="virtuoso-item-list"]'
SPIKE_CANDIDATE_SRC_PATTERN = r"media\.getMediaUrlRedirect"


# Spike default — translated from config/flow-selectors.yaml's
# V1 playwright `:has-text("X"):has-text("Y")` syntax into the
# extension's structured SelectorSpec (chrome can't run :has-text).
# All entries match `<button>` plus a list of substrings the element's
# innerText must contain. "arrow_forward" is a Material Symbols icon
# ligature → locale-free; the locale-specific labels are fallbacks.
SPIKE_DEFAULT_GENERATE_SELECTORS = [
    {"css": "button", "contains_all_text": ["arrow_forward"]},
    {"css": "button", "contains_all_text": ["arrow_forward", "Create"]},
    {"css": "button", "contains_all_text": ["arrow_forward", "创建"]},
    {"css": "button", "contains_all_text": ["arrow_forward", "建立"]},
    {"css": "button", "contains_all_text": ["arrow_forward", "Tạo"]},
    {"css": "button", "contains_all_text": ["arrow_forward", "สร้าง"]},
    {"css": "button", "contains_all_text": ["arrow_forward", "Buat"]},
    {"css": "button", "contains_all_text": ["arrow_forward", "Cipta"]},
    {"css": "button", "contains_all_text": ["arrow_forward", "Gumawa"]},
    {"css": "button", "contains_all_text": ["arrow_forward", "ဖန်တီး"]},
    {"css": "button", "contains_all_text": ["arrow_forward", "បង្កើត"]},
    {"css": "button", "contains_all_text": ["arrow_forward", "ສ້າງ"]},
    {"css": "button", "contains_all_text": ["arrow_forward", "作成"]},
    {"css": "button", "contains_all_text": ["arrow_forward", "만들기"]},
]


@dataclass
class ConnectedWorkstation:
    ws_id: str
    socket: WebSocket
    extension_version: str = ""
    chrome_version: str = ""
    connected_at: float = field(default_factory=time.time)
    last_heartbeat_at: float = field(default_factory=time.time)
    last_screenshot_data_url: Optional[str] = None
    last_screenshot_url: Optional[str] = None
    last_screenshot_at: Optional[float] = None
    last_rpc_method: Optional[str] = None
    last_rpc_at: Optional[float] = None
    last_rpc_ok: Optional[bool] = None
    last_rpc_summary: Optional[str] = None
    pending_rpc: dict[str, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)


_REGISTRY: dict[str, ConnectedWorkstation] = {}


@router.websocket("/ws/extension/{ws_id}")
async def extension_ws(socket: WebSocket, ws_id: str) -> None:
    await socket.accept()
    log.info("[extension_ws] %s accepted", ws_id)

    existing = _REGISTRY.pop(ws_id, None)
    if existing is not None:
        # Cancel any pending RPC the previous connection still owed us.
        for fut in list(existing.pending_rpc.values()):
            if not fut.done():
                fut.set_exception(RuntimeError("extension reconnected; previous rpc abandoned"))
        existing.pending_rpc.clear()
        try:
            await existing.socket.close()
        except Exception:  # noqa: BLE001 — best effort
            pass
        log.info("[extension_ws] %s replaced previous connection", ws_id)

    entry = ConnectedWorkstation(ws_id=ws_id, socket=socket)
    _REGISTRY[ws_id] = entry

    keepalive_task = asyncio.create_task(_keepalive_loop(entry))

    try:
        await socket.send_text(json.dumps({"type": "register_ack", "assigned_ws_id": ws_id}))
        while True:
            raw = await socket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError as e:
                log.warning("[extension_ws] %s bad json: %s", ws_id, e)
                continue
            await _handle_message(entry, msg)
    except WebSocketDisconnect:
        log.info("[extension_ws] %s disconnected", ws_id)
    except Exception as e:  # noqa: BLE001
        log.exception("[extension_ws] %s error: %s", ws_id, e)
    finally:
        keepalive_task.cancel()
        if _REGISTRY.get(ws_id) is entry:
            del _REGISTRY[ws_id]
        for fut in list(entry.pending_rpc.values()):
            if not fut.done():
                fut.set_exception(RuntimeError("extension disconnected"))


async def _keepalive_loop(entry: ConnectedWorkstation) -> None:
    """Push a ``ping`` every ``KEEPALIVE_INTERVAL_SEC`` so MV3 chrome
    sees inbound traffic and resets the service-worker idle timer.

    Without this, the SW gets killed after 30 s of inbound silence even
    while it's holding an open WebSocket — observed as ``register …``
    followed ~11 s later by ``disconnected`` in spike-extension.log,
    with subsequent RPC calls failing because ``_REGISTRY`` is empty
    by the time the V1 worker dispatches.

    Cancelled by the parent handler's ``finally`` when the socket goes
    away (operator closed chrome, network blip, etc.).
    """
    try:
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL_SEC)
            try:
                await entry.socket.send_text(json.dumps({"type": "ping", "ts": time.time()}))
            except Exception as e:  # noqa: BLE001
                log.debug("[extension_ws] %s keepalive send failed: %s", entry.ws_id, e)
                return
    except asyncio.CancelledError:
        pass


async def _handle_message(entry: ConnectedWorkstation, msg: dict[str, Any]) -> None:
    msg_type = msg.get("type")
    now = time.time()

    if msg_type == "register":
        entry.extension_version = str(msg.get("extension_version") or "")
        entry.chrome_version = str(msg.get("chrome_version") or "")
        entry.last_heartbeat_at = now
        log.info(
            "[extension_ws] register ws_id=%s ext=%s chrome=%s",
            entry.ws_id,
            entry.extension_version,
            entry.chrome_version,
        )
        await entry.socket.send_text(json.dumps({"type": "register_ack", "assigned_ws_id": entry.ws_id}))
        return

    if msg_type == "heartbeat":
        entry.last_heartbeat_at = now
        return

    if msg_type == "rpc_result":
        rpc_id = str(msg.get("rpc_id") or "")
        fut = entry.pending_rpc.pop(rpc_id, None)
        if fut is None:
            log.warning("[extension_ws] %s rpc_result for unknown id %s", entry.ws_id, rpc_id)
            return
        if not fut.done():
            fut.set_result(msg)
        return

    if msg_type == "log":
        level = str(msg.get("level") or "info").lower()
        message = msg.get("message")
        log_fn = {
            "debug": log.debug,
            "info": log.info,
            "warn": log.warning,
            "warning": log.warning,
            "error": log.error,
        }.get(level, log.info)
        log_fn("[extension_ws][ext-log] %s: %s", entry.ws_id, message)
        return

    log.debug("[extension_ws] %s unknown message type: %s", entry.ws_id, msg_type)


# -----------------------------------------------------------------------------
# RPC dispatcher (used by HTTP endpoints below)
# -----------------------------------------------------------------------------


async def call_rpc(
    *,
    ws_id: str,
    method: str,
    target_url: Optional[str] = None,
    args: Optional[dict[str, Any]] = None,
    timeout_sec: float = RPC_DEFAULT_TIMEOUT_SEC,
) -> dict[str, Any]:
    if method not in VALID_RPC_METHODS:
        raise ValueError(f"unknown method: {method}")
    timeout_sec = max(1.0, min(timeout_sec, RPC_MAX_TIMEOUT_SEC))
    entry = _REGISTRY.get(ws_id)
    if entry is None:
        connected = sorted(_REGISTRY.keys())
        if connected:
            hint = f"; connected ws_ids = {connected} (rename via the extension popup to match)"
        else:
            hint = (
                "; no extensions are connected — check (1) FLOW_HARVESTER_SPIKE_EXTENSION=1, "
                "(2) chrome has the unpacked extension/dist loaded, "
                "(3) the extension popup shows online + correct ws_id"
            )
        raise RuntimeError(f"workstation {ws_id} not connected{hint}")

    rpc_id = uuid.uuid4().hex
    fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    entry.pending_rpc[rpc_id] = fut

    payload: dict[str, Any] = {
        "type": "rpc",
        "rpc_id": rpc_id,
        "method": method,
        "args": args or {},
        "timeout_sec": timeout_sec,
    }
    if target_url:
        payload["target_url"] = target_url

    try:
        await entry.socket.send_text(json.dumps(payload))
    except Exception as e:
        entry.pending_rpc.pop(rpc_id, None)
        raise RuntimeError(f"send failed: {e}") from e

    try:
        result = await asyncio.wait_for(fut, timeout=timeout_sec + 5.0)
    except asyncio.TimeoutError:
        entry.pending_rpc.pop(rpc_id, None)
        raise RuntimeError(f"rpc timeout after {timeout_sec}s")

    entry.last_rpc_method = method
    entry.last_rpc_at = time.time()
    entry.last_rpc_ok = bool(result.get("ok"))
    if entry.last_rpc_ok:
        entry.last_rpc_summary = _summarise_rpc_payload(method, result.get("payload"))
        # Convenience: stash the most recent screenshot for the dashboard.
        if method == "take_screenshot" and isinstance(result.get("payload"), dict):
            data_url = result["payload"].get("data_url")
            if isinstance(data_url, str):
                entry.last_screenshot_data_url = data_url
                entry.last_screenshot_url = result["payload"].get("url")
                entry.last_screenshot_at = entry.last_rpc_at
    else:
        entry.last_rpc_summary = f"error: {result.get('error')}"
    return result


def _summarise_rpc_payload(method: str, payload: Any) -> str:
    if method == "read_page_state" and isinstance(payload, dict):
        data = payload.get("data") if isinstance(payload.get("data"), dict) else None
        if data is None:
            return f"ok={payload.get('ok')} error={payload.get('error')}"
        return f"url={data.get('url')!r} body_len={data.get('body_text_length')}"
    if method == "paste_prompt" and isinstance(payload, dict):
        data = payload.get("data") if isinstance(payload.get("data"), dict) else None
        if data is None:
            return f"ok={payload.get('ok')} error={payload.get('error')}"
        return (
            f"matched={data.get('matched_selector')!r} "
            f"final_value_len={len(str(data.get('final_value') or ''))}"
        )
    if method == "take_screenshot" and isinstance(payload, dict):
        return f"bytes={payload.get('bytes')} url={payload.get('url')!r}"
    return ""


# -----------------------------------------------------------------------------
# Operator-facing spike page
# -----------------------------------------------------------------------------


_SPIKE_PAGE_HTML = """\
<!doctype html>
<html lang="zh">
<head>
  <meta charset="utf-8">
  <title>V2 Extension Spike</title>
  <style>
    body { font-family: -apple-system, system-ui, sans-serif; padding: 24px; max-width: 980px; margin: auto; color: #1f2937; }
    h1 { margin-top: 0; }
    h2 { margin-bottom: 4px; }
    .card { border: 1px solid #e5e7eb; border-radius: 8px; padding: 16px; margin-bottom: 16px; }
    .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; }
    .pill.online { background: #d1fae5; color: #047857; }
    .pill.stale { background: #fee2e2; color: #b91c1c; }
    .pill.ok { background: #dbeafe; color: #1e40af; }
    .pill.err { background: #fee2e2; color: #b91c1c; }
    table { width: 100%; border-collapse: collapse; margin-bottom: 8px; }
    th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #f3f4f6; font-size: 13px; vertical-align: top; }
    code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; background: #f3f4f6; padding: 2px 4px; border-radius: 3px; }
    .rpc-form { background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 6px; padding: 10px 12px; margin-top: 10px; }
    .rpc-form h3 { margin: 0 0 6px; font-size: 13px; }
    .rpc-form .row { display: flex; gap: 6px; align-items: center; margin-bottom: 4px; }
    .rpc-form input { flex: 1; padding: 4px 6px; border: 1px solid #d1d5db; border-radius: 4px; font-family: ui-monospace, monospace; font-size: 12px; }
    .rpc-form button { padding: 5px 10px; border: 0; border-radius: 4px; background: #2563eb; color: white; font-size: 12px; cursor: pointer; }
    .rpc-form button:disabled { background: #9ca3af; }
    .rpc-result { margin-top: 6px; font-family: ui-monospace, monospace; font-size: 11px; max-height: 220px; overflow: auto; background: #1f2937; color: #e5e7eb; padding: 8px; border-radius: 4px; white-space: pre-wrap; }
    img.shot { max-width: 100%; border: 1px solid #e5e7eb; border-radius: 6px; margin-top: 8px; }
    .muted { color: #6b7280; font-size: 12px; }
  </style>
</head>
<body>
  <h1>V2 Extension Spike — RPC</h1>
  <p class="muted">
    Spike feature flag is ON. WS endpoint:
    <code>ws://127.0.0.1:8080/ws/extension/&lt;ws_id&gt;</code>.
    扩展只是 DOM I/O 桥；本页用 RPC 单步调用扩展端 FlowPort 方法验证可行性。
  </p>
  <div id="root"></div>

  <script>
    const root = document.getElementById('root');
    // refresh() rewrites root.innerHTML. While an RPC is mid-flight or
    // its result is still on screen, freezing the rebuild keeps the
    // form (and its input values + result panel) intact.
    let inflightRpc = 0;
    let frozenUntilMs = 0;
    const RESULT_VISIBLE_MS = 8000;

    async function refresh() {
      if (inflightRpc > 0 || Date.now() < frozenUntilMs) return;
      let payload;
      try {
        const r = await fetch('/spike/extension/state');
        payload = await r.json();
      } catch (e) {
        root.innerHTML = '<div class="card">无法获取状态: ' + e + '</div>';
        return;
      }
      if (!payload.connected.length) {
        root.innerHTML = '<div class="card">尚无扩展连接。在 chrome 装 <code>extension/dist</code>，每个 profile 一份。</div>';
        return;
      }
      root.innerHTML = payload.connected.map(renderWs).join('');
    }
    function renderWs(ws) {
      const stale = (Date.now()/1000) - ws.last_heartbeat_at > 60;
      let lastRpc = '';
      if (ws.last_rpc_method) {
        const cls = ws.last_rpc_ok ? 'ok' : 'err';
        lastRpc = '<span class="pill ' + cls + '">' + escapeHtml(ws.last_rpc_method) + '</span> '
                + '<span class="muted">' + escapeHtml(ws.last_rpc_summary || '') + '</span>';
      }
      return [
        '<div class="card">',
        '<h2>' + escapeHtml(ws.ws_id) + ' <span class="pill ' + (stale ? 'stale' : 'online') + '">' + (stale ? 'stale' : 'online') + '</span></h2>',
        '<table>',
        row('extension_version', ws.extension_version || '—'),
        row('chrome_version', ws.chrome_version || '—'),
        row('last_heartbeat_at', fmtTs(ws.last_heartbeat_at)),
        row('last_rpc', lastRpc || '—'),
        '</table>',
        rpcForm(ws.ws_id, 'read_page_state', [
          ['target_url', 'https://labs.google/fx/tools/flow', 'text'],
        ]),
        rpcForm(ws.ws_id, 'paste_prompt', [
          ['target_url', '', 'text'],
          ['args.selector', 'textarea', 'text'],
          ['args.prompt', 'hello from V2 spike', 'text'],
        ]),
        rpcForm(ws.ws_id, 'take_screenshot', [
          ['target_url', '', 'text'],
        ]),
        rpcForm(ws.ws_id, 'trigger_generation', [
          ['target_url', '', 'text'],
          ['args.selectors_json', JSON.stringify(__DEFAULT_GEN_SELECTORS__), 'text'],
        ]),
        rpcForm(ws.ws_id, 'attach_image', [
          ['target_url', '', 'text'],
          ['args.image_url', 'http://127.0.0.1:8080/files/<path-to-image>', 'text'],
          ['args.selector', 'input[type="file"]', 'text'],
          ['args.filename', 'frame.png', 'text'],
        ]),
        rpcForm(ws.ws_id, 'scrape_candidates', [
          ['target_url', '', 'text'],
          ['args.container_selector', '__VIRTUOSO_CONTAINER__', 'text'],
          ['args.src_pattern', '__CANDIDATE_SRC_PATTERN__', 'text'],
        ]),
        rpcForm(ws.ws_id, 'wait_round_complete', [
          ['target_url', '', 'text'],
          ['args.container_selector', '__VIRTUOSO_CONTAINER__', 'text'],
          ['args.src_pattern', '__CANDIDATE_SRC_PATTERN__', 'text'],
          ['args.baseline_srcs_json', '[]', 'text'],
          ['args.expected_count', '4', 'number'],
          ['args.timeout_sec', '300', 'number'],
          ['args.stability_window_sec', '30', 'number'],
          ['args.poll_interval_ms', '1000', 'number'],
        ], { rpc_timeout_sec: 320 }),
        rpcForm(ws.ws_id, 'download_video', [
          ['target_url', '', 'text'],
          ['args.url', '', 'text'],
          ['args.filename', 'FlowHarvester/output_v2/spike/' + ws.ws_id + '_' + Date.now() + '.mp4', 'text'],
          ['args.conflict_action', 'uniquify', 'text'],
        ], { rpc_timeout_sec: 120 }),
        ws.last_screenshot_data_url ? ('<p class="muted">最近一次 take_screenshot:</p><img class="shot" src="' + ws.last_screenshot_data_url + '" />') : '',
        '</div>',
      ].join('');
    }
    function row(k, v) { return '<tr><th style="width:200px">' + k + '</th><td>' + v + '</td></tr>'; }
    function fmtTs(t) { if (!t) return '—'; return new Date(t*1000).toLocaleString(); }
    function escapeHtml(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]); }
    function rpcForm(ws_id, method, fields, opts) {
      opts = opts || {};
      const inputs = fields.map(([name, def, type]) =>
        '<div class="row"><span class="muted" style="width:170px">' + escapeHtml(name) + '</span>'
        + '<input type="' + type + '" data-name="' + escapeHtml(name) + '" data-input-type="' + escapeHtml(type) + '" value="' + escapeHtml(def) + '" /></div>'
      ).join('');
      const timeoutAttr = opts.rpc_timeout_sec ? ' data-rpc-timeout="' + Number(opts.rpc_timeout_sec) + '"' : '';
      return [
        '<div class="rpc-form" data-ws="' + escapeHtml(ws_id) + '" data-method="' + escapeHtml(method) + '"' + timeoutAttr + '>',
          '<h3>RPC <code>' + escapeHtml(method) + '</code>' + (opts.rpc_timeout_sec ? ' <span class="muted">(timeout ' + opts.rpc_timeout_sec + 's)</span>' : '') + '</h3>',
          inputs,
          '<div class="row"><button onclick="callRpc(this)">派发</button></div>',
          '<div class="rpc-result" hidden></div>',
        '</div>',
      ].join('');
    }
    async function callRpc(btn) {
      const form = btn.closest('.rpc-form');
      const wsId = form.dataset.ws;
      const method = form.dataset.method;
      const inputs = form.querySelectorAll('input[data-name]');
      const body = new URLSearchParams();
      body.set('ws_id', wsId);
      body.set('method', method);
      const args = {};
      let target_url = '';
      for (const inp of inputs) {
        const name = inp.dataset.name;
        const v = inp.value;
        if (name === 'target_url') { if (v) target_url = v; continue; }
        if (name.startsWith('args.')) {
          // Convention: input named "args.foo_json" is JSON-decoded into args.foo
          if (name.endsWith('_json')) {
            const key = name.slice(5, -5);
            try {
              args[key] = JSON.parse(v);
            } catch (e) {
              const out = form.querySelector('.rpc-result');
              out.hidden = false;
              out.textContent = `invalid JSON in ${name}: ${e}`;
              return;
            }
          } else if (inp.dataset.inputType === 'number') {
            const n = parseFloat(v);
            args[name.slice(5)] = Number.isFinite(n) ? n : v;
          } else {
            args[name.slice(5)] = v;
          }
        }
      }
      if (target_url) body.set('target_url', target_url);
      body.set('args_json', JSON.stringify(args));
      if (form.dataset.rpcTimeout) body.set('timeout_sec', form.dataset.rpcTimeout);

      const out = form.querySelector('.rpc-result');
      out.hidden = false;
      out.textContent = '... waiting';
      btn.disabled = true;
      inflightRpc++;
      try {
        const r = await fetch('/spike/extension/rpc', {
          method: 'POST',
          headers: { 'content-type': 'application/x-www-form-urlencoded' },
          body: body.toString(),
        });
        const data = await r.json();
        out.textContent = JSON.stringify(data, null, 2);
      } catch (e) {
        out.textContent = 'ERROR: ' + e;
      } finally {
        inflightRpc--;
        // Keep the rebuild paused for a few seconds so the user can
        // actually read the JSON payload before the form refreshes.
        frozenUntilMs = Date.now() + RESULT_VISIBLE_MS;
        btn.disabled = false;
      }
    }
    // While the operator hovers over a result panel, keep extending
    // the freeze so they can scroll/copy without the form vanishing.
    document.body.addEventListener('mouseover', (e) => {
      if (e.target && e.target.classList && e.target.classList.contains('rpc-result')) {
        frozenUntilMs = Math.max(frozenUntilMs, Date.now() + 5000);
      }
    });

    refresh();
    setInterval(refresh, 2000);
  </script>
</body>
</html>
"""


@router.get("/spike/extension", response_class=HTMLResponse, include_in_schema=False)
async def spike_extension_page(_request: Request) -> HTMLResponse:
    html = _SPIKE_PAGE_HTML
    html = html.replace(
        "__DEFAULT_GEN_SELECTORS__",
        json.dumps(SPIKE_DEFAULT_GENERATE_SELECTORS, ensure_ascii=False),
    )
    # Bare strings — replaced inside JS string literals; no extra quotes.
    html = html.replace("__VIRTUOSO_CONTAINER__", SPIKE_VIRTUOSO_CONTAINER)
    html = html.replace("__CANDIDATE_SRC_PATTERN__", SPIKE_CANDIDATE_SRC_PATTERN)
    return HTMLResponse(html)


@router.get("/spike/extension/state", include_in_schema=False)
async def spike_extension_state() -> JSONResponse:
    connected = []
    for ws_id, entry in _REGISTRY.items():
        connected.append(
            {
                "ws_id": ws_id,
                "extension_version": entry.extension_version,
                "chrome_version": entry.chrome_version,
                "connected_at": entry.connected_at,
                "last_heartbeat_at": entry.last_heartbeat_at,
                "last_screenshot_url": entry.last_screenshot_url,
                "last_screenshot_at": entry.last_screenshot_at,
                "last_screenshot_data_url": entry.last_screenshot_data_url,
                "last_rpc_method": entry.last_rpc_method,
                "last_rpc_at": entry.last_rpc_at,
                "last_rpc_ok": entry.last_rpc_ok,
                "last_rpc_summary": entry.last_rpc_summary,
            }
        )
    return JSONResponse({"connected": connected})


@router.get("/spike/extension/file", include_in_schema=False)
async def spike_extension_file(abs: str = Query(..., min_length=1)) -> FileResponse:
    """Serve a local file by absolute path so the extension SW can fetch it.

    Used by ``ExtensionFlowPort.upload_source_assets`` to hand a local
    asset path off to chrome (the extension can't reach into the
    Co-Pilot's filesystem directly). Spike-only — production V2 should
    move to V1's existing ``/files/<rel>`` route + a per-task assets
    subtree.

    Path-traversal mitigation: must be absolute, must exist, must point
    inside one of the allowed roots:
      * the dev cwd (project repo)
      * V1's app_data_dir (``~/Library/Application Support/FlowHarvester``
        on macOS) — task assets live there once V1 has copied them.
    Anything else 403s.
    """
    from pathlib import Path as _P
    from app import paths as _ap

    p = _P(abs).resolve()
    if not p.is_absolute() or not p.exists() or not p.is_file():
        return JSONResponse({"error": "file not found"}, status_code=404)

    allowed_roots: list[_P] = [_P.cwd().resolve()]
    try:
        allowed_roots.append(_ap.app_data_dir().resolve())
    except Exception:  # noqa: BLE001 — fall back to cwd-only
        pass

    if not any(_path_inside(p, root) for root in allowed_roots):
        return JSONResponse(
            {"error": "path outside workspace + app_data_dir", "allowed": [str(r) for r in allowed_roots]},
            status_code=403,
        )
    return FileResponse(p)


def _path_inside(child, root) -> bool:
    try:
        child.relative_to(root)
        return True
    except ValueError:
        return False


@router.post("/spike/extension/rpc", include_in_schema=False)
async def spike_extension_rpc(
    ws_id: str = Form(...),
    method: str = Form(...),
    target_url: Optional[str] = Form(None),
    args_json: str = Form("{}"),
    timeout_sec: float = Form(RPC_DEFAULT_TIMEOUT_SEC),
) -> JSONResponse:
    try:
        args: dict[str, Any] = json.loads(args_json or "{}")
        if not isinstance(args, dict):
            raise ValueError("args_json must decode to an object")
    except (json.JSONDecodeError, ValueError) as e:
        return JSONResponse({"ok": False, "error": f"invalid args_json: {e}"}, status_code=400)

    try:
        result = await call_rpc(
            ws_id=ws_id,
            method=method,
            target_url=target_url,
            args=args,
            timeout_sec=timeout_sec,
        )
    except RuntimeError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    return JSONResponse({"ok": True, "rpc_result": result})
