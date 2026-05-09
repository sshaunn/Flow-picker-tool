// DOM-side helpers for RPC handlers.
//
// These functions are NOT registered as auto-match content scripts
// (design v0.6 §4.2 / C-062 — content scripts that inject on every Flow
// page load would taint the operator's daily browsing). Instead, the
// SW lazy-injects them via chrome.scripting.executeScript({ world:
// 'MAIN', func }) only when an RPC is dispatched.
//
// Each exported function must be self-contained — it ends up serialised
// by chrome.scripting and re-evaluated inside the page world. No imports
// from other modules survive serialisation.

/** Result envelope returned by every page-world helper. */
export type DomResult<T> =
  | { ok: true; data: T }
  | { ok: false; error: string; detail?: unknown }

/**
 * SelectorSpec — chrome-native equivalent of V1's playwright
 * `button:has-text("arrow_forward"):has-text("Create")` syntax. The
 * page-world helper resolves `css`, then keeps only elements whose
 * `innerText` contains every string in `contains_all_text`.
 *
 * Strings are accepted as a degenerate form (raw CSS, no text filter).
 */
export type SelectorSpec =
  | string
  | { css: string; contains_all_text?: string[]; aria_label_includes?: string }

/* --------------------------------------------------------------------- */
/* read_page_state                                                       */
/* --------------------------------------------------------------------- */

export function pageworldReadPageState(): DomResult<{
  url: string
  title: string
  ready_state: string
  body_text_snippet: string
  body_text_length: number
  ts: number
}> {
  try {
    const text = document.body?.innerText ?? ''
    return {
      ok: true,
      data: {
        url: window.location.href,
        title: document.title,
        ready_state: document.readyState,
        body_text_snippet: text.slice(0, 5000),
        body_text_length: text.length,
        ts: Date.now(),
      },
    }
  } catch (e) {
    return { ok: false, error: (e as Error).message }
  }
}

/* --------------------------------------------------------------------- */
/* paste_prompt                                                          */
/* --------------------------------------------------------------------- */

export function pageworldPastePrompt(
  selector: string,
  value: string,
): DomResult<{ matched_selector: string; final_value: string; element_tag: string; path: string }> {
  // Two paths:
  //   - HTMLTextAreaElement / HTMLInputElement → React value setter
  //     (defends against Grammarly's input listener, C-029)
  //   - contenteditable / role=textbox (V1's `[role="textbox"]
  //     [data-slate-editor="true"]` Slate.js editor) → focus + select
  //     all + beforeinput InputEvent + execCommand insertText fallback.
  //     Slate listens to beforeinput; the execCommand fallback covers
  //     editors that ignore synthetic InputEvents.
  // IME defense (C-052): also dispatch compositionend for textarea path.
  try {
    const candidates = selector
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean)
    let el: Element | null = null
    let matched = ''
    for (const sel of candidates) {
      const found = document.querySelector(sel)
      if (found) {
        el = found
        matched = sel
        break
      }
    }
    if (!el) {
      return { ok: false, error: `no element matched any of: ${selector}` }
    }

    if (el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement) {
      const proto = el instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set
      if (!setter) return { ok: false, error: 'value setter unavailable' }
      el.focus()
      setter.call(el, value)
      el.dispatchEvent(new Event('input', { bubbles: true }))
      el.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: value }))
      return {
        ok: true,
        data: {
          matched_selector: matched,
          final_value: el.value,
          element_tag: el.tagName,
          path: 'value-setter',
        },
      }
    }

    const html = el as HTMLElement
    const isCE = html.isContentEditable || html.getAttribute('role') === 'textbox'
    if (!isCE) {
      return {
        ok: false,
        error: `unsupported element: tag=${el.tagName} role=${html.getAttribute('role')}`,
      }
    }

    html.focus()
    // Select existing contents so insertText replaces (vs append).
    const sel2 = document.getSelection()
    if (sel2) {
      const range = document.createRange()
      range.selectNodeContents(html)
      sel2.removeAllRanges()
      sel2.addRange(range)
    }

    let path = 'beforeinput'
    const ev = new InputEvent('beforeinput', {
      bubbles: true,
      cancelable: true,
      inputType: 'insertText',
      data: value,
    })
    const accepted = html.dispatchEvent(ev)
    // If the editor didn't preventDefault → it isn't honoring our synthetic
    // event; fall back to execCommand which Slate.js does honor.
    if (accepted) {
      try {
        // execCommand is deprecated but Slate still listens to it.
        const ok = document.execCommand('insertText', false, value)
        if (!ok) path = 'beforeinput-only'
        else path = 'execCommand'
      } catch {
        path = 'beforeinput-only'
      }
    }

    // Read back what landed.
    const finalText = html.innerText ?? html.textContent ?? ''
    return {
      ok: true,
      data: {
        matched_selector: matched,
        final_value: finalText,
        element_tag: el.tagName,
        path,
      },
    }
  } catch (e) {
    return { ok: false, error: (e as Error).message }
  }
}

