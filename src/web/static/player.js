/* DVR player: one EVENT manifest gives both the live edge and the whole history.
 *
 * The latency readout leans on frag.programDateTime — real in hls.js sources but
 * absent from its API docs, so every read is guarded and the UI degrades to "—"
 * rather than breaking if a future release drops it.
 */

const LIVE_EDGE_OFFSET_S = 12;  // how far behind the newest segment playback sits
const LIVE_THRESHOLD_S = 20;    // within this of the edge still counts as "в эфире"

// A camera started from the page needs a few segments before its manifest exists.
const MANIFEST_WARMUP_TRIES = 20;
const MANIFEST_WARMUP_DELAY_MS = 3000;

/** Wall-clock lag of the played position, in seconds. Pure. */
export function latencySeconds(anchor, currentTime, now) {
  if (!anchor) return null;
  const playedAt = anchor.pdt + (currentTime - anchor.start) * 1000;
  return (now - playedAt) / 1000;
}

/** How the position relates to the live edge. Pure. */
export function liveState(behindSeconds, threshold = LIVE_THRESHOLD_S) {
  if (behindSeconds === null || !Number.isFinite(behindSeconds)) return { live: false, text: "—" };
  if (behindSeconds <= threshold) return { live: true, text: "в эфире" };
  return { live: false, text: `отстал на ${Math.round(behindSeconds)} с` };
}

function fmt(seconds) {
  if (seconds === null || !Number.isFinite(seconds)) return "—";
  return `${seconds.toFixed(1)} с`;
}

/** Human text for the two failures a misconfigured bucket actually produces. */
function errorText(data, url) {
  const d = data.details || "";
  if (d === "manifestLoadError" || d === "manifestLoadTimeOut") {
    const code = data.response && data.response.code;
    if (code === 404) return `манифест не найден: ${url}`;
    if (code === 403) return `доступ к манифесту запрещён (403) — публичный доступ к бакету выключен?`;
    return `манифест не загрузился — проверьте CORS на бакете и адрес: ${url}`;
  }
  if (d === "manifestParsingError") return "манифест загрузился, но не разбирается как HLS";
  if (data.type === "networkError") return `сетевая ошибка при загрузке: ${d}`;
  return `ошибка воспроизведения: ${d || data.type}`;
}

/** Everything needed to tell a stale manifest from a mis-seeked player. */
function describe(hls, video, anchor, lag, edge) {
  const d = hls.levels?.[hls.currentLevel]?.details;
  const s = video.seekable;
  const iso = (ms) => (ms ? new Date(ms).toISOString().slice(11, 23) : "—");
  const n = (x, k = 1) => (Number.isFinite(x) ? x.toFixed(k) : String(x));
  return [
    `фрагментов: ${d?.fragments?.length ?? "—"}  live: ${d?.live}  тип: ${d?.type}`,
    `PDT первого: ${iso(d?.fragments?.[0]?.programDateTime)}  последнего: ${iso(d?.fragments?.at(-1)?.programDateTime)}`,
    `currentTime: ${n(video.currentTime)}  liveSyncPosition: ${n(hls.liveSyncPosition)}  edge: ${n(edge)}`,
    `seekable: ${s.length ? `${n(s.start(0))}…${n(s.end(s.length - 1))}` : "пусто"}  duration: ${n(video.duration)}`,
    `якорь PDT: ${iso(anchor?.pdt)} @ start ${n(anchor?.start)}  задержка: ${n(lag)} с`,
    `paused: ${video.paused}  readyState: ${video.readyState}  now: ${iso(Date.now())}`,
  ].join("\n");
}

export function startPlayer(manifestUrl, ui) {
  const { video, latencyEl, stateEl, liveBtn, onError, debugEl } = ui;

  if (!window.Hls || !window.Hls.isSupported()) {
    onError("этот браузер не поддерживает Media Source Extensions");
    return null;
  }

  // Native HLS is deliberately not used even where it exists: its behaviour on
  // seeking back through an EVENT playlist is unreliable.
  const hls = new window.Hls({
    liveDurationInfinity: true,   // seekbar spans the whole session, not a window
    backBufferLength: 900,        // memory cap only; the seek range comes from the playlist
    lowLatencyMode: false,

    // Distance from the live edge, in seconds. The default is three *target
    // durations*, and EXT-X-TARGETDURATION is the longest segment ever produced —
    // in an EVENT playlist it only ever grows. One overlong segment (a reconnect
    // stretched one to 37 s here) would therefore pin the live edge two minutes
    // back for the rest of the session. Fixing it in seconds is immune to that.
    liveSyncDuration: LIVE_EDGE_OFFSET_S,
  });

  let anchor = null;

  hls.on(window.Hls.Events.FRAG_CHANGED, (_e, data) => {
    const pdt = data.frag && data.frag.programDateTime;
    if (typeof pdt === "number" && Number.isFinite(pdt)) {
      anchor = { pdt, start: data.frag.start };
    }
  });

  let warmupLeft = MANIFEST_WARMUP_TRIES;

  hls.on(window.Hls.Events.ERROR, (_e, data) => {
    if (!data.fatal) return;
    if (data.type === window.Hls.ErrorTypes.MEDIA_ERROR) {
      hls.recoverMediaError();
      return;
    }
    // A camera just handed in on the page has no manifest until its first
    // segments reach the bucket, so an early 404 means "not yet", not "wrong".
    const missing = data.details === "manifestLoadError"
      && data.response && data.response.code === 404;
    if (missing && warmupLeft-- > 0) {
      stateEl.textContent = "запускается…";
      setTimeout(() => hls.loadSource(manifestUrl), MANIFEST_WARMUP_DELAY_MS);
      return;
    }
    if (data.type === window.Hls.ErrorTypes.NETWORK_ERROR && data.details !== "manifestLoadError") {
      hls.startLoad();
      return;
    }
    onError(errorText(data, manifestUrl));
    hls.destroy();
  });

  function edgePosition() {
    if (Number.isFinite(hls.liveSyncPosition)) return hls.liveSyncPosition;
    const s = video.seekable;
    return s.length ? s.end(s.length - 1) : null;
  }

  function tick() {
    const lag = latencySeconds(anchor, video.currentTime, Date.now());
    latencyEl.textContent = fmt(lag);

    const edge = edgePosition();
    const behind = edge === null ? null : edge - video.currentTime;
    const st = liveState(behind);
    stateEl.textContent = st.text;
    stateEl.classList.toggle("live", st.live);
    liveBtn.disabled = st.live;

    if (debugEl) debugEl.textContent = describe(hls, video, anchor, lag, edge);
  }

  liveBtn.addEventListener("click", () => {
    const edge = edgePosition();
    if (edge !== null) {
      video.currentTime = edge;
      video.play().catch(() => {});
    }
  });

  hls.loadSource(manifestUrl);
  hls.attachMedia(video);
  hls.on(window.Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {}));

  setInterval(tick, 500);
  return hls;
}