/* --------------------------------------------------------------------- */
/* trigger_generation (click first matching selector)                    */
/* --------------------------------------------------------------------- */

export function pageworldClickFirstMatching(
  specs: SelectorSpec[],
  opts: { ensure_visible?: boolean; ensure_enabled?: boolean },
): DomResult<{
  matched_index: number
  matched_css: string
  matched_texts: string[]
  element_tag: string
  aria_label: string | null
  text_snippet: string
  candidates_seen: number
  visible_button_dump: Array<{ tag: string; text: string; aria: string | null }>
}> {
  // V1 fragility 9 / 11 / 15: must support multilingual button labels +
  // dump visible buttons on miss so the operator can see the page state.
  // Uses element.click() (not synthetic dispatch) — chrome page-world
  // click goes through the real React handler tree, which is what V1
  // worked out the hard way.
  try {
    const ensureVisible = opts.ensure_visible ?? true
    const ensureEnabled = opts.ensure_enabled ?? true

    const isVisible = (el: Element): boolean => {
      const r = (el as HTMLElement).getBoundingClientRect()
      if (r.width === 0 || r.height === 0) return false
      const cs = getComputedStyle(el as HTMLElement)
      if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return false
      return true
    }
    const isEnabled = (el: Element): boolean => {
      if (el instanceof HTMLButtonElement || el instanceof HTMLInputElement) return !el.disabled
      const aria = (el as HTMLElement).getAttribute('aria-disabled')
      return aria !== 'true'
    }

    let candidatesSeen = 0
    for (let i = 0; i < specs.length; i++) {
      const spec = specs[i]
      const css = typeof spec === 'string' ? spec : spec.css
      const requiredTexts = (typeof spec === 'string' ? [] : spec.contains_all_text ?? []).filter(Boolean)
      const requiredAria = typeof spec === 'string' ? '' : (spec.aria_label_includes ?? '')

      let matched: HTMLElement | null = null
      for (const el of document.querySelectorAll(css)) {
        candidatesSeen++
        const text = (el as HTMLElement).innerText ?? ''
        if (requiredTexts.length && !requiredTexts.every((t) => text.includes(t))) continue
        if (requiredAria) {
          const aria = (el as HTMLElement).getAttribute('aria-label') ?? ''
          if (!aria.includes(requiredAria)) continue
        }
        if (ensureVisible && !isVisible(el)) continue
        if (ensureEnabled && !isEnabled(el)) continue
        matched = el as HTMLElement
        break
      }

      if (matched) {
        const tag = matched.tagName
        const aria = matched.getAttribute('aria-label')
        const textSnippet = (matched.innerText ?? '').slice(0, 200)
        try {
          matched.click()
        } catch (e) {
          return { ok: false, error: `click() threw: ${(e as Error).message}` }
        }
        return {
          ok: true,
          data: {
            matched_index: i,
            matched_css: css,
            matched_texts: requiredTexts,
            element_tag: tag,
            aria_label: aria,
            text_snippet: textSnippet,
            candidates_seen: candidatesSeen,
            visible_button_dump: [],
          },
        }
      }
    }

    // None matched — dump visible buttons so operator can see what's on
    // screen (V1 v0.0.4 forensic helper, ported into the negative-path
    // payload).
    const dump: Array<{ tag: string; text: string; aria: string | null }> = []
    document.querySelectorAll('button, [role="button"]').forEach((el) => {
      if (dump.length >= 30) return
      if (!isVisible(el)) return
      dump.push({
        tag: el.tagName,
        text: ((el as HTMLElement).innerText ?? '').trim().slice(0, 80),
        aria: (el as HTMLElement).getAttribute('aria-label'),
      })
    })
    return {
      ok: false,
      error: `no element matched any of ${specs.length} selector(s); ${candidatesSeen} candidate(s) inspected`,
      detail: { visible_button_dump: dump, candidates_seen: candidatesSeen },
    }
  } catch (e) {
    return { ok: false, error: (e as Error).message }
  }
}

/* --------------------------------------------------------------------- */
/* attach_image (DataTransfer → file input)                              */
/* --------------------------------------------------------------------- */

export function pageworldAttachFile(
  selector: string,
  base64: string,
  mime: string,
  filename: string,
): DomResult<{
  matched_selector: string
  files_count: number
  filename: string
  size: number
}> {
  // V1 fragility 9 + C-029 (1Password autofill defense). chrome
  // page-world DataTransfer + dispatchEvent is the only reliable way
  // to set <input type=file> value programmatically.
  try {
    const input = document.querySelector(selector) as HTMLInputElement | null
    if (!input) return { ok: false, error: `no element matched: ${selector}` }
    if (!(input instanceof HTMLInputElement) || input.type !== 'file') {
      return { ok: false, error: `not a file input: ${selector} (type=${input.type})` }
    }
    // base64 → Uint8Array → Blob → File (page-world; SW hands us
    // bytes via chrome.scripting args because SW workers can't make
    // blob URLs).
    const bin = atob(base64)
    const arr = new Uint8Array(bin.length)
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i)
    const blob = new Blob([arr], { type: mime })
    const file = new File([blob], filename, { type: mime })
    const dt = new DataTransfer()
    dt.items.add(file)
    input.files = dt.files
    input.dispatchEvent(new Event('change', { bubbles: true }))
    return {
      ok: true,
      data: {
        matched_selector: selector,
        files_count: input.files.length,
        filename: file.name,
        size: file.size,
      },
    }
  } catch (e) {
    return { ok: false, error: (e as Error).message }
  }
}

/* --------------------------------------------------------------------- */
/* scrape_candidates (snapshot current Flow video src list)              */
/* --------------------------------------------------------------------- */

export function pageworldScrapeCandidates(
  containerSelector: string,
  srcPattern: string,
): DomResult<{ srcs: string[]; total_videos: number; ts: number }> {
  // V1 candidate detection: video.src that matches
  // `media\.getMediaUrlRedirect`. Returns the full set so the
  // caller can diff against a baseline (V2 keeps that diff in
  // python center for now).
  try {
    const re = new RegExp(srcPattern)
    const videos = document.querySelectorAll(`${containerSelector} video`)
    const srcs: string[] = []
    videos.forEach((v) => {
      const src = (v as HTMLVideoElement).src
      if (src && re.test(src)) srcs.push(src)
    })
    return { ok: true, data: { srcs, total_videos: videos.length, ts: Date.now() } }
  } catch (e) {
    return { ok: false, error: (e as Error).message }
  }
}

/* --------------------------------------------------------------------- */
/* wait_round_complete (poll for new video src + stability window)       */
/* --------------------------------------------------------------------- */

export async function pageworldWaitCandidates(
  opts: {
    container_selector: string
    src_pattern: string
    baseline_srcs: string[]
    expected_count: number
    timeout_sec: number
    stability_window_sec: number
    poll_interval_ms: number
  },
): Promise<
  DomResult<{
    new_srcs: string[]
    early_exit: boolean
    elapsed_ms: number
    timed_out: boolean
    polls: number
  }>
> {
  // V1 wait_for_round_complete (flow_playwright.py:1567) condensed:
  //   - collect candidate <video> srcs that match the tRPC redirect
  //     pattern (`media.getMediaUrlRedirect?name=<UUID>`)
  //   - subtract the caller-supplied baseline (snapshot taken BEFORE
  //     trigger_generation)
  //   - early-exit when new.size >= expected_count AND the set has
  //     been stable for stability_window_sec
  //   - timeout returns whatever was collected so far
  // Stability window matters because Flow mounts each x4 candidate
  // serially over ~20-40s; without it we'd return after the first
  // mp4 and miss the rest.
  try {
    const re = new RegExp(opts.src_pattern)
    const baseline = new Set(opts.baseline_srcs)
    const newSrcs = new Set<string>()
    let lastChange = 0
    let polls = 0
    const start = Date.now()
    const deadline = start + opts.timeout_sec * 1000

    const collect = (): boolean => {
      let changed = false
      const videos = document.querySelectorAll(`${opts.container_selector} video`)
      videos.forEach((v) => {
        const src = (v as HTMLVideoElement).src
        if (!src || !re.test(src)) return
        if (baseline.has(src)) return
        if (newSrcs.has(src)) return
        newSrcs.add(src)
        changed = true
      })
      if (changed) lastChange = Date.now()
      return changed
    }

    while (Date.now() < deadline) {
      polls++
      collect()
      const stableEnoughMs =
        lastChange > 0 ? Date.now() - lastChange : Number.NEGATIVE_INFINITY
      if (
        newSrcs.size >= opts.expected_count &&
        stableEnoughMs >= opts.stability_window_sec * 1000
      ) {
        return {
          ok: true,
          data: {
            new_srcs: [...newSrcs],
            early_exit: true,
            elapsed_ms: Date.now() - start,
            timed_out: false,
            polls,
          },
        }
      }
      await new Promise((r) => setTimeout(r, opts.poll_interval_ms))
    }
    return {
      ok: true,
      data: {
        new_srcs: [...newSrcs],
        early_exit: false,
        elapsed_ms: Date.now() - start,
        timed_out: true,
        polls,
      },
    }
  } catch (e) {
    return { ok: false, error: (e as Error).message }
  }
}
