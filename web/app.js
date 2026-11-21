/* TikTok Research & Phone Studio — Giao diện Web độc lập tối ưu. */
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtSize = (bytes) => !bytes ? '—' : bytes > 1073741824 ? `${(bytes / 1073741824).toFixed(1)} GB` : `${(bytes / 1048576).toFixed(1)} MB`;
const fmtTime = (ts) => ts ? new Date(ts * 1000).toLocaleTimeString('vi-VN') : '';
const enc = encodeURIComponent;

const state = {
  health: null,
  devices: [],
  selected: localStorage.getItem('trp.selected') || '',
  streams: {},
  research: { products: [], criteria: {}, steps: [] },
  researchSelected: new Set(),
  autoCapture: true,
  autoPasteId: localStorage.getItem('trp.autoPasteId') !== '0',
  stepsCollapsed: localStorage.getItem('trp.stepsCollapsed') === '1',
  logs: [],
  unreadLogs: 0,
  deviceFilter: 'all',
  miniMultiOpen: false,
  previewWide: false,
  leftWidth: Number(localStorage.getItem('trp.leftWidth')) || 252,
  rightWidth: Number(localStorage.getItem('trp.rightWidth')) || 460,
  sidebarCollapsed: localStorage.getItem('trp.sidebarCollapsed') === '1',
  phoneFitMode: localStorage.getItem('trp.phoneFitMode') || 'contain',
  contextSerial: '',
  lastProductId: '',
  lastPhoneClip: '',
  lan: [],
  booted: false,
};

/* ------------------------------------------------------------------ API */
async function api(path, { method = 'GET', body, raw = false } = {}) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  if (raw) {
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).error || response.statusText);
    return response;
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Lỗi ${response.status}`);
  return data;
}

function toast(message, kind = '') {
  const node = document.createElement('div');
  node.className = `toast ${kind}`;
  node.style.whiteSpace = 'pre-line';
  node.textContent = message;
  $('#toasts')?.append(node);
  setTimeout(() => node.remove(), kind === 'err' ? 7000 : 3800);
}

async function guard(button, task) {
  if (button) button.disabled = true;
  try {
    return await task();
  } catch (error) {
    toast(error.message, 'err');
    return undefined;
  } finally {
    if (button) button.disabled = false;
  }
}

const device = () => state.devices.find((d) => d.serial === state.selected) || null;
const ready = (d = device()) => !!d && d.state === 'device';

async function copyTextSafe(text) {
  if (!text) return false;
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.left = '-9999px';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      return true;
    } catch {
      return false;
    }
  }
}

/* ---------------------------------------------------------- WebSocket */
let ws = null;
let wsRetry = 0;

function connectWs() {
  ws = new WebSocket(`ws://${location.host}/ws/screen`);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    wsRetry = 0;
    if (state.selected && screen.mode === 'live') {
      wsSend({ type: 'resync', serial: state.selected });
    }
  };
  ws.onclose = () => { setTimeout(connectWs, Math.min(5000, 500 * 2 ** wsRetry++)); };
  ws.onmessage = (event) => {
    if (typeof event.data !== 'string') {
      screen.onBinary(event.data);
      return;
    }
    let message;
    try { message = JSON.parse(event.data); } catch { return; }
    handleWs(message);
  };
}

function wsSend(message) {
  if (ws && ws.readyState === 1) {
    ws.send(JSON.stringify(message));
    return true;
  }
  return false;
}

function handleWs(m) {
  switch (m.type) {
    case 'device-update': {
      const before = new Set(state.devices.map((d) => d.serial));
      const devices = m.devices || [];
      if (state.booted) {
        devices.filter((d) => !before.has(d.serial)).forEach((d) => {
          toast(`📱 Đã kết nối ${d.label || d.serial}${d.state === 'unauthorized' ? ' — bấm “Cho phép” trên điện thoại' : ''}`, 'ok');
        });
      }
      setDevices(devices);
      break;
    }
    case 'bluestacks-update':
      renderBsOptions(m.instances);
      break;
    case 'wireless-discovered':
      renderLan(m.services);
      break;
    case 'wireless-qr':
      if ($('#dlg-qr')?.open) renderQrStatus(m);
      break;
    case 'auto-capture-state':
      state.autoCapture = !!m.enabled;
      renderAutoCaptureChip();
      break;
    case 'stream-state':
      state.streams = Object.fromEntries((m.streams || []).map((s) => [s.serial, s]));
      screen.syncMode();
      renderDevices();
      break;
    case 'stream-config':
      if (m.serial === state.selected) screen.codec = m.codec;
      break;
    case 'stream-size':
      if (state.streams[m.serial]) Object.assign(state.streams[m.serial], { width: m.width, height: m.height });
      if (m.serial === state.selected) screen.setSize(m.width, m.height);
      break;
    case 'stream-ended':
      delete state.streams[m.serial];
      if (m.serial === state.selected && screen.mode === 'live') {
        screen.preferShot = true;
        screen.setMode('shot');
        toast(m.message || 'Stream đã dừng. Đã chuyển sang Ảnh chụp.', 'info');
      }
      break;
    case 'stream-log':
      appendLog({ at: Date.now() / 1000, message: `[${m.serial}] scrcpy: ${m.message}` });
      break;
    case 'product-auto-captured': {
      const p = m.product || {};
      const title = p.title || p.id || 'Sản phẩm mới';
      toast(`✨ [Phone] Đã bắt link: ${title}`, 'ok');
      loadResearch(p.id, 'new');
      break;
    }
    case 'product-duplicate-detected': {
      const p = m.product || {};
      const title = p.title || p.id || 'Sản phẩm';
      toast(`⚠️ [Trùng lặp] Sản phẩm "${title}" đã có trong danh sách!`, 'warn');
      loadResearch(p.id || m.productId, 'duplicate');
      break;
    }
    case 'video-pushed': {
      if (m.productId) {
        setPhoneProductIdStrip(m.productId);
      }
      break;
    }
    case 'video-product-id-copied': {
      if (m.productId) {
        setPhoneProductIdStrip(m.productId);
      }
      break;
    }
    case 'device-clipboard-synced': {
      state.lastPhoneClip = m.text;
      const short = (m.text.length > 55) ? m.text.slice(0, 52) + '…' : m.text;
      toast(`📋 Đã đồng bộ từ Phone: ${short}`, 'ok');
      const addInput = $('#r-add-url');
      if ($('#dlg-research-add')?.open && addInput && !addInput.value) {
        addInput.value = m.text;
        addInput.dispatchEvent(new Event('input'));
      }
      const bulkInput = $('#bulk-paste-text');
      if ($('#dlg-research-bulk')?.open && bulkInput && !bulkInput.value) {
        bulkInput.value = m.text;
      }
      break;
    }
    case 'research-update': {
      if (m.products) {
        state.research = { ...(state.research || {}), products: m.products };
        renderResearch(m.highlightId || null, 'new');
      } else {
        loadResearch();
      }
      break;
    }
    case 'research-fetch-progress': {
      const b = m.batch || {};
      const bar = $('#research-fetch-progress-bar');
      const text = $('#research-progress-text');
      const count = $('#research-progress-count');
      const fill = $('#research-progress-fill');
      if (bar) {
        if (b.running) {
          bar.hidden = false;
          if (text) text.textContent = b.message || 'Đang tải thông tin sản phẩm (Cách 1 - SPAI Auto)...';
          if (count) count.textContent = `${b.current} / ${b.total}`;
          const pct = b.total > 0 ? Math.round((b.current / b.total) * 100) : 0;
          if (fill) fill.style.width = `${pct}%`;
        } else {
          if (b.message) toast(`⚡ ${b.message}`, (b.failed > 0 && b.success === 0) ? 'warn' : 'ok');
          setTimeout(() => { if (bar) bar.hidden = true; }, 2500);
        }
      }
      break;
    }
    case 'log':
      appendLog(m.entry);
      break;
    case 'error':
    case 'control-error':
      toast(m.message, 'err');
      break;
    default:
      break;
  }
}

/* ------------------------------------------------------------ Screen */
const screen = {
  canvas: $('#screen-canvas'),
  img: $('#screen-img'),
  ctx: null,
  decoder: null,
  codec: 'h264',
  config: null,
  waitingKey: true,
  width: 0,
  height: 0,
  mode: 'none',
  frames: 0,
  shotTimer: null,
  shotBusy: false,
  pointer: null,
  pendingMove: null,
  moving: false,
  startingStream: false,
  lastFrameAt: 0,
  decodeErrors: 0,
  liveStartedAt: 0,
  preferShot: false,

  init() {
    this.ctx = this.canvas.getContext('2d');
    for (const el of [this.canvas, this.img]) {
      el.addEventListener('pointerdown', (e) => this.down(e));
      el.addEventListener('pointermove', (e) => this.move(e));
      el.addEventListener('pointerup', (e) => this.up(e));
      el.addEventListener('pointercancel', (e) => this.up(e));
      el.addEventListener('contextmenu', (e) => { e.preventDefault(); this.key(4); });
    }
    this.canvas.addEventListener('wheel', (e) => this.wheel(e), { passive: false });
    setInterval(() => this.watchdog(), 1000);
  },

  overlay(message, spinner = true) {
    const ov = $('#screen-overlay');
    if (!ov) return;
    ov.hidden = !message;
    const msgEl = $('#screen-msg');
    if (msgEl) msgEl.textContent = message || '';
    const sp = $('.spinner', ov);
    if (sp) sp.style.display = spinner ? '' : 'none';
  },

  reset() {
    try { this.decoder?.close(); } catch { /* ignore */ }
    this.decoder = null;
    this.config = null;
    this.waitingKey = true;
    this.frames = 0;
    this.decodeErrors = 0;
  },

  setSize(w, h) {
    if (!w || !h) return;
    this.width = w;
    this.height = h;
    const phone = $('#phone');
    if (phone) phone.style.aspectRatio = `${w} / ${h}`;
    updatePhoneStageSize();
  },

  syncMode() {
    const d = device();
    const live = d && state.streams[d.serial];
    const streamBtn = $('#btn-stream-toggle');
    if (streamBtn) {
      streamBtn.innerHTML = `<span class="dock-ico">${live ? '⏹' : '▶️'}</span><span class="dock-lbl">${live ? 'Dừng Live' : 'Bật Live'}</span>`;
    }
    if (!d) {
      this.setMode('none');
      this.overlay('Chưa chọn thiết bị', false);
      return;
    }
    if (!ready(d)) {
      this.setMode('none');
      this.overlay(d.state === 'unauthorized' ? 'Hãy bấm "Cho phép gỡ lỗi USB" trên điện thoại' : `Thiết bị ${d.state}`, false);
      return;
    }
    if (live && 'VideoDecoder' in window && !this.preferShot) {
      if (this.mode !== 'live') {
        this.setMode('live');
        this.reset();
        this.liveStartedAt = Date.now();
        if (live.width) this.setSize(live.width, live.height);
        this.overlay('Đang nhận hình realtime…');
        wsSend({ type: 'resync', serial: d.serial });
      }
    } else {
      if (ready(d) && 'VideoDecoder' in window && !this.startingStream && !this.preferShot) {
        this.startingStream = true;
        this.overlay('Đang kết nối màn hình realtime…');
        api('/api/stream/start', { method: 'POST', body: { serial: d.serial } })
          .then((res) => {
            if (res?.streams) {
              state.streams = Object.fromEntries(res.streams.map((s) => [s.serial, s]));
              this.syncMode();
            }
          })
          .catch(() => { })
          .finally(() => { this.startingStream = false; });
      }
      this.setMode('shot');
    }
  },

  setMode(mode) {
    if (this.mode === mode) return;
    this.mode = mode;
    clearTimeout(this.shotTimer);
    this.canvas.hidden = mode === 'shot';
    this.img.hidden = mode !== 'shot';
    const chip = $('#screen-mode');
    if (chip) {
      chip.className = `chip ${mode === 'live' ? 'live' : mode === 'shot' ? 'warn' : ''}`;
      chip.textContent = mode === 'live' ? 'Realtime' : mode === 'shot' ? 'Ảnh chụp' : '—';
    }
    if (mode === 'shot') {
      this.overlay('Đang chụp màn hình…');
      this.pollShot();
    }
    if (mode !== 'live') this.reset();
  },

  async pollShot() {
    if (this.mode !== 'shot' || !ready()) return;
    const serial = state.selected;
    if (!this.shotBusy && !document.hidden) {
      this.shotBusy = true;
      try {
        const response = await api(`/api/devices/${enc(serial)}/screenshot?t=${Date.now()}`, { raw: true });
        const blob = await response.blob();
        if (serial === state.selected && this.mode === 'shot') {
          const url = URL.createObjectURL(blob);
          this.img.onload = () => {
            URL.revokeObjectURL(url);
            this.setSize(this.img.naturalWidth, this.img.naturalHeight);
            this.overlay('');
          };
          this.img.src = url;
        }
      } catch (error) {
        this.overlay(`Không chụp được màn hình: ${error.message}`, false);
      } finally {
        this.shotBusy = false;
      }
    }
    this.shotTimer = setTimeout(() => this.pollShot(), 1100);
  },

  pollShotNow() {
    if (this.mode === 'shot') {
      clearTimeout(this.shotTimer);
      this.pollShot();
    }
  },

  watchdog() {
    if (this.mode === 'live' && state.streams[state.selected]) {
      const elapsed = Date.now() - this.lastFrameAt;
      if (this.waitingKey || (elapsed > 1500 && this.frames > 0)) {
        this.lastFrameAt = Date.now();
        wsSend({ type: 'resync', serial: state.selected });
      }
      if (this.frames === 0 && this.liveStartedAt && (Date.now() - this.liveStartedAt > 8000)) {
        toast('Không giải mã được video — đã chuyển sang Ảnh chụp. Bấm chip để thử lại.', 'warn');
        this.preferShot = true;
        this.setMode('shot');
      }
    }
  },

  onBinary(buffer) {
    const u8 = new Uint8Array(buffer);
    if (u8[0] !== 1) return;
    const len = u8[1];
    const serial = new TextDecoder().decode(u8.subarray(2, 2 + len));
    if (serial !== state.selected || this.mode !== 'live') return;
    const offset = 2 + len;
    const flags = u8[offset];
    const view = new DataView(buffer, offset + 1, 8);
    const pts = Number(view.getBigUint64(0) & 0x1FFFFFFFFFFFFFFFn);
    const data = u8.slice(offset + 9);
    if (flags & 0x40) { this.configure(data); return; }
    this.decode(flags & 0x20, pts, data);
  },

  codecString(data) {
    for (let i = 0; i + 4 < data.length; i++) {
      if (data[i] === 0 && data[i + 1] === 0 && data[i + 2] === 1 && (data[i + 3] & 0x1f) === 7) {
        const hex = (b) => b.toString(16).padStart(2, '0');
        return `avc1.${hex(data[i + 4])}${hex(data[i + 5])}${hex(data[i + 6])}`;
      }
    }
    return 'avc1.42e01f';
  },

  configure(data) {
    this.reset();
    this.config = data;
    if (this.codec && !/^h264/i.test(this.codec)) {
      this.overlay(`Codec ${this.codec} chưa hỗ trợ — chuyển sang ảnh chụp`, false);
      this.setMode('shot');
      return;
    }
    try {
      this.decoder = new VideoDecoder({
        output: (frame) => this.draw(frame),
        error: () => {
          this.decodeErrors++;
          if (this.decodeErrors >= 4) {
            toast('Lỗi giải mã video liên tục — đã chuyển sang Ảnh chụp.', 'warn');
            this.preferShot = true;
            this.setMode('shot');
            return;
          }
          this.reset();
          setTimeout(() => wsSend({ type: 'resync', serial: state.selected }), 300);
        },
      });
      this.decoder.configure({ codec: this.codecString(data), optimizeForLatency: true, hardwareAcceleration: 'no-preference' });
      this.waitingKey = true;
    } catch (error) {
      this.overlay(`Trình duyệt không giải mã được H.264: ${error.message}`, false);
      this.setMode('shot');
    }
  },

  decode(isKey, pts, data) {
    if (!this.decoder || this.decoder.state !== 'configured') return;
    if (isKey) {
      this.waitingKey = false;
    } else if (this.waitingKey) {
      return;
    } else if (this.decoder.decodeQueueSize > 25) {
      this.waitingKey = true;
      wsSend({ type: 'resync', serial: state.selected });
      return;
    }
    let payload = data;
    if (isKey && this.config) {
      payload = new Uint8Array(this.config.length + data.length);
      payload.set(this.config, 0);
      payload.set(data, this.config.length);
    }
    try {
      this.decoder.decode(new EncodedVideoChunk({ type: isKey ? 'key' : 'delta', timestamp: pts, data: payload }));
    } catch {
      this.decodeErrors++;
      if (this.decodeErrors >= 4) {
        this.preferShot = true;
        this.setMode('shot');
        return;
      }
      this.waitingKey = true;
      wsSend({ type: 'resync', serial: state.selected });
    }
  },

  draw(frame) {
    const w = frame.displayWidth;
    const h = frame.displayHeight;
    if (this.canvas.width !== w || this.canvas.height !== h) {
      this.canvas.width = w;
      this.canvas.height = h;
      this.setSize(w, h);
    }
    this.ctx.drawImage(frame, 0, 0);
    frame.close();
    this.lastFrameAt = Date.now();
    if (this.frames++ === 0) this.overlay('');
  },

  containedGeometry(el) {
    const iw = el === this.img ? this.img.naturalWidth : this.canvas.width;
    const ih = el === this.img ? this.img.naturalHeight : this.canvas.height;
    if (!iw || !ih) return null;
    const rect = el.getBoundingClientRect();
    const scale = Math.min(rect.width / iw, rect.height / ih);
    if (!Number.isFinite(scale) || scale <= 0) return null;
    const shownWidth = iw * scale;
    const shownHeight = ih * scale;
    const left = rect.left + (rect.width - shownWidth) / 2;
    const top = rect.top + (rect.height - shownHeight) / 2;
    return { left, top, scale, width: shownWidth, height: shownHeight, iw, ih };
  },

  point(e) {
    const geom = this.containedGeometry(e.currentTarget);
    if (!geom) return null;
    const x = Math.round((e.clientX - geom.left) / geom.scale);
    const y = Math.round((e.clientY - geom.top) / geom.scale);
    const clampedX = Math.max(0, Math.min(geom.iw, x));
    const clampedY = Math.max(0, Math.min(geom.ih, y));
    return {
      x: clampedX,
      y: clampedY,
      rx: geom.iw ? clampedX / geom.iw : 0,
      ry: geom.ih ? clampedY / geom.ih : 0,
      width: geom.iw,
      height: geom.ih,
      geom,
    };
  },

  canControl() {
    return ready(device());
  },

  sendTouch(action, pt) {
    if (!pt) return false;
    return wsSend({ type: 'control', serial: state.selected, event: 'touch', action, x: pt.x, y: pt.y, width: pt.width, height: pt.height });
  },

  down(e) {
    if (e.button !== 0) return;
    $('#phone')?.focus({ preventScroll: true });
    const pt = this.point(e);
    if (!pt || !this.canControl()) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    this.pointer = { ...pt, at: Date.now() };
    if (this.mode === 'live') this.sendTouch('down', pt);
  },

  move(e) {
    if (!this.pointer) return;
    const pt = this.point(e);
    if (!pt) return;
    this.pendingMove = pt;
    if (this.moving) return;
    this.moving = true;
    requestAnimationFrame(() => {
      this.moving = false;
      if (this.pendingMove && this.mode === 'live') {
        this.sendTouch('move', this.pendingMove);
      }
      this.pendingMove = null;
    });
  },

  up(e) {
    const pointer = this.pointer;
    this.pointer = null;
    this.pendingMove = null;
    if (!pointer) return;
    const pt = this.point(e) || pointer;
    const dist = Math.hypot((pt.rx - pointer.rx) * pt.geom.width, (pt.ry - pointer.ry) * pt.geom.height);
    if (this.mode === 'live') {
      const sent = this.sendTouch(e.type === 'pointercancel' ? 'cancel' : 'up', pt);
      if (sent) return;
    }
    const body = dist < 10
      ? { action: 'tap', x: pt.x, y: pt.y }
      : { action: 'swipe', x1: pointer.x, y1: pointer.y, x2: pt.x, y2: pt.y, duration: Math.min(2000, Math.max(120, Date.now() - pointer.at)) };
    api(`/api/devices/${enc(state.selected)}/input`, { method: 'POST', body })
      .then(() => setTimeout(() => this.pollShotNow(), 350))
      .catch((err) => toast(err.message, 'err'));
  },

  wheel(e) {
    const wrap = $('.phone-wrap');
    if (state.phoneFitMode === 'width' && wrap && wrap.scrollHeight > wrap.clientHeight + 4 && !e.altKey) {
      return;
    }
    if (this.mode !== 'live' || !this.canControl()) return;
    e.preventDefault();
    const pt = this.point(e);
    if (!pt) return;
    const dy = e.deltaY > 0 ? -0.25 : 0.25;
    this.sendTouch('down', pt);
    let step = 0;
    const timer = setInterval(() => {
      step++;
      const nextY = Math.min(pt.height, Math.max(0, Math.round(pt.y + (dy * pt.height * step) / 6)));
      this.sendTouch(step >= 6 ? 'up' : 'move', { ...pt, y: nextY });
      if (step >= 6) clearInterval(timer);
    }, 16);
  },

  key(keycode) {
    if (!this.canControl()) return;
    if (keycode === 224) {
      api(`/api/devices/${enc(state.selected)}/wakeup`, { method: 'POST' })
        .then(() => toast('☀ Đã gửi lệnh đánh thức màn hình & mở khóa.', 'ok'))
        .catch(() => { });
      return;
    }
    if (![66, 67, 112].includes(keycode) && this.mode === 'live' && wsSend({ type: 'control', serial: state.selected, event: 'keycode', keycode })) {
      return;
    }
    api(`/api/devices/${enc(state.selected)}/input`, { method: 'POST', body: { action: 'key', keycode } })
      .then(() => setTimeout(() => this.pollShotNow(), 350))
      .catch((err) => toast(err.message, 'err'));
  },
};

/* ---------------------------------------------------------- Devices */
function deviceStatus(d) {
  if (d.state === 'unauthorized') return ['warn', 'Chờ cho phép USB'];
  if (d.state === 'offline') return ['bad', 'Offline'];
  if (d.state !== 'device') return ['warn', d.state];
  return ['ok', 'Sẵn sàng'];
}

function formatCleanSerial(serial) {
  if (!serial) return '';
  if (serial.includes('._adb-tls-connect._tcp')) {
    const m = serial.match(/^adb-([^-]+)-/);
    return m ? `Wi-Fi TLS • ${m[1]}` : 'Wi-Fi ADB (TLS)';
  }
  return serial;
}

function setDevices(devices) {
  state.devices = devices || [];
  for (const d of state.devices) {
    if (d.stream && d.stream.running) {
      state.streams[d.serial] = d.stream;
    }
  }
  const readyDevices = state.devices.filter((d) => d.state === 'device');
  const usbDevice = readyDevices.find((d) => d.kind === 'usb');
  const emuDevice = readyDevices.find((d) => d.kind === 'bluestacks' || d.kind === 'emulator');

  const isStillConnected = state.selected && state.devices.some((d) => d.serial === state.selected);
  if (!isStillConnected) {
    const first = usbDevice || emuDevice || readyDevices[0] || state.devices[0];
    if (first && first.serial !== state.selected) {
      selectDevice(first.serial);
      return;
    }
    if (!first) {
      state.selected = '';
    }
  }
  renderDevices();
  renderHeader();
  screen.syncMode();
  if (state.miniMultiOpen) renderMiniMultiGrid();
  renderStudioStatusBar();
}

function selectDevice(serial) {
  if (!serial) return;
  const changed = serial !== state.selected;
  state.selected = serial;
  localStorage.setItem('trp.selected', serial);
  if (changed) {
    clearTimeout(screen.shotTimer);
    screen.mode = 'none';
    screen.preferShot = false;
    screen.reset();
    screen.width = screen.height = 0;
  }
  renderDevices();
  renderHeader();
  screen.syncMode();
  const d = device();
  if (d && ready(d)) {
    api(`/api/devices/${enc(d.serial)}/wakeup`, { method: 'POST' }).catch(() => { });
    loadDeviceImeStatus(d.serial);
    if (!state.streams[d.serial] && 'VideoDecoder' in window) {
      api('/api/stream/start', { method: 'POST', body: { serial: d.serial } }).catch(() => { });
    }
  }
}

function renderDevices() {
  const list = $('#device-list');
  const total = state.devices.length;
  if ($('#device-count')) $('#device-count').textContent = String(total);
  if ($('#ws-collapsed-dev-count')) $('#ws-collapsed-dev-count').textContent = String(total);

  const connectBox = $('#connect-hub-box');
  if (connectBox && connectBox.dataset.userToggled !== '1') {
    connectBox.hidden = total > 0;
  }

  const filter = state.deviceFilter || 'all';
  const filtered = state.devices.filter((d) => {
    if (filter === 'usb') return d.kind === 'usb';
    if (filter === 'wireless') return d.kind === 'wireless';
    if (filter === 'live') return !!state.streams[d.serial];
    return true;
  });

  if ($('#device-empty')) $('#device-empty').hidden = filtered.length > 0;
  if (list) {
    list.innerHTML = filtered.map((d, idx) => {
      const [tone, text] = deviceStatus(d);
      const kind = { bluestacks: '🟩 BlueStacks', wireless: '📶 Wi-Fi', usb: '🔌 USB', emulator: '🖥️ Emulator' }[d.kind] || d.kind;
      const live = state.streams[d.serial];
      const numStr = d.number || String(idx + 1).padStart(2, '0');
      const cleanSer = formatCleanSerial(d.serial);
      const metaLine = [d.profile?.familyLabel || cleanSer, d.profile ? `${d.profile.width}×${d.profile.height}` : ''].filter(Boolean).join(' · ');
      const storage = d.storage?.status === 'READY' ? `<span class="chip ok">💾 ${fmtSize(d.storage.freeBytes)}</span>` : '';
      const groupChip = (d.group && d.group !== 'Mặc định') ? `<span class="chip" style="border-color:rgba(139,92,246,0.4);color:#c4b5fd;">🏷️ ${esc(d.group)}</span>` : '';
      return `<div class="device ${d.serial === state.selected ? 'active' : ''}" data-serial="${esc(d.serial)}" title="${esc(d.label)} (${esc(d.serial)})">
        <div class="name">
          <span class="dev-num-badge">#${esc(numStr)}</span>
          <span class="dot ${tone}"></span>
          <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(d.label)}</span>
        </div>
        <div class="meta">${esc(metaLine)}</div>
        <div class="tags"><span class="chip">${kind}</span><span class="chip ${tone}">${esc(text)}</span>${live ? '<span class="chip live">Live</span>' : ''}${storage || groupChip}</div>
      </div>`;
    }).join('');
  }

  const swBar = $('#preview-switcher-bar');
  if (swBar) swBar.hidden = state.devices.length <= 1;
  const pillsBox = $('#preview-dev-pills');
  if (pillsBox) {
    pillsBox.innerHTML = state.devices.map((d, idx) => {
      const numStr = d.number || String(idx + 1).padStart(2, '0');
      const [tone] = deviceStatus(d);
      return `<button type="button" class="preview-dev-pill ${d.serial === state.selected ? 'active' : ''}" data-select-serial="${esc(d.serial)}" title="${esc(d.label)} (${esc(d.serial)})">
        <span class="dot ${tone}"></span>
        <b>#${esc(numStr)}</b>
        <span>${esc((d.label || d.serial).slice(0, 12))}</span>
      </button>`;
    }).join('');
  }

  renderStudioStatusBar();
}

function renderHeader() {
  const d = device();
  const numStr = d ? (d.number || '01') : '--';
  if ($('#ws-title')) $('#ws-title').textContent = d ? d.label : 'Chọn một điện thoại';
  const wsSub = $('#ws-sub');
  if (wsSub) {
    wsSub.textContent = d
      ? [d.profile?.familyLabel || formatCleanSerial(d.serial), d.profile ? `${d.profile.width}×${d.profile.height}` : '', d.profile?.navigationLabel].filter(Boolean).join(' · ')
      : 'Thiết bị sẵn sàng sẽ tự hiện màn hình realtime bên phải.';
  }

  const modeBadge = $('#ws-mode-badge');
  if (modeBadge) {
    if (!d) {
      modeBadge.textContent = 'CHỜ KẾT NỐI';
    } else if (d.kind === 'usb') {
      modeBadge.textContent = `🔌 USB #${numStr}`;
    } else if (d.kind === 'wireless') {
      modeBadge.textContent = `📶 WI-FI #${numStr}`;
    } else {
      modeBadge.textContent = `📱 MÁY #${numStr}`;
    }
  }

  if ($('#preview-dev-num')) $('#preview-dev-num').textContent = `#${numStr}`;
  if ($('#preview-dev-title')) $('#preview-dev-title').textContent = d ? d.label : 'Màn hình';
  renderStudioStatusBar();
}

function renderStudioStatusBar() {
  const readyCount = state.devices.filter((d) => d.state === 'device').length;
  const totalCount = state.devices.length;
  if ($('#sb-dev-count')) $('#sb-dev-count').textContent = `${readyCount}/${totalCount}`;
  if ($('#sb-adb-dot')) $('#sb-adb-dot').className = `status-dot ${state.health?.adb !== false ? 'ok' : 'bad'}`;
  if ($('#sb-port') && state.health?.port) $('#sb-port').textContent = String(state.health.port);

  const products = state.research?.products || [];
  if ($('#sb-research-count')) $('#sb-research-count').textContent = String(products.length);
  if ($('#nav-research-count')) $('#nav-research-count').textContent = String(products.length);
  if ($('#sb-hot-count')) $('#sb-hot-count').textContent = String(products.filter((p) => p.is_hot).length);
}

function renderAutoCaptureChip() {
  const chip = $('#chip-auto-clip');
  if (!chip) return;
  if (state.autoCapture) {
    chip.className = 'chip ok pulse';
    chip.textContent = '📱 Tự bắt link: BẬT';
  } else {
    chip.className = 'chip warn';
    chip.textContent = '📱 Tự bắt link: TẮT';
  }
}

async function loadDeviceImeStatus(serial = state.selected) {
  if (!serial) return;
  try {
    const info = await api(`/api/devices/${enc(serial)}/ime`);
    const isAdb = info.mode === 'adb';
    const sbIme = $('#sb-ime-state');
    if (sbIme && serial === state.selected) {
      sbIme.textContent = isAdb ? 'ADB IME' : 'Gboard Chuẩn';
      sbIme.style.color = isAdb ? '#fcd34d' : '#34d399';
    }
  } catch { /* ignore */ }
}

async function switchDeviceIme(serial = state.selected, mode = 'normal') {
  if (!serial) {
    toast('Vui lòng chọn điện thoại trước.', 'warn');
    return;
  }
  try {
    const res = await api(`/api/devices/${enc(serial)}/ime`, { method: 'POST', body: { mode } });
    toast(`✅ ${res.message}`, 'ok');
    loadDeviceImeStatus(serial);
    setTimeout(() => screen.pollShotNow(), 350);
  } catch (err) {
    toast(`Lỗi chuyển bàn phím: ${err.message}`, 'err');
  }
}

/* ------------------------------------------- Copy & Paste Product ID to Phone */
function setPhoneProductIdStrip(productId) {
  const code = $('#phone-product-id');
  if (!code) return;
  if (!productId) {
    code.textContent = 'Chưa có ID (Thả video)';
    return;
  }
  code.textContent = productId;
  state.lastProductId = productId;
}

async function copyAndPasteProductIdToPhone(productId, shouldPaste = true) {
  const pid = String(productId || '').trim();
  if (!pid) {
    toast('Chưa có ID sản phẩm.', 'warn');
    return;
  }
  await copyTextSafe(pid);
  setPhoneProductIdStrip(pid);

  if (!state.selected) {
    toast(`📋 Đã copy ID [${pid}] trên máy tính (chưa kết nối điện thoại).`, 'info');
    return;
  }
  try {
    const res = await api('/api/research/paste-id-to-phone', {
      method: 'POST',
      body: { serial: state.selected, productId: pid, paste: shouldPaste },
    });
    if (shouldPaste && res.pasted) {
      toast(`📲 Đã Copy & Paste thẳng ID [${pid}] vào điện thoại!`, 'ok');
    } else {
      toast(`📋 Đã Copy ID [${pid}] vào bảng nhớ Điện thoại & Máy tính!`, 'ok');
    }
    setTimeout(() => screen.pollShotNow(), 350);
  } catch (err) {
    toast(`Lỗi gửi ID sang điện thoại: ${err.message}`, 'err');
  }
}

$('#chk-auto-paste-id')?.addEventListener('change', (e) => {
  state.autoPasteId = e.target.checked;
  localStorage.setItem('trp.autoPasteId', state.autoPasteId ? '1' : '0');
  toast(
    state.autoPasteId
      ? '⚡ Đã BẬT: Khi thả video vào máy sẽ tự động Copy & Paste thẳng ID sản phẩm vào điện thoại!'
      : '📋 Đã chuyển sang chế độ chỉ Copy ID sản phẩm vào Clipboard (bấm "Paste Phone" khi cần dán).',
    'ok'
  );
});

function resolveActiveProductId() {
  const txt = ($('#phone-product-id')?.textContent || '').trim();
  const domId = /^\d{8,}$/.test(txt) ? txt : '';
  return state.lastProductId || domId || '';
}

$('#btn-copy-product-id')?.addEventListener('click', () => {
  const id = resolveActiveProductId();
  if (!id) {
    toast('Chưa có ID sản phẩm nào. Hãy kéo thả video có tên chứa ID vào máy hoặc chọn từ bảng Research!', 'warn');
    return;
  }
  copyAndPasteProductIdToPhone(id, false);
});

$('#btn-paste-product-id')?.addEventListener('click', () => {
  const id = resolveActiveProductId();
  if (!id) {
    pastePcClipboardToPhone();
    return;
  }
  copyAndPasteProductIdToPhone(id, true);
});

/* ------------------------------------------- Gõ phím & Dán chữ vào Phone */
let liveTypeTimer = null;
let isImeComposing = false;

async function sendTextToPhone(rawText, { isPaste = false, silent = false } = {}) {
  const text = String(rawText || '');
  if (!text || !screen.canControl()) return false;
  try {
    await api(`/api/devices/${enc(state.selected)}/input`, {
      method: 'POST',
      body: { action: 'text', text },
    });
    if (!silent) {
      const short = text.length > 42 ? text.slice(0, 40) + '…' : text;
      toast(isPaste ? `📋 Đã dán vào điện thoại: "${short}"` : `⌨️ Đã nhập vào điện thoại: "${short}"`, 'ok');
    }
    setTimeout(() => screen.pollShotNow(), 350);
    return true;
  } catch (err) {
    toast(`Lỗi gửi chữ vào điện thoại: ${err.message}`, 'err');
    return false;
  }
}

async function pastePcClipboardToPhone(explicitText = '') {
  if (!screen.canControl()) {
    toast('Vui lòng chọn điện thoại đang kết nối trước.', 'warn');
    return;
  }
  let clipText = explicitText;
  if (!clipText) {
    try { clipText = await navigator.clipboard.readText(); } catch { clipText = ''; }
  }
  try {
    if (clipText) {
      await sendTextToPhone(clipText, { isPaste: true });
    } else {
      const res = await api(`/api/devices/${enc(state.selected)}/input`, {
        method: 'POST',
        body: { action: 'paste_pc' },
      });
      const short = (res.text || '').length > 42 ? res.text.slice(0, 40) + '…' : (res.text || '');
      toast(`📋 Đã dán vào điện thoại: "${short}"`, 'ok');
      setTimeout(() => screen.pollShotNow(), 350);
    }
  } catch (err) {
    toast(`Lỗi dán chữ: ${err.message}`, 'err');
  }
}

$('#btn-paste-pc-clip')?.addEventListener('click', () => pastePcClipboardToPhone());
$('#btn-side-paste')?.addEventListener('click', () => pastePcClipboardToPhone());

$('#btn-phone-backspace')?.addEventListener('click', (e) => {
  if (!screen.canControl()) return;
  if (e.shiftKey) {
    api(`/api/devices/${enc(state.selected)}/input`, { method: 'POST', body: { action: 'clear_text' } })
      .then(() => { toast('🧹 Đã xoá sạch ô văn bản trên điện thoại.', 'ok'); setTimeout(() => screen.pollShotNow(), 350); })
      .catch((err) => toast(err.message, 'err'));
  } else {
    screen.key(67);
  }
});

const sendTextInput = $('#send-text-input');
if (sendTextInput) {
  sendTextInput.addEventListener('compositionstart', () => {
    isImeComposing = true;
    clearTimeout(liveTypeTimer);
  });
  sendTextInput.addEventListener('compositionend', () => {
    isImeComposing = false;
    scheduleLiveTypeFlush();
  });
  sendTextInput.addEventListener('input', () => {
    if (!isImeComposing) scheduleLiveTypeFlush();
  });
  sendTextInput.addEventListener('keydown', (e) => {
    if (e.key === 'Backspace' && !sendTextInput.value) {
      e.preventDefault();
      screen.key(67);
    } else if (e.key === 'Delete' && !sendTextInput.value) {
      e.preventDefault();
      screen.key(112);
    } else if (e.key === 'Enter' && !isImeComposing && !sendTextInput.value) {
      e.preventDefault();
      screen.key(66);
    }
  });
}

function scheduleLiveTypeFlush() {
  clearTimeout(liveTypeTimer);
  liveTypeTimer = setTimeout(() => {
    if (isImeComposing) return;
    const input = $('#send-text-input');
    if (!input || !input.value) return;
    const text = input.value;
    input.value = '';
    sendTextToPhone(text, { silent: true });
  }, 600);
}

$('#send-text')?.addEventListener('submit', (e) => {
  e.preventDefault();
  clearTimeout(liveTypeTimer);
  const input = $('#send-text-input');
  const text = input?.value || '';
  if (!text) {
    screen.key(66);
    return;
  }
  input.value = '';
  sendTextToPhone(text, { silent: false });
});

/* ------------------------------------------- Video Drag & Drop vào Phone */
let isUploadingVideo = false;

function showPhoneDropOverlay(title, sub, showProgress = false) {
  const overlay = $('#phone-drop-overlay');
  if (!overlay) return;
  if (title) {
    const elTitle = overlay.querySelector('.drop-title');
    if (elTitle) elTitle.textContent = title;
  }
  if (sub) {
    const elSub = overlay.querySelector('.drop-sub');
    if (elSub) elSub.textContent = sub;
  }
  const prog = $('#phone-drop-progress');
  if (prog) prog.hidden = !showProgress;
  overlay.hidden = false;
}

function hidePhoneDropOverlay(force = false) {
  const overlay = $('#phone-drop-overlay');
  if (!overlay) return;
  if (isUploadingVideo && !force) return;
  if (force) isUploadingVideo = false;
  overlay.hidden = true;
  const prog = $('#phone-drop-progress');
  if (prog) prog.hidden = true;
  const bar = $('#phone-drop-bar');
  if (bar) bar.style.width = '0%';
  const pct = $('#phone-drop-pct');
  if (pct) pct.textContent = 'Đang nạp 0%…';
}

function uploadSingleVideo(file, index, total) {
  return new Promise((resolve) => {
    const prefix = total > 1 ? `[${index}/${total}] ` : '';
    showPhoneDropOverlay(
      `${prefix}Đang nạp: ${file.name}`,
      state.autoPasteId
        ? 'Đang đẩy video vào Thư viện & tự động Copy + Paste ID sản phẩm vào điện thoại...'
        : 'Đang đẩy video vào Thư viện & tự động Copy ID sản phẩm vào Clipboard...',
      true
    );

    const bar = $('#phone-drop-bar');
    const pct = $('#phone-drop-pct');
    if (bar) bar.style.width = '0%';
    if (pct) pct.textContent = `${prefix}0%`;

    const autoPasteParam = state.autoPasteId ? '1' : '0';
    const xhr = new XMLHttpRequest();
    xhr.timeout = 300000;
    const url = `/api/devices/${enc(state.selected)}/upload-video?filename=${enc(file.name)}&autoPaste=${autoPasteParam}`;
    xhr.open('POST', url, true);
    xhr.setRequestHeader('X-Filename', enc(file.name));
    xhr.setRequestHeader('X-Auto-Paste', autoPasteParam);

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) {
        if (e.loaded >= e.total) {
          if (bar) bar.style.width = '99%';
          if (pct) pct.textContent = `${prefix}Đang nạp vào Thư viện TikTok & Paste ID sản phẩm vào máy…`;
        } else {
          const percent = Math.min(98, Math.round((e.loaded / e.total) * 100));
          if (bar) bar.style.width = `${percent}%`;
          if (pct) pct.textContent = `${prefix}${percent}% (${fmtSize(e.loaded)} / ${fmtSize(e.total)})`;
        }
      }
    };

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        if (bar) bar.style.width = '100%';
        if (pct) pct.textContent = `${prefix}100% Hoàn tất!`;
        let data = {};
        try { data = JSON.parse(xhr.responseText); } catch { /* ignore */ }
        resolve({
          ok: true,
          name: file.name,
          productId: data.productId || '',
          phonePasted: !!data.phonePasted,
          clipboardCopied: !!data.clipboardCopied,
        });
      } else {
        let errMsg = xhr.statusText;
        try {
          const res = JSON.parse(xhr.responseText);
          if (res.error) errMsg = res.error;
        } catch {
          if (xhr.responseText) errMsg = xhr.responseText.slice(0, 100);
        }
        resolve({ ok: false, name: file.name, error: errMsg });
      }
    };

    xhr.onerror = () => resolve({ ok: false, name: file.name, error: 'Mất kết nối tới máy chủ' });
    xhr.ontimeout = () => resolve({ ok: false, name: file.name, error: 'Quá thời gian truyền video (timeout 5 phút)' });
    xhr.send(file);
  });
}

async function uploadVideosToPhone(files) {
  if (!state.selected) {
    const firstReady = state.devices.find((d) => d.state === 'device');
    if (firstReady) {
      selectDevice(firstReady.serial);
    } else {
      toast('Vui lòng kết nối điện thoại trước khi kéo thả nạp video.', 'warn');
      return;
    }
  }
  const list = Array.from(files || []).filter(Boolean);
  if (!list.length) return;

  isUploadingVideo = true;
  let successCount = 0;
  let anyPasted = false;
  const errors = [];
  const extractedIds = [];

  for (let i = 0; i < list.length; i++) {
    const file = list[i];
    const res = await uploadSingleVideo(file, i + 1, list.length);
    if (res.ok) {
      successCount++;
      if (res.productId) {
        extractedIds.push(res.productId);
        await copyTextSafe(res.productId);
      }
      if (res.phonePasted) anyPasted = true;
    } else {
      errors.push(`${file.name}: ${res.error}`);
    }
  }

  isUploadingVideo = false;

  if (successCount === list.length) {
    const lastId = extractedIds[extractedIds.length - 1] || '';
    if (lastId) {
      setPhoneProductIdStrip(lastId);
      const actionMsg = anyPasted
        ? `📋✍️ Đã Copy & Paste thẳng ID [${lastId}] vào điện thoại!`
        : `📋 Đã Copy ID [${lastId}] vào Clipboard điện thoại & PC!`;
      toast(`🎬 Đã nạp ${list.length === 1 ? `"${list[0].name}"` : `${list.length} video`} vào Thư viện TikTok!\n${actionMsg}`, 'ok');
    } else {
      toast(`🎬 Đã nạp thành công ${list.length === 1 ? `"${list[0].name}"` : `${list.length} video`} vào Thư viện TikTok!`, 'ok');
    }
    setTimeout(() => {
      hidePhoneDropOverlay();
      screen.pollShotNow();
    }, 1000);
  } else if (successCount > 0) {
    toast(`⚠️ Đã nạp ${successCount}/${list.length} video. Lỗi: ${errors.join(', ')}`, 'warn');
    setTimeout(() => hidePhoneDropOverlay(), 2200);
  } else {
    toast(`❌ Lỗi nạp video: ${errors.join(', ')}`, 'err');
    hidePhoneDropOverlay(true);
  }
}

function filterVideoFiles(fileList) {
  return Array.from(fileList || []).filter((f) => {
    if (!f || !f.name) return false;
    const idx = f.name.lastIndexOf('.');
    const ext = idx >= 0 ? f.name.slice(idx).toLowerCase() : '';
    return (f.type && f.type.startsWith('video/')) || ['.mp4', '.mov', '.webm', '.m4v', '.mkv', '.avi'].includes(ext);
  });
}

function initPhoneDragDrop() {
  const dropZone = $('#screen-panel') || $('#phone');
  if (!dropZone) return;

  $('#btn-close-drop-overlay')?.addEventListener('click', (e) => {
    e.stopPropagation();
    hidePhoneDropOverlay(true);
  });

  // Ngăn Edge/Chrome tự phát video nếu người dùng kéo thả lệch ra ngoài khung điện thoại
  window.addEventListener('dragover', (e) => {
    e.preventDefault();
  });
  window.addEventListener('drop', (e) => {
    e.preventDefault();
    if (!isUploadingVideo) hidePhoneDropOverlay();
    const files = Array.from(e.dataTransfer?.files || []);
    if (!files.length) return;
    const videoFiles = filterVideoFiles(files);
    if (videoFiles.length) {
      uploadVideosToPhone(videoFiles);
    } else {
      toast('Hãy kéo thả file Video (.mp4, .mov, .webm, .m4v, .mkv, .avi) vào khung màn hình điện thoại!', 'warn');
    }
  });
  window.addEventListener('dragend', () => {
    if (!isUploadingVideo) hidePhoneDropOverlay();
  });

  const showHoverDrop = (e) => {
    const types = Array.from(e.dataTransfer?.types || []);
    const hasFiles = types.length === 0 || types.includes('Files') || types.includes('application/x-moz-file');
    if (hasFiles && !isUploadingVideo) {
      showPhoneDropOverlay(
        'Thả Video vào Điện Thoại',
        state.autoPasteId
          ? 'Tự động nạp vào Thư viện TikTok · Copy & Paste thẳng ID sản phẩm vào điện thoại!'
          : 'Tự động nạp vào Thư viện TikTok · Copy ID sản phẩm vào Clipboard điện thoại!',
        false
      );
    }
  };

  dropZone.addEventListener('dragenter', (e) => {
    e.preventDefault();
    e.stopPropagation();
    showHoverDrop(e);
  });

  dropZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
    showHoverDrop(e);
  });

  dropZone.addEventListener('dragleave', (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (!dropZone.contains(e.relatedTarget) && !isUploadingVideo) {
      hidePhoneDropOverlay();
    }
  });

  dropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    e.stopPropagation();

    const files = Array.from(e.dataTransfer?.files || []);
    if (!files.length) {
      if (!isUploadingVideo) hidePhoneDropOverlay();
      return;
    }

    const videoFiles = filterVideoFiles(files);
    if (!videoFiles.length) {
      hidePhoneDropOverlay();
      toast('Vui lòng kéo thả file Video (.mp4, .mov, .webm, .m4v, .mkv, .avi)!', 'warn');
      return;
    }

    uploadVideosToPhone(videoFiles);
  });

  const triggerPickVideo = () => {
    if (!state.selected && !state.devices.some((d) => d.state === 'device')) {
      toast('Vui lòng kết nối điện thoại trước khi nạp video.', 'warn');
      return;
    }
    const inputPush = $('#input-push-video');
    if (inputPush) {
      inputPush.value = '';
      inputPush.click();
    }
  };

  $('#btn-push-video')?.addEventListener('click', triggerPickVideo);
  $('#btn-top-push-video')?.addEventListener('click', triggerPickVideo);
  $('#input-push-video')?.addEventListener('change', (e) => {
    const files = Array.from(e.target.files || []);
    if (files.length) uploadVideosToPhone(files);
  });
}

/* -------------------------------------------------- AI Research Sản Phẩm */
function getProductDateInfo(timestamp) {
  if (!timestamp) return { key: 'unknown', title: 'Chưa rõ ngày', groupType: 'older', timeStr: '—', fullDate: '—' };
  const ms = (timestamp > 1e11) ? timestamp : timestamp * 1000;
  const d = new Date(ms);
  if (isNaN(d.getTime())) return { key: 'unknown', title: 'Chưa rõ ngày', groupType: 'older', timeStr: '—', fullDate: '—' };

  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  const key = `${y}-${m}-${day}`;
  const timeStr = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;

  const now = new Date();
  const todayStr = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
  const yest = new Date(now.getTime() - 24 * 60 * 60 * 1000);
  const yestStr = `${yest.getFullYear()}-${String(yest.getMonth() + 1).padStart(2, '0')}-${String(yest.getDate()).padStart(2, '0')}`;

  let title = `Ngày ${day} tháng ${m}, ${y}`;
  let groupType = 'older';
  if (key === todayStr) {
    title = 'Hôm nay';
    groupType = 'today';
  } else if (key === yestStr) {
    title = 'Hôm qua';
    groupType = 'yesterday';
  }
  return { key, title, groupType, timeStr, fullDate: `${day}/${m}/${y}` };
}

async function loadResearch(highlightId = null, highlightType = 'new') {
  try {
    const data = await api('/api/research/products');
    state.research = data;
    if (typeof data.autoCapture === 'boolean') {
      state.autoCapture = data.autoCapture;
      renderAutoCaptureChip();
    }
    const legacyBtn = $('#btn-research-import-legacy');
    if (legacyBtn) legacyBtn.hidden = !data.hasLegacyResearch;
    renderResearch(highlightId, highlightType);
  } catch (error) {
    toast(`Lỗi tải dữ liệu nghiên cứu: ${error.message}`, 'err');
  }
}

function renderResearch(highlightId = null, highlightType = 'new') {
  const { products = [], steps = [] } = state.research || {};
  if ($('#research-count')) $('#research-count').textContent = String(products.length);

  const stepsGrid = $('#research-steps-grid');
  if (stepsGrid && steps.length > 0) {
    stepsGrid.innerHTML = steps.map((s) => `
      <div class="research-step-card" title="${esc(s.desc)}">
        <div class="step-card-head"><span class="step-icon">${esc(s.icon)}</span><span>${esc(s.name)}</span></div>
        <div class="step-desc">${esc(s.desc)}</div>
      </div>
    `).join('');
    stepsGrid.hidden = state.stepsCollapsed;
    if ($('#btn-toggle-steps')) $('#btn-toggle-steps').textContent = state.stepsCollapsed ? 'Mở rộng' : 'Thu gọn';
  }

  const search = ($('#research-search')?.value || '').toLowerCase().trim();
  const filterDate = $('#research-filter-date')?.value || 'all';

  const filtered = products.filter((p) => {
    const dateInfo = getProductDateInfo(p.created_at);
    p._dateInfo = dateInfo;
    if (filterDate === 'hot') {
      if (!p.is_hot) return false;
    } else if (filterDate !== 'all' && dateInfo.groupType !== filterDate) {
      return false;
    }
    if (search) {
      const hay = `${p.title || ''} ${p.id || ''} ${p.category || ''} ${p.shop_name || ''} ${p.share_url || ''}`.toLowerCase();
      if (!hay.includes(search)) return false;
    }
    return true;
  });

  const groupMap = {};
  filtered.forEach((p) => {
    const info = p._dateInfo || getProductDateInfo(p.created_at);
    if (!groupMap[info.key]) {
      groupMap[info.key] = { key: info.key, title: info.title, groupType: info.groupType, items: [] };
    }
    groupMap[info.key].items.push(p);
  });

  const sortedKeys = Object.keys(groupMap).sort().reverse();
  if ($('#research-group-count')) $('#research-group-count').textContent = `${sortedKeys.length} ngày`;

  const empty = $('#research-empty');
  const tbody = $('#research-tbody');
  if (empty) empty.hidden = filtered.length > 0;

  const photoCount = products.filter((p) => p.has_details || p.local_thumb || p.image_url).length;
  const photoChip = $('#spai-photo-count');
  if (photoChip) {
    photoChip.textContent = `🖼️ ${photoCount}/${products.length} có ảnh`;
    photoChip.title = `${photoCount} / ${products.length} sản phẩm đã tải ảnh & mô tả chi tiết qua Cách 1 (SPAI Auto).`;
  }

  const hotCount = products.filter((p) => p.is_hot).length;
  const hotChip = $('#research-hot-count-chip');
  if (hotChip) {
    hotChip.textContent = `🔥 ${hotCount} SP HOT`;
    hotChip.style.background = filterDate === 'hot' ? 'rgba(239, 68, 68, 0.4)' : 'rgba(239, 68, 68, 0.15)';
    hotChip.style.borderColor = filterDate === 'hot' ? '#f87171' : 'rgba(239, 68, 68, 0.3)';
    hotChip.style.fontWeight = filterDate === 'hot' ? 'bold' : 'normal';
  }

  if (tbody) {
    let globalIndex = 1;
    let html = '';

    sortedKeys.forEach((key) => {
      const group = groupMap[key];
      group.items.sort((a, b) => (b.created_at || 0) - (a.created_at || 0));

      html += `
        <tr class="r-group-row">
          <td colspan="9">
            <div class="r-group-wrap">
              <span class="r-group-badge ${esc(group.groupType)}">📅 ${esc(group.title)}</span>
              <span class="r-group-count">(${group.items.length} sản phẩm)</span>
              <div class="r-group-line"></div>
              <button type="button" class="btn ghost xs r-btn-copy-group-full" data-group-key="${esc(group.key)}" style="color: #4ade80; border-color: rgba(74, 222, 128, 0.35);" title="Copy toàn bộ thông tin của tất cả sản phẩm trong ${esc(group.title)}">📋 Copy Full ngày này</button>
              <button type="button" class="btn ghost xs r-btn-copy-group" data-group-key="${esc(group.key)}" title="Copy tất cả ID của ${esc(group.title)}">📋 Copy ID ngày này</button>
            </div>
          </td>
        </tr>
      `;

      group.items.forEach((p) => {
        const checked = state.researchSelected.has(p.id) ? 'checked' : '';
        let flashCls = '';
        if (highlightId && p.id === highlightId) {
          flashCls = (highlightType === 'duplicate') ? 'row-duplicate-flash' : 'row-flash';
        }
        const dateInfo = p._dateInfo || getProductDateInfo(p.created_at);
        const sourceLabel = p.notes || '📱 Phone';
        const displayUrl = p.pdp_url || p.share_url || (p.id ? `https://shop.tiktok.com/vn/pdp/${p.id}` : '');

        html += `
          <tr data-id="${esc(p.id)}" class="${flashCls}">
            <td style="width: 26px;"><input type="checkbox" class="r-check" ${checked} data-id="${esc(p.id)}"></td>
            <td style="width: 24px; text-align: center; color: var(--muted); font-size: 11px;">${globalIndex++}</td>
            <td style="width: 36px; text-align: center;">
              <button type="button" class="btn-hot-toggle ${p.is_hot ? 'active' : ''}" data-id="${esc(p.id)}" title="${p.is_hot ? '🔥 Đang là sản phẩm HOT (Bấm để bỏ đánh dấu)' : 'Bấm để đánh dấu SP HOT'}">🔥</button>
            </td>
            <td class="prod-thumb-cell" style="width: 48px; text-align: center;">
              ${(p.local_thumb || p.image_url || p.has_details) ? `
                <div class="prod-thumb-box" data-id="${esc(p.id)}" title="Bấm để xem toàn bộ ảnh và mô tả chi tiết" style="width: 40px; height: 40px; border-radius: 6px; overflow: hidden; border: 1px solid var(--border); margin: 0 auto; cursor: pointer; display: flex; align-items: center; justify-content: center; background: #1a1e28;">
                  <img src="/api/research/products/${esc(p.id)}/image" class="prod-thumb-img" alt="${esc(p.title)}" loading="lazy" style="width: 100%; height: 100%; object-fit: cover;">
                </div>
              ` : `
                <button type="button" class="btn ghost xs r-btn-fetch-one" data-id="${esc(p.id)}" title="Tải ảnh và mô tả qua Cách 1 (SPAI Auto)" style="font-size: 10px; padding: 2px 4px; white-space: nowrap;">📸 Tải</button>
              `}
            </td>
            <td>
              <div class="prod-name" title="${esc(p.title)}" data-open-detail="${esc(p.id)}">${esc(p.title || 'Sản phẩm TikTok Shop')}</div>
              <div class="prod-meta-row">
                ${p.shop_name ? `<span class="prod-meta-shop" title="Cửa hàng: ${esc(p.shop_name)}">🏪 ${esc(p.shop_name)}</span>` : ''}
                ${p.category ? `<span class="chip" style="font-size: 10px; padding: 1px 5px;">🏷️ ${esc(p.category)}</span>` : ''}
                <span class="prod-meta-time" title="${esc(sourceLabel)}">⏰ ${esc(dateInfo.timeStr)}</span>
              </div>
            </td>
            <td class="prod-col-metrics" style="width: 95px;">
              <div class="prod-cell-price">${p.price > 0 ? Number(p.price).toLocaleString('vi-VN') + ' ₫' : '<span class="muted" style="font-size: 11px; font-weight: normal;">Chưa rõ</span>'}</div>
              <div class="prod-cell-sold" title="Số lượng đã bán">${p.sold_total > 0 ? '🔥 ' + Number(p.sold_total).toLocaleString('vi-VN') : (p.sold_total_label ? '🔥 ' + esc(p.sold_total_label) : '<span class="muted" style="font-size: 10.5px;">—</span>')}</div>
            </td>
            <td style="width: 120px; text-align: center;">
              <div class="prod-id-badge" title="Bấm để Copy ID vào PC & Phone: ${esc(p.id)}" data-copy="${esc(p.id)}" style="font-size: 10.5px; padding: 2px 5px;">
                📋 ${esc(p.id)}
              </div>
            </td>
            <td style="width: 65px; text-align: center;">
              <div class="prod-web-actions">
                ${displayUrl ? `
                  <a href="${esc(displayUrl)}" target="_blank" rel="noopener noreferrer" class="btn ghost xs" title="Mở link sản phẩm" style="padding: 2px 5px; font-size: 11px; color: #38bdf8;">🌐 Web</a>
                  <button type="button" class="btn ghost xs r-btn-link" title="Copy link sản phẩm" data-link="${esc(displayUrl)}" style="padding: 2px 4px; font-size: 11px;">📋</button>
                ` : '<span class="muted" style="font-size: 11px;">—</span>'}
              </div>
            </td>
            <td style="width: 230px; text-align: right;">
              <div class="prod-actions-row">
                <button type="button" class="btn ghost xs r-btn-paste-phone" title="Copy & Paste thẳng ID sản phẩm này vào ô đang chọn trên điện thoại" data-id="${esc(p.id)}" style="font-size: 10.5px; padding: 2px 6px; color: #38bdf8; border-color: rgba(56, 189, 248, 0.4);">📲 Paste Phone</button>
                <button type="button" class="btn ghost xs r-btn-copy-full" title="Copy toàn bộ thông tin sản phẩm (Tên, Giá, Đã bán, ID, Link ảnh...)" data-id="${esc(p.id)}" style="font-size: 10.5px; padding: 2px 6px; color: #4ade80; border-color: rgba(74, 222, 128, 0.35);">📋 Full</button>
                <button type="button" class="btn ghost xs r-btn-folder ${p.is_hot ? 'btn-folder-hot' : ''}" title="Mở thư mục lưu trữ sản phẩm trên máy tính" data-id="${esc(p.id)}" style="font-size: 10.5px; padding: 2px 5px;">${p.is_hot ? '🔥 Folder' : '📁 Folder'}</button>
                <button type="button" class="btn ghost xs r-btn-view-detail" title="Xem chi tiết toàn bộ ảnh & mô tả" data-id="${esc(p.id)}" style="font-size: 10.5px; padding: 2px 5px;">🔍</button>
                <button type="button" class="btn danger ghost xs r-btn-del" title="Xóa khỏi danh sách" data-id="${esc(p.id)}" style="font-size: 10.5px; padding: 2px 4px;">✕</button>
              </div>
            </td>
          </tr>
        `;
      });
    });

    tbody.innerHTML = html;

    if (highlightId) {
      setTimeout(() => {
        const row = tbody.querySelector(`tr[data-id="${CSS.escape(highlightId)}"]`);
        if (row) row.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }, 100);
    }
    const selectAllEl = $('#research-select-all');
    if (selectAllEl) {
      selectAllEl.checked = filtered.length > 0 && filtered.every((p) => state.researchSelected.has(p.id));
    }
    updateResearchSelectionUI();
  }
  renderStudioStatusBar();
}

function updateResearchSelectionUI() {
  const count = state.researchSelected.size;
  const delSelBtn = $('#btn-research-del-sel');
  if (delSelBtn) {
    delSelBtn.textContent = `🗑 Xóa chọn (${count})`;
    delSelBtn.disabled = count === 0;
  }
  const copySelBtn = $('#btn-research-copy-sel');
  if (copySelBtn) copySelBtn.textContent = count > 0 ? `📋 Copy đã chọn (${count})` : '📋 Copy đã chọn';
  const copyImgsSelBtn = $('#btn-research-copy-images-sel');
  if (copyImgsSelBtn) copyImgsSelBtn.textContent = count > 0 ? `🖼️ Copy link ảnh (${count})` : '🖼️ Copy link ảnh';
  const copyFullSelBtn = $('#btn-research-copy-full-sel');
  if (copyFullSelBtn) copyFullSelBtn.textContent = count > 0 ? `📋 Copy Full (${count})` : '📋 Copy Full';
}

function formatFullProductText(p) {
  if (!p) return '';
  const pdpUrl = p.pdp_url || (p.id ? `https://shop.tiktok.com/vn/pdp/${p.id}` : '');
  const priceStr = p.price > 0 ? `${Number(p.price).toLocaleString('vi-VN')} ₫` : 'Chưa rõ';
  const origPriceStr = (p.original_price > 0 && p.original_price > p.price)
    ? ` (Giá gốc: ${Number(p.original_price).toLocaleString('vi-VN')} ₫)`
    : '';
  const soldStr = p.sold_total > 0 ? Number(p.sold_total).toLocaleString('vi-VN') : (p.sold_total_label || 'Chưa rõ');

  const lines = [
    `📦 Tên sản phẩm: ${p.title || 'Sản phẩm TikTok Shop'}`,
    `🆔 ID Sản phẩm: ${p.id || ''}`,
    `🏪 Cửa hàng: ${p.shop_name || 'Chưa rõ'}`,
    `💰 Giá bán: ${priceStr}${origPriceStr}`,
    `🔥 Đã bán: ${soldStr}`,
  ];
  if (p.category) lines.push(`🏷️ Danh mục: ${p.category}`);
  if (pdpUrl) lines.push(`🌐 Link Web: ${pdpUrl}`);
  if (p.share_url && p.share_url !== pdpUrl) lines.push(`🔗 Link Chia sẻ: ${p.share_url}`);

  if (Array.isArray(p.variations) && p.variations.length > 0) {
    const varLines = p.variations.map((v) => {
      if (typeof v === 'string') return `  - ${v}`;
      if (v && typeof v === 'object') {
        const gName = v.group_name || 'Phân loại';
        const opts = (v.options || []).map((o) => typeof o === 'string' ? o : (o.name || '')).filter(Boolean).join(', ');
        return `  - ${gName}: ${opts}`;
      }
      return '';
    }).filter(Boolean);
    if (varLines.length > 0) lines.push(`🎨 Phân loại hàng:\n${varLines.join('\n')}`);
  }
  if (p.description) lines.push(`📝 Mô tả sản phẩm:\n${p.description}`);

  const images = (Array.isArray(p.images) && p.images.length > 0) ? p.images : (p.image_url ? [p.image_url] : []);
  if (images.length > 0) lines.push(`🖼️ Link ảnh sản phẩm (${images.length} ảnh):\n${images.join('\n')}`);
  if (Array.isArray(p.desc_images) && p.desc_images.length > 0) {
    lines.push(`🖼️ Link ảnh mô tả (${p.desc_images.length} ảnh):\n${p.desc_images.join('\n')}`);
  }
  return lines.join('\n');
}

$('#chip-auto-clip')?.addEventListener('click', async () => {
  try {
    const res = await api('/api/research/auto-capture', { method: 'POST', body: {} });
    state.autoCapture = !!res.enabled;
    renderAutoCaptureChip();
    toast(state.autoCapture ? '📱 Đã BẬT tự động bắt link từ điện thoại!' : '⏸ Đã TẮT tự động bắt link từ điện thoại.', 'ok');
  } catch (err) {
    toast(err.message, 'err');
  }
});

$('#btn-toggle-steps')?.addEventListener('click', () => {
  const grid = $('#research-steps-grid');
  if (!grid) return;
  state.stepsCollapsed = !grid.hidden;
  grid.hidden = state.stepsCollapsed;
  localStorage.setItem('trp.stepsCollapsed', state.stepsCollapsed ? '1' : '0');
  $('#btn-toggle-steps').textContent = state.stepsCollapsed ? 'Mở rộng' : 'Thu gọn';
});

$('#btn-research-import-legacy')?.addEventListener('click', (e) => guard(e.currentTarget, async () => {
  if (!confirm('Nhập toàn bộ danh sách sản phẩm & ảnh có sẵn từ Tool cũ sang kho mới này (không ghi đè sản phẩm đã có)?')) return;
  const res = await api('/api/research/import-legacy', { method: 'POST', body: { mode: 'legacy' } });
  toast(`📂 Đã nhập thành công ${res.added} sản phẩm từ Tool cũ (bỏ qua ${res.skipped} trùng lặp)!`, 'ok');
  loadResearch();
}));

$('#btn-research-add-manual')?.addEventListener('click', () => {
  $('#form-research-add')?.reset();
  if (state.lastPhoneClip) {
    $('#r-add-url').value = state.lastPhoneClip;
    $('#r-add-url').dispatchEvent(new Event('input'));
  }
  $('#dlg-research-add')?.showModal();
});
$('#r-add-cancel')?.addEventListener('click', () => $('#dlg-research-add')?.close());

$('#btn-paste-phone-single')?.addEventListener('click', async () => {
  let text = state.lastPhoneClip;
  if (!text && state.selected) {
    try { text = (await api(`/api/stream/${enc(state.selected)}/clipboard`)).clipboard; } catch { /* ignore */ }
  }
  if (!text) {
    try { text = await navigator.clipboard.readText(); } catch { /* ignore */ }
  }
  if (!text) { toast('Chưa có link sao chép từ điện thoại.', 'warn'); return; }
  $('#r-add-url').value = text;
  $('#r-add-url').dispatchEvent(new Event('input'));
  toast('Đã dán link từ điện thoại!', 'ok');
});

$('#r-add-url')?.addEventListener('input', () => {
  const val = $('#r-add-url').value.trim();
  if (!val || val.length < 10) return;
  api('/api/research/resolve', { method: 'POST', body: { url: val } })
    .then((res) => {
      if (res.title && !$('#r-add-title').value) $('#r-add-title').value = res.title;
      if (res.product_id && !$('#r-add-id').value) $('#r-add-id').value = res.product_id;
    })
    .catch(() => { });
});

$('#form-research-add')?.addEventListener('submit', (e) => {
  e.preventDefault();
  const payload = {
    share_url: $('#r-add-url').value.trim(),
    title: $('#r-add-title').value.trim(),
    id: $('#r-add-id').value.trim(),
    category: $('#r-add-cat')?.value.trim() || '',
    notes: $('#r-add-notes')?.value.trim() || '',
  };
  api('/api/research/products', { method: 'POST', body: payload })
    .then((res) => {
      $('#dlg-research-add').close();
      if (res.duplicate) {
        toast(`⚠️ [Trùng lặp] Sản phẩm "${res.product?.title || payload.title}" đã có trong danh sách!`, 'warn');
        loadResearch(res.product?.id, 'duplicate');
      } else {
        const prod = res.product || res;
        toast(`Đã lưu sản phẩm: ${prod.title || prod.id}`, 'ok');
        loadResearch(prod.id, 'new');
      }
    })
    .catch((err) => toast(err.message, 'err'));
});

$('#btn-research-bulk-paste')?.addEventListener('click', () => {
  $('#form-research-bulk')?.reset();
  if (state.lastPhoneClip) $('#bulk-paste-text').value = state.lastPhoneClip;
  $('#dlg-research-bulk')?.showModal();
});
$('#bulk-paste-cancel')?.addEventListener('click', () => $('#dlg-research-bulk')?.close());

$('#btn-paste-phone-bulk')?.addEventListener('click', async () => {
  let text = state.lastPhoneClip;
  if (!text && state.selected) {
    try { text = (await api(`/api/stream/${enc(state.selected)}/clipboard`)).clipboard; } catch { /* ignore */ }
  }
  if (!text) {
    try { text = await navigator.clipboard.readText(); } catch { /* ignore */ }
  }
  if (!text) { toast('Chưa có link sao chép từ điện thoại.', 'warn'); return; }
  const cur = $('#bulk-paste-text').value.trim();
  $('#bulk-paste-text').value = cur ? `${cur}\n${text}` : text;
  toast('Đã chèn nội dung từ điện thoại!', 'ok');
});

$('#form-research-bulk')?.addEventListener('submit', (e) => guard(e.currentTarget, async () => {
  e.preventDefault();
  const text = $('#bulk-paste-text').value.trim();
  if (!text) throw new Error('Vui lòng dán danh sách link hoặc ID sản phẩm.');
  toast('Đang nạp danh sách & tải ảnh qua Cách 1 (SPAI Auto)…', 'ok');
  const res = await api('/api/research/batch-add', { method: 'POST', body: { text } });
  $('#dlg-research-bulk').close();
  if (res.duplicates > 0 && res.added > 0) {
    toast(`⚡ Đã thêm ${res.added} sản phẩm mới (bỏ qua ${res.duplicates} trùng lặp).`, 'warn');
  } else if (res.duplicates > 0 && res.added === 0) {
    toast(`⚠️ Toàn bộ ${res.duplicates} sản phẩm đã có trong danh sách!`, 'warn');
  } else {
    toast(`⚡ Đã thêm thành công ${res.added} sản phẩm!`, 'ok');
  }
  loadResearch();
}));

$('#btn-research-copy-all-ids')?.addEventListener('click', async () => {
  const { products = [] } = state.research || {};
  if (!products.length) { toast('Danh sách sản phẩm trống.', 'warn'); return; }
  await copyTextSafe(products.map((p) => p.id).join('\n'));
  toast(`📋 Đã copy ${products.length} ID sản phẩm vào Clipboard!`, 'ok');
});

$('#btn-research-copy-sel')?.addEventListener('click', async () => {
  const selected = [...state.researchSelected];
  if (!selected.length) { toast('Chưa tick chọn sản phẩm nào.', 'warn'); return; }
  await copyTextSafe(selected.join('\n'));
  toast(`📋 Đã copy ${selected.length} ID sản phẩm đã chọn!`, 'ok');
});

$('#btn-research-copy-images-sel')?.addEventListener('click', async () => {
  const { products = [] } = state.research || {};
  if (!products.length) { toast('Danh sách sản phẩm trống.', 'warn'); return; }
  const selectedIds = state.researchSelected;
  const targetProducts = selectedIds.size > 0 ? products.filter((p) => selectedIds.has(p.id)) : products;
  const allUrls = [];
  targetProducts.forEach((p) => {
    if (p.images?.length) allUrls.push(...p.images);
    else if (p.image_url) allUrls.push(p.image_url);
  });
  const uniqueUrls = [...new Set(allUrls.filter(Boolean))];
  if (!uniqueUrls.length) {
    toast('Chưa có link ảnh nào. Hãy bấm "⚡ Tải ảnh & mô tả (Cách 1 - SPAI)" trước!', 'warn');
    return;
  }
  await copyTextSafe(uniqueUrls.join('\n'));
  toast(`🖼️ Đã copy ${uniqueUrls.length} link ảnh vào clipboard!`, 'ok');
});

$('#btn-research-copy-full-sel')?.addEventListener('click', async () => {
  const { products = [] } = state.research || {};
  if (!products.length) { toast('Danh sách sản phẩm trống.', 'warn'); return; }
  const selectedIds = state.researchSelected;
  const targetProducts = selectedIds.size > 0 ? products.filter((p) => selectedIds.has(p.id)) : products;
  const fullText = targetProducts.map((p, i) => `========== SẢN PHẨM #${i + 1} ==========\n${formatFullProductText(p)}`).join('\n\n');
  await copyTextSafe(fullText);
  toast(`📋 Đã copy Full thông tin của ${targetProducts.length} sản phẩm!`, 'ok');
});

$('#btn-research-open-data-dir')?.addEventListener('click', async () => {
  try {
    const res = await api('/api/research/open-folder', { method: 'POST', body: { type: 'images' } });
    if (res.ok) toast('📁 Đã mở thư mục sản phẩm trong File Explorer!', 'ok');
  } catch (err) {
    toast(`Lỗi: ${err.message}`, 'err');
  }
});
$('#btn-research-open-data-dir-top')?.addEventListener('click', () => $('#btn-research-open-data-dir')?.click());

$('#btn-research-open-hot-dir')?.addEventListener('click', async () => {
  try {
    const res = await api('/api/research/open-folder', { method: 'POST', body: { type: 'hot' } });
    if (res.ok) toast('🔥 Đã mở thư mục SP HOT trong File Explorer!', 'ok');
  } catch (err) {
    toast(`Lỗi: ${err.message}`, 'err');
  }
});
$('#btn-research-open-hot-dir-top')?.addEventListener('click', () => $('#btn-research-open-hot-dir')?.click());

$('#research-hot-count-chip')?.addEventListener('click', () => {
  const filterSelect = $('#research-filter-date');
  if (!filterSelect) return;
  filterSelect.value = (filterSelect.value === 'hot') ? 'all' : 'hot';
  renderResearch();
});

$('#btn-research-save-now')?.addEventListener('click', (e) => guard(e.currentTarget, async () => {
  const res = await api('/api/research/save', { method: 'POST' });
  toast(`💾 Đã lưu toàn bộ ${res.count} sản phẩm.`, 'ok');
}));

$('#btn-research-export')?.addEventListener('click', () => {
  window.open('/api/research/export', '_blank');
});

$('#btn-research-del-sel')?.addEventListener('click', (e) => guard(e.currentTarget, async () => {
  const selected = [...state.researchSelected];
  if (!selected.length) return;
  if (!confirm(`Xóa ${selected.length} sản phẩm đã chọn khỏi danh sách?`)) return;
  const res = await api('/api/research/delete-selected', { method: 'POST', body: { ids: selected } });
  state.researchSelected.clear();
  updateResearchSelectionUI();
  toast(`🗑 Đã xóa ${res.deleted || selected.length} sản phẩm!`, 'ok');
  loadResearch();
}));

$('#btn-research-restore')?.addEventListener('click', (e) => guard(e.currentTarget, async () => {
  if (!confirm('Khôi phục lại danh sách sản phẩm từ bản sao lưu gần nhất?')) return;
  const res = await api('/api/research/restore-backup', { method: 'POST' });
  if (res.ok && res.restored > 0) {
    toast(`⟲ Đã khôi phục ${res.restored} sản phẩm từ bản sao lưu!`, 'ok');
    loadResearch();
  } else {
    toast('Chưa có bản sao lưu nào.', 'warn');
  }
}));

$('#btn-research-clear')?.addEventListener('click', () => {
  const { products = [] } = state.research || {};
  if ($('#clear-modal-total')) $('#clear-modal-total').textContent = String(products.length);
  $('#dlg-research-confirm-clear')?.showModal();
});
$('#btn-cancel-clear')?.addEventListener('click', () => $('#dlg-research-confirm-clear')?.close());
$('#btn-confirm-execute-clear')?.addEventListener('click', (e) => guard(e.currentTarget, async () => {
  $('#dlg-research-confirm-clear')?.close();
  await api('/api/research/clear', { method: 'POST' });
  state.researchSelected.clear();
  updateResearchSelectionUI();
  toast('Đã xóa sạch danh sách (đã tạo bản sao lưu an toàn).', 'ok');
  loadResearch();
}));

$('#research-search')?.addEventListener('input', () => renderResearch());
$('#research-filter-date')?.addEventListener('change', () => renderResearch());

$('#research-select-all')?.addEventListener('change', (e) => {
  const checked = e.target.checked;
  const { products = [] } = state.research || {};
  if (checked) products.forEach((p) => state.researchSelected.add(p.id));
  else state.researchSelected.clear();
  renderResearch();
});

async function startBatchFetchResearch(method = 'spaiauto') {
  const { products = [] } = state.research || {};
  if (!products.length) {
    toast('Danh sách sản phẩm trống.', 'warn');
    return;
  }
  const selected = [...state.researchSelected];
  const ids = selected.length > 0 ? selected : [];
  const targetDesc = selected.length > 0 ? `${selected.length} sản phẩm đã chọn` : `các sản phẩm trong kho`;
  const modeDesc = method === 'browser'
    ? 'Thử Cách 2 (Browser) — nếu TikTok báo lỗi vùng quốc gia sẽ tự động dùng Cách 1 (SPAI Auto)'
    : 'Cách 1 (SPAI Auto tốc độ cao, không mở trình duyệt)';
  if (!confirm(`Tải ảnh, giá, số lượng bán và mô tả cho ${targetDesc} bằng ${modeDesc}?`)) return;
  try {
    const res = await api('/api/research/batch-fetch-details', {
      method: 'POST',
      body: { ids, force: selected.length > 0, method },
    });
    if (res.ok) {
      toast(`⚡ ${res.message || `Đang tải thông tin ${res.total} sản phẩm...`}`, 'ok');
      if (res.total > 0 && $('#research-fetch-progress-bar')) {
        $('#research-fetch-progress-bar').hidden = false;
      }
    } else {
      toast(res.message || 'Không thể bắt đầu tiến trình tải.', 'warn');
    }
  } catch (err) {
    toast(`Lỗi: ${err.message}`, 'err');
  }
}

$('#btn-research-fetch-all')?.addEventListener('click', () => startBatchFetchResearch('spaiauto'));
$('#btn-research-fetch-browser')?.addEventListener('click', () => startBatchFetchResearch('browser'));
$('#btn-research-stop-batch')?.addEventListener('click', () => {
  api('/api/research/batch-fetch-stop', { method: 'POST' })
    .then(() => toast('Đã gửi yêu cầu dừng tải.', 'info'))
    .catch((err) => toast(err.message, 'err'));
});

$('#research-tbody')?.addEventListener('change', (e) => {
  const chk = e.target.closest('.r-check');
  if (chk) {
    const id = chk.dataset.id;
    if (chk.checked) state.researchSelected.add(id);
    else state.researchSelected.delete(id);
    updateResearchSelectionUI();
  }
});

$('#research-tbody')?.addEventListener('click', async (e) => {
  const groupFullBtn = e.target.closest('.r-btn-copy-group-full');
  if (groupFullBtn) {
    const key = groupFullBtn.dataset.groupKey;
    const groupProds = (state.research?.products || []).filter((p) => (p._dateInfo || getProductDateInfo(p.created_at)).key === key);
    if (!groupProds.length) return;
    const fullText = groupProds.map((p, i) => `========== SẢN PHẨM #${i + 1} ==========\n${formatFullProductText(p)}`).join('\n\n');
    await copyTextSafe(fullText);
    toast(`📋 Đã copy Full thông tin của ${groupProds.length} sản phẩm trong ngày!`, 'ok');
    return;
  }

  const groupBtn = e.target.closest('.r-btn-copy-group');
  if (groupBtn) {
    const key = groupBtn.dataset.groupKey;
    const groupIds = (state.research?.products || [])
      .filter((p) => (p._dateInfo || getProductDateInfo(p.created_at)).key === key)
      .map((p) => p.id);
    if (!groupIds.length) return;
    await copyTextSafe(groupIds.join('\n'));
    toast(`📋 Đã copy ${groupIds.length} ID sản phẩm của ngày này!`, 'ok');
    return;
  }

  const pastePhoneBtn = e.target.closest('.r-btn-paste-phone');
  if (pastePhoneBtn) {
    const id = pastePhoneBtn.dataset.id;
    await copyAndPasteProductIdToPhone(id, true);
    return;
  }

  const copyFullBtn = e.target.closest('.r-btn-copy-full');
  if (copyFullBtn) {
    const id = copyFullBtn.dataset.id;
    const prod = (state.research?.products || []).find((p) => p.id === id);
    if (!prod) return;
    await copyTextSafe(formatFullProductText(prod));
    toast(`📋 Đã copy Full thông tin SP "${(prod.title || prod.id).slice(0, 32)}..."!`, 'ok');
    return;
  }

  const hotBtn = e.target.closest('.btn-hot-toggle');
  if (hotBtn) {
    const id = hotBtn.dataset.id;
    try {
      const res = await api('/api/research/toggle-hot', { method: 'POST', body: { id } });
      if (res.ok) {
        const prod = (state.research?.products || []).find((p) => p.id === id);
        if (prod) prod.is_hot = res.is_hot;
        toast(res.is_hot ? '🔥 Đã thêm vào SP HOT & tạo thư mục lưu trữ!' : 'Đã bỏ đánh dấu SP HOT', 'ok');
        renderResearch();
      }
    } catch (err) {
      toast(err.message, 'err');
    }
    return;
  }

  const folderBtn = e.target.closest('.r-btn-folder');
  if (folderBtn) {
    const id = folderBtn.dataset.id;
    try {
      const res = await api('/api/research/open-folder', { method: 'POST', body: { id } });
      if (res.ok) toast(`📁 Đã mở thư mục [${id}] trong File Explorer!`, 'ok');
    } catch (err) {
      toast(err.message, 'err');
    }
    return;
  }

  const copyBtn = e.target.closest('[data-copy]');
  if (copyBtn) {
    const id = copyBtn.dataset.copy;
    await copyAndPasteProductIdToPhone(id, false);
    return;
  }

  const linkBtn = e.target.closest('.r-btn-link');
  if (linkBtn) {
    await copyTextSafe(linkBtn.dataset.link);
    toast('Đã copy link sản phẩm!', 'ok');
    return;
  }

  const delBtn = e.target.closest('.r-btn-del');
  if (delBtn) {
    const id = delBtn.dataset.id;
    await api('/api/research/delete', { method: 'POST', body: { id } });
    state.researchSelected.delete(id);
    toast('Đã xóa sản phẩm.', 'ok');
    loadResearch();
    return;
  }

  const detailTarget = e.target.closest('.r-btn-view-detail, .prod-thumb-box, [data-open-detail]');
  if (detailTarget) {
    const id = detailTarget.dataset.id || detailTarget.dataset.openDetail;
    const prod = (state.research?.products || []).find((p) => p.id === id);
    if (prod) openProductDetail(prod);
    return;
  }

  const fetchBtn = e.target.closest('.r-btn-fetch-one');
  if (fetchBtn) {
    const id = fetchBtn.dataset.id;
    fetchBtn.disabled = true;
    fetchBtn.textContent = '⏳...';
    try {
      toast('⚡ Đang tải ảnh & mô tả qua Cách 1 (SPAI Auto)…', 'info');
      const res = await api(`/api/research/products/${enc(id)}/fetch-details`, {
        method: 'POST',
        body: { method: 'spaiauto' },
      });
      if (res.ok && res.product) {
        toast(`✨ [Cách 1 - SPAI] Đã tải xong: ${(res.product.title || '').slice(0, 35)}...`, 'ok');
        loadResearch();
      }
    } catch (err) {
      toast(`Lỗi tải sản phẩm: ${err.message}`, 'err');
      fetchBtn.disabled = false;
      fetchBtn.textContent = '📸 Thử lại';
    }
  }
});

/* ------------------------------------------- Product Detail Modal & Hover Preview */
let currentDetailProduct = null;

let hoverTooltip = document.getElementById('prod-hover-preview-tooltip');
if (!hoverTooltip) {
  hoverTooltip = document.createElement('div');
  hoverTooltip.id = 'prod-hover-preview-tooltip';
  hoverTooltip.className = 'prod-hover-preview';
  hoverTooltip.innerHTML = '<img src="" alt="Preview">';
  document.body.appendChild(hoverTooltip);
}

const researchTable = $('#research-table');
if (researchTable) {
  researchTable.addEventListener('mouseover', (e) => {
    const box = e.target.closest('.prod-thumb-box');
    if (!box?.dataset.id) return;
    hoverTooltip.querySelector('img').src = `/api/research/products/${enc(box.dataset.id)}/image`;
    hoverTooltip.style.display = 'block';
  });
  researchTable.addEventListener('mouseout', (e) => {
    const box = e.target.closest('.prod-thumb-box');
    if (box && !box.contains(e.relatedTarget)) hoverTooltip.style.display = 'none';
  });
  researchTable.addEventListener('mousemove', (e) => {
    if (hoverTooltip.style.display !== 'block') return;
    const pad = 16;
    let x = e.clientX + pad;
    let y = e.clientY - 110;
    if (x + 230 > window.innerWidth) x = e.clientX - 230 - pad;
    if (y < 10) y = 10;
    if (y + 230 > window.innerHeight) y = window.innerHeight - 240;
    hoverTooltip.style.left = `${x}px`;
    hoverTooltip.style.top = `${y}px`;
  });
}

function renderDetailGallery(images, productId) {
  const mainImg = $('#pdetail-main-img');
  const counter = $('#pdetail-img-counter');
  const thumbsStrip = $('#pdetail-thumbs-strip');
  const origLink = $('#pdetail-link-orig-img');
  if (!mainImg) return;

  if (!images || images.length === 0) {
    const fallbackUrl = `/api/research/products/${enc(productId)}/image`;
    mainImg.src = fallbackUrl;
    if (counter) counter.textContent = '1 / 1';
    if (thumbsStrip) thumbsStrip.innerHTML = '';
    if (origLink) origLink.href = fallbackUrl;
    return;
  }

  function selectImg(idx) {
    const url = images[idx];
    mainImg.src = url;
    if (counter) counter.textContent = `${idx + 1} / ${images.length}`;
    if (origLink) origLink.href = url;
    thumbsStrip?.querySelectorAll('.prod-thumb-item').forEach((el, i) => {
      el.classList.toggle('active', i === idx);
    });
  }

  if (thumbsStrip) {
    thumbsStrip.innerHTML = images.map((url, i) => `
      <div class="prod-thumb-item ${i === 0 ? 'active' : ''}" data-idx="${i}">
        <img src="${esc(url)}" alt="Thumb ${i + 1}" loading="lazy">
      </div>
    `).join('');
    thumbsStrip.querySelectorAll('.prod-thumb-item').forEach((el) => {
      el.addEventListener('click', () => selectImg(parseInt(el.dataset.idx, 10)));
    });
  }
  selectImg(0);
}

async function loadProductDetailFolder(prodId) {
  const pathEl = $('#pdetail-folder-path');
  const filesEl = $('#pdetail-folder-files');
  const labelEl = $('#pdetail-folder-label');
  if (!pathEl || !filesEl) return;

  pathEl.textContent = 'Đang kiểm tra thư mục máy tính...';
  filesEl.innerHTML = '<span class="chip" style="font-size: 10.5px; opacity: 0.7;">Đang tải danh sách tệp...</span>';
  try {
    const res = await api(`/api/research/product-files?id=${enc(prodId)}`);
    if (res.ok) {
      pathEl.textContent = res.path || '';
      if (labelEl) labelEl.textContent = res.is_hot ? 'Thư mục SP HOT trên máy tính' : 'Thư mục Sản phẩm trên máy tính';
      if (res.files?.length) {
        filesEl.innerHTML = res.files.map((f) => {
          const icon = f.is_dir ? '📁' : (/\.(webp|jpg|png)$/i.test(f.name) ? '🖼️' : (f.name.endsWith('.json') ? '📄' : '📝'));
          const sizeTxt = f.size ? ` (${Math.round(f.size / 1024)} KB)` : (f.count !== undefined ? ` (${f.count} file)` : '');
          return `<span class="chip" style="font-size: 11px; padding: 2px 7px;">${icon} ${esc(f.name)}${sizeTxt}</span>`;
        }).join('');
      } else {
        filesEl.innerHTML = '<span class="chip" style="font-size: 10.5px;">Chưa có tệp</span>';
      }
    }
  } catch (err) {
    pathEl.textContent = 'Không thể lấy đường dẫn thư mục';
    filesEl.innerHTML = `<span class="chip" style="font-size: 10.5px;">${esc(err.message)}</span>`;
  }
}

function openProductDetail(p) {
  if (!p) return;
  currentDetailProduct = p;
  const dlg = $('#dlg-product-detail');
  if (!dlg) return;

  $('#pdetail-title').textContent = p.title || 'Sản phẩm TikTok Shop';
  $('#pdetail-shop').textContent = p.shop_name ? `🏪 ${p.shop_name}` : '🏪 Chưa có tên Shop';
  $('#pdetail-cat').textContent = p.category ? `🏷️ ${p.category}` : '🏷️ Chưa phân loại';
  $('#pdetail-status').textContent = p.status_label || p.status || 'Tiềm năng';

  const hotBtn = $('#btn-pdetail-toggle-hot');
  if (hotBtn) {
    hotBtn.textContent = p.is_hot ? '🔥 Đang là SP HOT' : '🔥 Đánh dấu HOT';
    hotBtn.className = p.is_hot ? 'btn sm btn-hot-active' : 'btn sm ghost';
  }

  $('#pdetail-price').textContent = p.price > 0 ? `${Number(p.price).toLocaleString('vi-VN')} ₫` : 'Chưa có giá';
  const origEl = $('#pdetail-orig-price');
  if (origEl) {
    if (p.original_price > 0 && p.original_price > p.price) {
      origEl.textContent = `${Number(p.original_price).toLocaleString('vi-VN')} ₫`;
      origEl.style.display = 'block';
    } else {
      origEl.style.display = 'none';
    }
  }

  $('#pdetail-sold').textContent = p.sold_total > 0 ? Number(p.sold_total).toLocaleString('vi-VN') : (p.sold_total_label || '0');
  $('#pdetail-ratio').textContent = p.ratio_label || (p.ratio ? `1 : ${p.ratio}` : '—');
  $('#pdetail-ratio-sub').textContent = p.orders_7d ? `${p.orders_7d} đơn / ${p.creators_7d || 1} creator` : 'Đơn vs Creator';

  $('#pdetail-id').value = p.id || '';
  const pdpUrl = p.pdp_url || (p.id ? `https://shop.tiktok.com/vn/pdp/${p.id}` : (p.share_url || ''));
  $('#pdetail-pdp-url').value = pdpUrl;
  if ($('#pdetail-open-web')) $('#pdetail-open-web').href = pdpUrl || '#';

  const varBox = $('#pdetail-variations-box');
  const varList = $('#pdetail-variations-list');
  if (p.variations?.length && varBox && varList) {
    varBox.style.display = 'block';
    varList.innerHTML = p.variations.map((v) => {
      if (typeof v === 'string') return `<span class="chip" style="font-size: 11px;">${esc(v)}</span>`;
      if (v && typeof v === 'object') {
        const gName = v.group_name || 'Phân loại';
        const opts = (v.options || []).map((o) => typeof o === 'string' ? o : (o.name || '')).filter(Boolean).join(', ');
        return `<span class="chip" style="font-size: 11px;"><b>${esc(gName)}:</b> ${esc(opts || 'Mặc định')}</span>`;
      }
      return '';
    }).filter(Boolean).join('');
  } else if (varBox) {
    varBox.style.display = 'none';
  }

  const images = p.images?.length ? p.images : (p.image_url ? [p.image_url] : []);
  renderDetailGallery(images, p.id);

  const copyImgsBtn = $('#btn-pdetail-copy-images');
  if (copyImgsBtn) {
    copyImgsBtn.textContent = images.length > 0 ? `📋 Copy link ảnh (${images.length})` : '📋 Copy link ảnh';
    copyImgsBtn.disabled = images.length === 0;
  }

  const descEl = $('#pdetail-desc-text');
  if (descEl) {
    if (p.description) {
      descEl.textContent = p.description;
      if ($('#pdetail-desc-badge')) $('#pdetail-desc-badge').textContent = `${p.description.length} ký tự`;
    } else {
      descEl.innerHTML = '<i>Chưa có mô tả chi tiết. Bấm <b>⚡ Tải lại (Cách 1 - SPAI)</b> để cập nhật toàn bộ ảnh & mô tả.</i>';
      if ($('#pdetail-desc-badge')) $('#pdetail-desc-badge').textContent = 'Chưa tải';
    }
  }

  const descImgs = p.desc_images || [];
  if ($('#btn-pdetail-copy-desc-images')) {
    $('#btn-pdetail-copy-desc-images').textContent = descImgs.length > 0 ? `📋 Copy link ảnh mô tả (${descImgs.length})` : '📋 Copy link ảnh mô tả';
  }
  const descImgBox = $('#pdetail-desc-images-box');
  const descImgGrid = $('#pdetail-desc-images-grid');
  if (descImgBox && descImgGrid) {
    if (descImgs.length > 0) {
      descImgBox.style.display = 'block';
      descImgGrid.innerHTML = descImgs.map((img) => `
        <div class="prod-desc-thumb" title="Bấm để mở ảnh kích thước lớn" onclick="window.open('${esc(img)}', '_blank')">
          <img src="${esc(img)}" alt="Ảnh mô tả" loading="lazy">
        </div>
      `).join('');
    } else {
      descImgBox.style.display = 'none';
    }
  }

  loadProductDetailFolder(p.id);
  if (!dlg.open) dlg.showModal();
}

$('#btn-close-product-detail')?.addEventListener('click', () => $('#dlg-product-detail')?.close());
$('#btn-pdetail-close')?.addEventListener('click', () => $('#dlg-product-detail')?.close());

$('#btn-pdetail-copy-full')?.addEventListener('click', async () => {
  if (!currentDetailProduct) return;
  await copyTextSafe(formatFullProductText(currentDetailProduct));
  toast(`📋 Đã copy Full thông tin SP "${(currentDetailProduct.title || currentDetailProduct.id).slice(0, 32)}..."!`, 'ok');
});

$('#btn-pdetail-copy-id')?.addEventListener('click', () => {
  if (currentDetailProduct?.id) copyAndPasteProductIdToPhone(currentDetailProduct.id, false);
});

$('#btn-pdetail-paste-phone')?.addEventListener('click', () => {
  if (currentDetailProduct?.id) copyAndPasteProductIdToPhone(currentDetailProduct.id, true);
});

$('#btn-pdetail-copy-link')?.addEventListener('click', async () => {
  const link = $('#pdetail-pdp-url')?.value;
  if (link) {
    await copyTextSafe(link);
    toast('Đã copy Link sản phẩm!', 'ok');
  }
});

$('#btn-pdetail-copy-desc')?.addEventListener('click', async () => {
  if (currentDetailProduct?.description) {
    await copyTextSafe(currentDetailProduct.description);
    toast('Đã copy toàn bộ nội dung mô tả sản phẩm!', 'ok');
  } else {
    toast('Chưa có nội dung mô tả để copy.', 'warn');
  }
});

$('#btn-pdetail-copy-images')?.addEventListener('click', async () => {
  const p = currentDetailProduct;
  if (!p) return;
  const images = p.images?.length ? p.images : (p.image_url ? [p.image_url] : []);
  if (!images.length) return;
  await copyTextSafe(images.join('\n'));
  toast(`📋 Đã copy ${images.length} link ảnh sản phẩm!`, 'ok');
});

$('#btn-pdetail-copy-desc-images')?.addEventListener('click', async () => {
  const descImgs = currentDetailProduct?.desc_images || [];
  if (!descImgs.length) return;
  await copyTextSafe(descImgs.join('\n'));
  toast(`📋 Đã copy ${descImgs.length} link ảnh mô tả!`, 'ok');
});

$('#btn-pdetail-toggle-hot')?.addEventListener('click', async () => {
  if (!currentDetailProduct?.id) return;
  const id = currentDetailProduct.id;
  try {
    const res = await api('/api/research/toggle-hot', { method: 'POST', body: { id } });
    if (res.ok) {
      currentDetailProduct.is_hot = res.is_hot;
      const prod = (state.research?.products || []).find((p) => p.id === id);
      if (prod) prod.is_hot = res.is_hot;
      openProductDetail(currentDetailProduct);
      renderResearch();
      toast(res.is_hot ? '🔥 Đã thêm vào SP HOT & đồng bộ thư mục!' : 'Đã bỏ đánh dấu SP HOT', 'ok');
    }
  } catch (err) {
    toast(err.message, 'err');
  }
});

async function openProductFolderOnDesktop(prodId, isHot) {
  try {
    const res = await api('/api/research/open-folder', {
      method: 'POST',
      body: { id: prodId, type: isHot ? 'hot' : 'images' },
    });
    if (res.ok) {
      toast('📁 Đã mở thư mục sản phẩm trong File Explorer!', 'ok');
      loadProductDetailFolder(prodId);
    }
  } catch (err) {
    toast(err.message, 'err');
  }
}

$('#btn-pdetail-open-folder')?.addEventListener('click', () => {
  if (currentDetailProduct?.id) openProductFolderOnDesktop(currentDetailProduct.id, currentDetailProduct.is_hot);
});
$('#btn-pdetail-open-folder-action')?.addEventListener('click', () => {
  if (currentDetailProduct?.id) openProductFolderOnDesktop(currentDetailProduct.id, currentDetailProduct.is_hot);
});
$('#btn-pdetail-copy-folder-path')?.addEventListener('click', async () => {
  const path = $('#pdetail-folder-path')?.textContent?.trim();
  if (path && !path.startsWith('Đang') && !path.startsWith('Không')) {
    await copyTextSafe(path);
    toast('📋 Đã copy đường dẫn thư mục!', 'ok');
  }
});

async function refreshProductDetailWithMethod(btn, method = 'spaiauto') {
  if (!currentDetailProduct?.id) return;
  const oldText = btn.textContent;
  btn.disabled = true;
  btn.textContent = '⏳ Đang tải...';
  try {
    toast(
      method === 'browser'
        ? '🌐 Đang thử Cách 2 (Browser) — nếu TikTok báo lỗi vùng quốc gia sẽ tự động dùng Cách 1 (SPAI Auto)…'
        : '⚡ Đang cập nhật qua Cách 1 (SPAI Auto)…',
      'info'
    );
    const res = await api(`/api/research/products/${enc(currentDetailProduct.id)}/fetch-details`, {
      method: 'POST',
      body: { method },
    });
    if (res.ok && res.product) {
      const srcLabel = res.fallback_from === 'browser'
        ? 'Cách 1 (SPAI Auto — tự động chuyển từ Cách 2 lỗi vùng)'
        : (res.source === 'browser_tiktokshop' ? 'Cách 2 (Browser)' : 'Cách 1 (SPAI Auto)');
      toast(`✨ Đã cập nhật thành công qua ${srcLabel}!`, 'ok');
      currentDetailProduct = res.product;
      openProductDetail(res.product);
      loadResearch();
    }
  } catch (err) {
    toast(`Lỗi: ${err.message}`, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = oldText;
  }
}

$('#btn-pdetail-refresh-data')?.addEventListener('click', (e) => refreshProductDetailWithMethod(e.currentTarget, 'spaiauto'));
$('#btn-pdetail-refresh-browser')?.addEventListener('click', (e) => refreshProductDetailWithMethod(e.currentTarget, 'browser'));

/* ------------------------------------------- Wi-Fi ADB, QR, USB, BlueStacks */
function renderBsOptions(instances) {
  const select = $('#bs-select');
  if (!select) return;
  const current = select.value;
  select.innerHTML = '<option value="">BlueStacks…</option>' + (instances || []).map((i) =>
    `<option value="${esc(i.id)}" data-port="${i.port}">${i.online ? '🟢' : '⚪'} ${esc(i.name)} · A${esc(i.androidVersion)} · ${i.port}</option>`).join('');
  if (current && [...select.options].some((o) => o.value === current)) {
    select.value = current;
  } else {
    const online = (instances || []).find((i) => i.online);
    if (online) select.value = online.id;
  }
}

async function loadBlueStacks() {
  try {
    const { instances } = await api('/api/bluestacks/instances');
    renderBsOptions(instances);
  } catch { /* ignore */ }
}

function bsSelected() {
  const option = $('#bs-select')?.selectedOptions?.[0];
  if (!option || !option.value) throw new Error('Chọn một instance BlueStacks.');
  return { id: option.value, port: Number(option.dataset.port) };
}

$('#bs-refresh')?.addEventListener('click', loadBlueStacks);
$('#bs-launch')?.addEventListener('click', (e) => guard(e.currentTarget, async () => {
  await api('/api/bluestacks/launch', { method: 'POST', body: { id: bsSelected().id } });
  toast('Đang bật BlueStacks… khi khởi động xong sẽ tự động kết nối.', 'ok');
}));
$('#bs-connect')?.addEventListener('click', (e) => guard(e.currentTarget, async () => {
  const sel = bsSelected();
  const r = await api('/api/bluestacks/connect', { method: 'POST', body: { id: sel.id, port: sel.port } });
  toast(r.message, 'ok');
  loadBlueStacks();
}));

function buildLanItemsHtml(services) {
  return (services || []).map((s) => {
    const title = s.displayName || s.model || s.name || s.host;
    return `<div class="lan-item"><span>📶</span><div class="info">${esc(title)}<small>${esc(s.address)}</small></div>
      ${s.connected ? '<span class="chip ok">Đã nối</span>' : `<button type="button" class="btn xs primary" data-lan="${esc(s.address)}">Kết nối</button>`}</div>`;
  }).join('');
}

function renderLan(services) {
  state.lan = services || [];
  const itemsHtml = buildLanItemsHtml(state.lan);
  const sideBox = $('#lan-list');
  if (sideBox) {
    sideBox.innerHTML = state.lan.length
      ? `<div class="lan-head"><span class="small muted">Tìm thấy trong mạng LAN:</span><button type="button" class="btn xs ghost" data-lan-refresh="1">🔄 Quét</button></div>${itemsHtml}`
      : `<div class="lan-head"><span class="small muted">Chưa thấy máy Wi-Fi trong LAN</span><button type="button" class="btn xs ghost" data-lan-refresh="1">🔄 Quét LAN</button></div>`;
  }
  const modalBox = $('#w-lan-list');
  if (modalBox) {
    modalBox.innerHTML = state.lan.length
      ? itemsHtml
      : '<div class="small muted">Không thấy máy nào đang bật Gỡ lỗi không dây. Hãy kiểm tra điện thoại cùng Wi-Fi/LAN hoặc dùng <b>📷 Quét mã QR</b>.</div>';
  }
}

async function refreshLanDiscover(btn) {
  if (btn) btn.disabled = true;
  try {
    const r = await api('/api/wireless/discover?refresh=1');
    renderLan(r.services);
  } catch (err) {
    toast(`Lỗi quét LAN: ${err.message}`, 'err');
  } finally {
    if (btn) btn.disabled = false;
  }
}

function openCodeDialog(skipAutoScan = false) {
  if ($('#w-result')) $('#w-result').textContent = '';
  renderLan(state.lan);
  $('#dlg-wireless')?.showModal();
  if (!skipAutoScan) refreshLanDiscover($('#w-scan-lan'));
}

$('#btn-wireless')?.addEventListener('click', () => openCodeDialog(false));
$('#hub-code')?.addEventListener('click', () => openCodeDialog(false));
$('#w-scan-lan')?.addEventListener('click', () => refreshLanDiscover($('#w-scan-lan')));

function openQrDialog() {
  if ($('#dlg-wireless')?.open) $('#dlg-wireless').close();
  $('#dlg-qr')?.showModal();
  startQr();
}

$('#w-open-qr')?.addEventListener('click', openQrDialog);
$('#hub-qr')?.addEventListener('click', openQrDialog);
$('#btn-top-qr')?.addEventListener('click', openQrDialog);

$('#w-pair')?.addEventListener('click', (e) => {
  const button = e.currentTarget;
  button.disabled = true;
  $('#w-result').textContent = '⏳ Đang ghép nối…';
  api('/api/wireless/pair', {
    method: 'POST',
    body: {
      address: $('#w-pair-addr').value.trim(),
      code: $('#w-pair-code').value.trim(),
      connectAddress: $('#w-conn-addr').value.trim(),
    },
  })
    .then((r) => {
      $('#w-result').innerHTML = `<span style="color:var(--ok)">✅ ${esc(r.message)}</span>`;
      if (r.connected) {
        toast(`Đã kết nối ${r.connected}`, 'ok');
        setTimeout(() => $('#dlg-wireless').close(), 1200);
      }
    })
    .catch((err) => { $('#w-result').innerHTML = `<span style="color:var(--bad)">❌ ${esc(err.message)}</span>`; })
    .finally(() => { button.disabled = false; });
});

$('#w-connect')?.addEventListener('click', (e) => {
  const button = e.currentTarget;
  button.disabled = true;
  api('/api/wireless/connect', { method: 'POST', body: { address: $('#w-conn-addr').value.trim() } })
    .then((r) => { $('#w-result').innerHTML = `<span style="color:var(--ok)">✅ ${esc(r.message)}</span>`; toast(r.message, 'ok'); })
    .catch((err) => { $('#w-result').innerHTML = `<span style="color:var(--bad)">❌ ${esc(err.message)}</span>`; })
    .finally(() => { button.disabled = false; });
});

$('#w-auto-find')?.addEventListener('click', (e) => {
  const raw = ($('#w-conn-addr').value || $('#w-pair-addr').value || '').trim();
  const ip = raw.split(':')[0].trim();
  if (!ip) {
    $('#w-result').innerHTML = '<span style="color:var(--bad)">Vui lòng nhập IP máy (ví dụ 192.168.1.18).</span>';
    return;
  }
  const button = e.currentTarget;
  button.disabled = true;
  $('#w-result').innerHTML = `<span class="muted">⏳ Đang tự động dò cổng trên <b>${esc(ip)}</b>…</span>`;
  api('/api/wireless/connect', { method: 'POST', body: { address: ip } })
    .then((r) => {
      $('#w-result').innerHTML = `<span style="color:var(--ok)">✅ ${esc(r.message)}</span>`;
      toast(r.message, 'ok');
      setTimeout(() => $('#dlg-wireless').close(), 1200);
    })
    .catch((err) => { $('#w-result').innerHTML = `<span style="color:var(--bad)">❌ ${esc(err.message)}</span>`; })
    .finally(() => { button.disabled = false; });
});

function renderQrStatus(view) {
  const box = $('#qr-status');
  if (!box || !view || view.status === 'IDLE') return;
  const icon = { WAITING: '📷', PAIRING: '🔐', CONNECTING: '🔗', CONNECTED: '✅', NEEDS_PORT: '⚡', FAILED: '❌', CANCELLED: '⏹' }[view.status] || '';
  box.className = `qr-status ${view.status === 'CONNECTED' ? 'ok' : (view.status === 'FAILED' ? 'bad' : (view.status === 'NEEDS_PORT' ? 'warn' : ''))}`;
  box.textContent = `${icon} ${view.message || ''}`;

  const fallback = $('#qr-manual-fallback');
  if (fallback) {
    if (view.phoneIp) $('#qr-direct-ip').value = view.phoneIp;
    if (view.status === 'CONNECTING' || view.status === 'NEEDS_PORT' || view.status === 'FAILED') {
      fallback.style.display = 'block';
    } else if (view.status === 'WAITING' || view.status === 'CONNECTED') {
      fallback.style.display = 'none';
    }
  }
  if (view.status === 'CONNECTED') {
    toast(view.message, 'ok');
    setTimeout(() => { if ($('#dlg-qr')?.open) $('#dlg-qr').close(); }, 1400);
  }
}

async function startQr() {
  $('#qr-box').innerHTML = '<div class="spinner"></div>';
  $('#qr-status').className = 'qr-status';
  $('#qr-status').textContent = 'Đang tạo mã…';
  if ($('#qr-manual-fallback')) $('#qr-manual-fallback').style.display = 'none';
  if ($('#qr-direct-msg')) $('#qr-direct-msg').textContent = '';
  try {
    const r = await api('/api/wireless/qr/start', { method: 'POST' });
    $('#qr-box').innerHTML = r.svg;
    renderQrStatus(r);
  } catch (err) {
    $('#qr-box').innerHTML = '';
    renderQrStatus({ status: 'FAILED', message: err.message });
  }
}

$('#qr-renew')?.addEventListener('click', startQr);
$('#dlg-qr')?.addEventListener('close', () => { api('/api/wireless/qr/cancel', { method: 'POST' }).catch(() => { }); });

$('#qr-direct-connect')?.addEventListener('click', (e) => {
  const ip = ($('#qr-direct-ip').value || '').trim();
  const port = ($('#qr-direct-port').value || '').trim();
  if (!ip) {
    $('#qr-direct-msg').innerHTML = '<span style="color:var(--bad)">Vui lòng nhập IP máy.</span>';
    return;
  }
  const addr = port ? `${ip}:${port}` : ip;
  const button = e.currentTarget;
  button.disabled = true;
  $('#qr-direct-msg').innerHTML = '<span class="muted">⏳ Đang kết nối…</span>';
  api('/api/wireless/connect', { method: 'POST', body: { address: addr } })
    .then((r) => {
      $('#qr-direct-msg').innerHTML = `<span style="color:var(--ok)">✅ ${esc(r.message)}</span>`;
      toast(r.message, 'ok');
      setTimeout(() => { if ($('#dlg-qr')?.open) $('#dlg-qr').close(); }, 1200);
    })
    .catch((err) => { $('#qr-direct-msg').innerHTML = `<span style="color:var(--bad)">❌ ${esc(err.message)}</span>`; })
    .finally(() => { button.disabled = false; });
});

async function diagnoseUsb() {
  $('#usb-result').innerHTML = '<div class="spinner"></div><p class="muted small">Đang kiểm tra cổng USB và ADB…</p>';
  try {
    const r = await api('/api/usb/diagnose');
    const devices = r.usbDevices.length
      ? `<p class="small muted">Windows đang thấy:</p><ul class="small">${r.usbDevices.map((d) => `<li>${esc(d.name)} ${d.brand ? `· ${esc(d.brand)}` : ''} <span class="muted">(${esc(d.status)})</span></li>`).join('')}</ul>`
      : '<p class="small muted">Windows không thấy thiết bị Android nào trên cổng USB.</p>';
    $('#usb-result').innerHTML = `<div class="usb-verdict ${r.level}">${esc(r.verdict)}</div>
      ${r.hints.length ? `<ol class="steps small">${r.hints.map((h) => `<li>${esc(h)}</li>`).join('')}</ol>` : ''}${devices}`;
  } catch (err) {
    $('#usb-result').innerHTML = `<div class="usb-verdict bad">${esc(err.message)}</div>`;
  }
}

$('#hub-usb')?.addEventListener('click', () => { $('#dlg-usb')?.showModal(); diagnoseUsb(); });
$('#usb-again')?.addEventListener('click', diagnoseUsb);
$('#hub-adb')?.addEventListener('click', (e) => guard(e.currentTarget, async () => {
  const r = await api('/api/adb/restart', { method: 'POST' });
  setDevices(r.devices);
  toast(`Đã làm mới ADB · ${r.devices.length} thiết bị.`, 'ok');
}));

function handleLanListClick(e) {
  if (e.target.closest('[data-lan-refresh]')) {
    refreshLanDiscover(e.target.closest('[data-lan-refresh]'));
    return;
  }
  const btn = e.target.closest('[data-lan]');
  const address = btn?.dataset?.lan;
  if (!address) return;
  guard(btn, async () => {
    try {
      const r = await api('/api/wireless/connect', { method: 'POST', body: { address } });
      toast(r.message, 'ok');
      refreshLanDiscover();
    } catch (err) {
      $('#w-conn-addr').value = address;
      $('#w-pair-addr').value = `${address.split(':')[0]}:`;
      if (!$('#dlg-wireless').open) openCodeDialog(true);
      $('#w-result').innerHTML = `<span style="color:var(--warn)">${esc(err.message)}</span>`;
    }
  });
}
$('#lan-list')?.addEventListener('click', handleLanListClick);
$('#w-lan-list')?.addEventListener('click', handleLanListClick);

/* ------------------------------------------- Device Controls, Context Menu & Logs */
$('#device-list')?.addEventListener('click', (e) => {
  const card = e.target.closest('.device');
  if (card) selectDevice(card.dataset.serial);
});
$('#device-list')?.addEventListener('contextmenu', (e) => {
  const card = e.target.closest('.device');
  if (!card) return;
  e.preventDefault();
  selectDevice(card.dataset.serial);
  openDeviceContextMenu(card.dataset.serial, e.clientX, e.clientY);
});

$('#btn-stream-toggle')?.addEventListener('click', (e) => guard(e.currentTarget, async () => {
  if (!ready()) throw new Error('Thiết bị chưa sẵn sàng.');
  if (state.streams[state.selected]) {
    await api('/api/stream/stop', { method: 'POST', body: { serial: state.selected } });
  } else {
    screen.overlay('Đang khởi động scrcpy…');
    await api('/api/stream/start', { method: 'POST', body: { serial: state.selected } });
  }
}));

$$('#screen-panel .key[data-key]').forEach((btn) => btn.addEventListener('click', () => screen.key(Number(btn.dataset.key))));

$('#btn-open-tiktok')?.addEventListener('click', async () => {
  if (!state.selected) { toast('Chưa chọn thiết bị', 'err'); return; }
  toast('🎵 Đang mở ứng dụng TikTok…', 'ok');
  try {
    const res = await api(`/api/devices/${enc(state.selected)}/open-tiktok`, { method: 'POST' });
    toast(res.message || 'Đã mở TikTok!', 'ok');
  } catch (err) {
    toast(`Không mở được TikTok: ${err.message}`, 'err');
  }
});

$('#btn-stop-tiktok')?.addEventListener('click', async () => {
  if (!state.selected) { toast('Chưa chọn thiết bị', 'err'); return; }
  try {
    const res = await api(`/api/devices/${enc(state.selected)}/stop-tiktok`, { method: 'POST' });
    toast(res.message || 'Đã dừng TikTok', 'ok');
  } catch (err) {
    toast(`Lỗi dừng TikTok: ${err.message}`, 'err');
  }
});

$('#btn-fullscreen')?.addEventListener('click', () => {
  const phone = $('#phone');
  if (document.fullscreenElement) document.exitFullscreen();
  else phone?.requestFullscreen?.();
});

function getDefaultRightWidth() {
  const vw = window.innerWidth || 1536;
  if (vw >= 1680) return 490;
  if (vw >= 1400) return 455;
  return 415;
}

function getLayoutBounds() {
  const vw = window.innerWidth || 1536;
  const minLeft = 205;
  const maxLeft = Math.min(380, Math.max(240, Math.floor(vw * 0.28)));
  const effLeft = state.sidebarCollapsed ? 0 : Math.max(minLeft, Math.min(maxLeft, state.leftWidth || 252));
  const leftTotal = state.sidebarCollapsed ? 0 : (effLeft + 9);
  const minWorkspace = vw < 1280 ? 340 : 380;
  const minRight = 350;
  const maxRight = Math.max(430, Math.min(860, vw - leftTotal - minWorkspace - 32));
  return { minLeft, maxLeft, minRight, maxRight };
}

function updatePhoneStageSize() {
  const phone = $('#phone');
  const wrap = $('.phone-wrap');
  if (!phone || !wrap) return;

  if (document.fullscreenElement === phone) {
    phone.style.width = '';
    phone.style.height = '';
    return;
  }

  const isFitWidth = state.phoneFitMode === 'width';
  wrap.classList.toggle('fit-width-mode', isFitWidth);

  const rect = wrap.getBoundingClientRect();
  const availW = Math.max(160, rect.width - (isFitWidth ? 10 : 6));
  const availH = Math.max(220, rect.height - 6);
  const ratio = (screen.width > 0 && screen.height > 0) ? (screen.width / screen.height) : (9 / 19.5);

  let targetW;
  let targetH;
  if (isFitWidth) {
    targetW = Math.floor(availW);
    targetH = Math.floor(targetW / ratio);
  } else {
    targetH = Math.floor(availH);
    targetW = Math.floor(targetH * ratio);
    if (targetW > availW) {
      targetW = Math.floor(availW);
      targetH = Math.floor(targetW / ratio);
    }
  }

  phone.style.width = `${targetW}px`;
  phone.style.height = `${targetH}px`;

  const bottomNav = $('.phone-bottom-nav');
  if (bottomNav) {
    bottomNav.style.maxWidth = `${Math.max(248, Math.min(420, targetW))}px`;
  }
}

function applyLayoutSizes({ save = false } = {}) {
  const layout = $('#main-layout') || $('.layout');
  if (!layout) return;

  const { minLeft, maxLeft, minRight, maxRight } = getLayoutBounds();
  if (!localStorage.getItem('trp.rightWidth') && !state._userResizedRight) {
    state.rightWidth = getDefaultRightWidth();
  }

  state.leftWidth = Math.round(Math.max(minLeft, Math.min(maxLeft, state.leftWidth || 252)));
  state.rightWidth = Math.round(Math.max(minRight, Math.min(maxRight, state.rightWidth || getDefaultRightWidth())));
  state.previewWide = state.rightWidth >= 535;

  layout.style.setProperty('--left-col', `${state.leftWidth}px`);
  layout.style.setProperty('--right-col', `${state.rightWidth}px`);
  layout.classList.toggle('sidebar-collapsed', !!state.sidebarCollapsed);
  layout.classList.toggle('preview-wide', state.previewWide);

  const expandSidebarBtn = $('#btn-expand-sidebar');
  if (expandSidebarBtn) expandSidebarBtn.hidden = !state.sidebarCollapsed;

  const slider = $('#screen-width-slider');
  if (slider) {
    slider.min = String(minRight);
    slider.max = String(maxRight);
    slider.value = String(state.rightWidth);
  }

  const pxLabel = $('#screen-width-val');
  if (pxLabel) pxLabel.textContent = `${state.rightWidth}px`;

  const expandBtn = $('#btn-preview-expand');
  if (expandBtn) {
    expandBtn.textContent = state.previewWide ? '⤡ Thu gọn' : '⤢ Rộng';
    expandBtn.className = `btn xs ${state.previewWide ? 'studio-btn-cyan' : 'ghost'}`;
  }

  const fitBtn = $('#btn-phone-fit-mode');
  if (fitBtn) {
    const isWidth = state.phoneFitMode === 'width';
    fitBtn.textContent = isWidth ? '↔ Đầy ngang' : '↕ Khớp khung';
    fitBtn.classList.toggle('active-fit-width', isWidth);
  }

  if (save) {
    localStorage.setItem('trp.leftWidth', String(state.leftWidth));
    localStorage.setItem('trp.rightWidth', String(state.rightWidth));
    localStorage.setItem('trp.sidebarCollapsed', state.sidebarCollapsed ? '1' : '0');
    localStorage.setItem('trp.phoneFitMode', state.phoneFitMode);
  }

  requestAnimationFrame(updatePhoneStageSize);
}

function togglePreviewLarge() {
  state._userResizedRight = true;
  const { maxRight } = getLayoutBounds();
  if (state.rightWidth >= 535) {
    state.rightWidth = getDefaultRightWidth();
  } else {
    state.rightWidth = Math.min(maxRight, Math.max(560, getDefaultRightWidth() + 120));
  }
  applyLayoutSizes({ save: true });
}

function initLayoutResizers() {
  applyLayoutSizes({ save: false });

  const phoneWrap = $('.phone-wrap');
  if (phoneWrap && 'ResizeObserver' in window) {
    const ro = new ResizeObserver(() => updatePhoneStageSize());
    ro.observe(phoneWrap);
  }
  document.addEventListener('fullscreenchange', updatePhoneStageSize);
  window.addEventListener('resize', () => applyLayoutSizes({ save: false }));

  // Kéo thả thanh chia Cột Phải (Màn hình điện thoại)
  const rightResizer = $('#right-resizer');
  if (rightResizer) {
    let dragging = false;
    let startX = 0;
    let startW = 0;

    rightResizer.addEventListener('pointerdown', (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      dragging = true;
      state._userResizedRight = true;
      startX = e.clientX;
      startW = state.rightWidth;
      rightResizer.setPointerCapture(e.pointerId);
      rightResizer.classList.add('dragging');
      document.body.classList.add('is-col-resizing');
    });

    rightResizer.addEventListener('pointermove', (e) => {
      if (!dragging) return;
      const dx = e.clientX - startX;
      state.rightWidth = startW - dx;
      applyLayoutSizes({ save: false });
    });

    const stopRightDrag = () => {
      if (!dragging) return;
      dragging = false;
      rightResizer.classList.remove('dragging');
      document.body.classList.remove('is-col-resizing');
      applyLayoutSizes({ save: true });
    };
    rightResizer.addEventListener('pointerup', stopRightDrag);
    rightResizer.addEventListener('pointercancel', stopRightDrag);
    rightResizer.addEventListener('dblclick', () => {
      togglePreviewLarge();
      toast(`📐 Độ rộng khung Màn hình: ${state.rightWidth}px`, 'info');
    });
  }

  // Kéo thả thanh chia Cột Trái (Danh sách Điện thoại)
  const leftResizer = $('#left-resizer');
  if (leftResizer) {
    let dragging = false;
    let startX = 0;
    let startW = 0;

    leftResizer.addEventListener('pointerdown', (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      dragging = true;
      startX = e.clientX;
      startW = state.leftWidth;
      leftResizer.setPointerCapture(e.pointerId);
      leftResizer.classList.add('dragging');
      document.body.classList.add('is-col-resizing');
    });

    leftResizer.addEventListener('pointermove', (e) => {
      if (!dragging) return;
      const dx = e.clientX - startX;
      state.leftWidth = startW + dx;
      applyLayoutSizes({ save: false });
    });

    const stopLeftDrag = () => {
      if (!dragging) return;
      dragging = false;
      leftResizer.classList.remove('dragging');
      document.body.classList.remove('is-col-resizing');
      applyLayoutSizes({ save: true });
    };
    leftResizer.addEventListener('pointerup', stopLeftDrag);
    leftResizer.addEventListener('pointercancel', stopLeftDrag);
    leftResizer.addEventListener('dblclick', () => {
      state.leftWidth = 252;
      applyLayoutSizes({ save: true });
      toast('📐 Đã đặt lại độ rộng cột Điện thoại (252px).', 'info');
    });
  }

  // Thanh trượt & nút +/- chỉnh độ rộng trên đầu khung Màn hình
  $('#screen-width-slider')?.addEventListener('input', (e) => {
    state._userResizedRight = true;
    state.rightWidth = Number(e.target.value);
    applyLayoutSizes({ save: false });
  });
  $('#screen-width-slider')?.addEventListener('change', (e) => {
    state._userResizedRight = true;
    state.rightWidth = Number(e.target.value);
    applyLayoutSizes({ save: true });
  });
  $('#btn-screen-narrow')?.addEventListener('click', () => {
    state._userResizedRight = true;
    state.rightWidth -= 40;
    applyLayoutSizes({ save: true });
  });
  $('#btn-screen-wider')?.addEventListener('click', () => {
    state._userResizedRight = true;
    state.rightWidth += 40;
    applyLayoutSizes({ save: true });
  });
  $('#screen-width-val')?.addEventListener('click', () => {
    state._userResizedRight = true;
    state.rightWidth = getDefaultRightWidth();
    applyLayoutSizes({ save: true });
    toast(`📐 Đã đặt lại độ rộng chuẩn (${state.rightWidth}px).`, 'info');
  });

  // Đổi chế độ hiển thị: Khớp khung dọc (mặc định) / Đầy chiều ngang
  $('#btn-phone-fit-mode')?.addEventListener('click', () => {
    state.phoneFitMode = state.phoneFitMode === 'width' ? 'contain' : 'width';
    applyLayoutSizes({ save: true });
    toast(
      state.phoneFitMode === 'width'
        ? '↔ Đã chuyển sang chế độ Đầy chiều ngang: Màn hình máy sẽ phóng to tối đa theo độ rộng khung (lăn chuột để cuộn xem trên/dưới).'
        : '↕ Đã chuyển sang chế độ Khớp khung: Hiển thị trọn vẹn toàn bộ màn hình điện thoại.',
      'ok'
    );
  });

  // Thu gọn / Mở lại cột danh sách Điện thoại bên trái
  $('#btn-collapse-sidebar')?.addEventListener('click', () => {
    state.sidebarCollapsed = true;
    applyLayoutSizes({ save: true });
  });
  $('#btn-expand-sidebar')?.addEventListener('click', () => {
    state.sidebarCollapsed = false;
    applyLayoutSizes({ save: true });
  });

  $('#btn-preview-expand')?.addEventListener('click', togglePreviewLarge);
}

let miniPollTimer = null;
function renderMiniMultiGrid() {
  const grid = $('#mini-multi-grid');
  if (!grid || !state.miniMultiOpen) return;
  if (!state.devices.length) {
    grid.innerHTML = '<div class="muted small">Chưa có thiết bị kết nối.</div>';
    return;
  }
  grid.innerHTML = state.devices.map((d, idx) => {
    const numStr = d.number || String(idx + 1).padStart(2, '0');
    const isReady = d.state === 'device';
    const src = isReady ? `/api/devices/${enc(d.serial)}/screenshot?t=${Date.now()}` : '';
    return `
      <div class="mini-phone-card ${d.serial === state.selected ? 'active' : ''}" data-mini-serial="${esc(d.serial)}" title="Chuyển sang điều khiển #${esc(numStr)} ${esc(d.label)}">
        <div class="mini-head">
          <span>#${esc(numStr)} ${esc((d.label || d.serial).slice(0, 10))}</span>
          <span class="dot ${isReady ? 'ok' : 'warn'}"></span>
        </div>
        ${isReady
        ? `<img data-mini-img="${esc(d.serial)}" src="${esc(src)}" alt="${esc(d.label)}" loading="lazy">`
        : `<div style="aspect-ratio:9/19;display:grid;place-items:center;font-size:11px;color:#fb923c;">Offline</div>`
      }
      </div>
    `;
  }).join('');
}

$('#btn-toggle-mini-multi')?.addEventListener('click', () => {
  state.miniMultiOpen = !state.miniMultiOpen;
  const wrap = $('#mini-multi-grid');
  if (wrap) wrap.hidden = !state.miniMultiOpen;
  const btn = $('#btn-toggle-mini-multi');
  if (btn) {
    btn.className = `btn xs ${state.miniMultiOpen ? 'primary' : 'ghost'}`;
    btn.textContent = state.miniMultiOpen ? '⊞ Ẩn' : '⊞';
  }
  requestAnimationFrame(updatePhoneStageSize);
  clearInterval(miniPollTimer);
  if (state.miniMultiOpen) {
    renderMiniMultiGrid();
    miniPollTimer = setInterval(renderMiniMultiGrid, 2200);
  }
});
$('#mini-multi-grid')?.addEventListener('click', (e) => {
  const card = e.target.closest('[data-mini-serial]');
  if (card) selectDevice(card.dataset.miniSerial);
});
$('#preview-dev-pills')?.addEventListener('click', (e) => {
  const pill = e.target.closest('[data-select-serial]');
  if (pill) selectDevice(pill.dataset.selectSerial);
});

/* ---- Device Context Menu ---- */
function openDeviceContextMenu(serial, x = null, y = null) {
  const menu = $('#device-context-menu');
  if (!menu) return;
  const targetSerial = serial || state.selected || state.devices[0]?.serial || '';
  if (!targetSerial) {
    toast('Chưa có thiết bị nào được kết nối.', 'warn');
    return;
  }
  state.contextSerial = targetSerial;
  const idx = state.devices.findIndex((d) => d.serial === targetSerial);
  const d = state.devices[idx] || { serial: targetSerial, label: targetSerial, number: '01' };
  const numStr = d.number || String((idx >= 0 ? idx : 0) + 1).padStart(2, '0');

  if ($('#dcm-num')) $('#dcm-num').textContent = numStr;
  if ($('#dcm-title')) $('#dcm-title').textContent = d.label || d.serial;
  if ($('#dcm-sub')) $('#dcm-sub').textContent = `${d.serial}${d.group ? ` · 🏷️ ${d.group}` : ''}`;

  menu.hidden = false;
  const menuW = 270;
  const menuH = Math.min(window.innerHeight - 24, menu.offsetHeight || 520);
  let anchorX = x;
  let anchorY = y;
  const screenPanel = $('#screen-panel');
  if ((anchorX === null || anchorY === null) && screenPanel) {
    const spRect = screenPanel.getBoundingClientRect();
    anchorX = spRect.left - menuW - 8;
    anchorY = Math.max(60, spRect.top + 8);
  }
  menu.style.left = `${Math.max(10, Math.min(window.innerWidth - menuW - 12, anchorX ?? (window.innerWidth - 680)))}px`;
  menu.style.top = `${Math.max(10, Math.min(window.innerHeight - menuH - 12, anchorY ?? 68))}px`;
}

function closeDeviceContextMenu() {
  const menu = $('#device-context-menu');
  if (menu) menu.hidden = true;
}

async function handleDeviceContextAction(act) {
  const serial = state.contextSerial || state.selected;
  closeDeviceContextMenu();
  if (!serial) return;
  const d = state.devices.find((x) => x.serial === serial);

  switch (act) {
    case 'large_screen':
      selectDevice(serial);
      if (!state.previewWide) togglePreviewLarge();
      break;
    case 'change_number': {
      const next = prompt(`Nhập số hiệu mới cho máy "${d?.label || serial}" (01, 02...):`, d?.number || '01');
      if (next === null) return;
      await api(`/api/devices/${enc(serial)}`, { method: 'POST', body: { number: next.trim() } });
      setDevices((await api('/api/devices')).devices);
      break;
    }
    case 'rename': {
      const next = prompt('Nhập tên hiển thị mới cho thiết bị:', d?.label || serial);
      if (next === null) return;
      await api(`/api/devices/${enc(serial)}`, { method: 'POST', body: { label: next.trim() } });
      setDevices((await api('/api/devices')).devices);
      break;
    }
    case 'change_tag': {
      const next = prompt('Nhập tên Thẻ / Nhóm cho thiết bị:', d?.group || '');
      if (next === null) return;
      await api(`/api/devices/${enc(serial)}`, { method: 'POST', body: { group: next.trim() } });
      setDevices((await api('/api/devices')).devices);
      break;
    }
    case 'toggle_video_mode': {
      const nextMode = d?.videoMode === 'compatible' ? 'auto' : 'compatible';
      await api(`/api/devices/${enc(serial)}/video-mode`, { method: 'POST', body: { videoMode: nextMode } });
      toast(`🎞️ Đã chuyển sang chế độ ${nextMode === 'auto' ? 'Tự động (1280px Mượt)' : 'Tương thích (1024px Nhẹ)'}.`, 'ok');
      setDevices((await api('/api/devices')).devices);
      break;
    }
    case 'push_video':
      selectDevice(serial);
      $('#input-push-video')?.click();
      break;
    case 'send_text': {
      selectDevice(serial);
      const text = prompt(`Nhập văn bản muốn dán vào máy "${d?.label || serial}":`, '');
      if (!text) return;
      await sendTextToPhone(text);
      break;
    }
    case 'quick_phrases':
      selectDevice(serial);
      $('#dlg-quick-phrases')?.showModal();
      break;
    case 'save_screenshot': {
      const a = document.createElement('a');
      a.href = `/api/devices/${enc(serial)}/screenshot?t=${Date.now()}`;
      a.download = `screenshot_${(d?.label || serial).replace(/[^a-zA-Z0-9_-]/g, '_')}_${Date.now()}.png`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      toast('📸 Đang tải ảnh chụp màn hình về PC…', 'ok');
      break;
    }
    case 'export_clipboard':
      if (state.lastPhoneClip) {
        await copyTextSafe(state.lastPhoneClip);
        toast(`📋 Đã copy bảng nhớ từ Phone: ${state.lastPhoneClip.slice(0, 60)}`, 'ok');
      } else {
        toast('Hãy bấm Sao chép trên điện thoại để đồng bộ sang PC.', 'info');
      }
      break;
    case 'ime_normal':
      await switchDeviceIme(serial, 'normal');
      break;
    case 'ime_adb':
      await switchDeviceIme(serial, 'adb');
      break;
    case 'open_tiktok':
      await api(`/api/devices/${enc(serial)}/open-tiktok`, { method: 'POST' });
      toast('🎵 Đã mở TikTok!', 'ok');
      break;
    case 'stop_tiktok':
      await api(`/api/devices/${enc(serial)}/stop-tiktok`, { method: 'POST' });
      toast('⏹ Đã buộc dừng TikTok!', 'ok');
      break;
    case 'adb_settings':
      await api(`/api/devices/${enc(serial)}/quick-action`, { method: 'POST', body: { action: 'settings' } });
      break;
    case 'adb_wifi':
      await api(`/api/devices/${enc(serial)}/quick-action`, { method: 'POST', body: { action: 'wifi_settings' } });
      break;
    case 'adb_ime_settings':
      await api(`/api/devices/${enc(serial)}/quick-action`, { method: 'POST', body: { action: 'ime_settings' } });
      break;
    case 'switch_tcpip': {
      const r = await api(`/api/devices/${enc(serial)}/quick-action`, { method: 'POST', body: { action: 'tcpip' } });
      toast(r.message || '🔌 Đã mở cổng Wi-Fi ADB 5555!', 'ok');
      break;
    }
    case 'reconnect':
      toast('🔄 Đang đánh thức & kết nối lại stream…', 'ok');
      await api(`/api/devices/${enc(serial)}/wakeup`, { method: 'POST' }).catch(() => { });
      await api('/api/stream/start', { method: 'POST', body: { serial } }).catch(() => { });
      selectDevice(serial);
      break;
    case 'disconnect_device':
      if (d?.kind === 'wireless') {
        const r = await api('/api/wireless/disconnect', { method: 'POST', body: { address: serial } });
        toast(r.message || 'Đã ngắt kết nối Wi-Fi.', 'ok');
        setDevices((await api('/api/devices')).devices);
      } else {
        toast('Máy đang cắm cáp USB trực tiếp.', 'info');
      }
      break;
    default:
      break;
  }
}

document.addEventListener('click', (e) => {
  const menu = $('#device-context-menu');
  if (menu && !menu.hidden && !menu.contains(e.target)) closeDeviceContextMenu();
});
$('#dcm-close')?.addEventListener('click', closeDeviceContextMenu);
$('#device-context-menu')?.addEventListener('click', (e) => {
  const item = e.target.closest('[data-dcm]');
  if (item) {
    handleDeviceContextAction(item.dataset.dcm);
    return;
  }
  const navKey = e.target.closest('[data-dcm-key]');
  if (navKey) {
    const serial = state.contextSerial || state.selected;
    closeDeviceContextMenu();
    if (serial) {
      api(`/api/devices/${enc(serial)}/input`, { method: 'POST', body: { action: 'key', keycode: Number(navKey.dataset.dcmKey) } })
        .then(() => setTimeout(() => screen.pollShotNow(), 350))
        .catch((err) => toast(err.message, 'err'));
    }
  }
});

const toggleMenuBtn = (e) => {
  e.stopPropagation();
  const menu = $('#device-context-menu');
  if (menu && !menu.hidden && state.contextSerial === state.selected) closeDeviceContextMenu();
  else openDeviceContextMenu(state.selected);
};
$('#btn-ws-device-menu')?.addEventListener('click', toggleMenuBtn);
$('#btn-preview-more-menu')?.addEventListener('click', toggleMenuBtn);
$('#btn-side-screenshot')?.addEventListener('click', () => handleDeviceContextAction('save_screenshot'));
$('#btn-ws-fix-ime')?.addEventListener('click', () => switchDeviceIme(state.selected, 'normal'));
$('#btn-quick-ime-fix')?.addEventListener('click', () => switchDeviceIme(state.selected, 'normal'));

$('#sidebar-filter-pills')?.addEventListener('click', (e) => {
  const pill = e.target.closest('[data-filter]');
  if (!pill) return;
  state.deviceFilter = pill.dataset.filter || 'all';
  $$('#sidebar-filter-pills .filter-pill').forEach((p) => p.classList.toggle('active', p === pill));
  renderDevices();
});

$('#btn-toggle-connect-hub')?.addEventListener('click', () => {
  const box = $('#connect-hub-box');
  if (!box) return;
  box.dataset.userToggled = '1';
  box.hidden = !box.hidden;
});

async function runBatchDeviceAction(action, extra = {}) {
  const readySerials = state.devices.filter((d) => d.state === 'device').map((d) => d.serial);
  if (!readySerials.length) {
    toast('Chưa có thiết bị nào ở trạng thái Sẵn sàng.', 'warn');
    return;
  }
  try {
    const res = await api('/api/devices/batch-action', {
      method: 'POST',
      body: { serials: readySerials, action, ...extra },
    });
    toast(`✅ Đã thực hiện xong cho ${res.success}/${res.total} máy!`, 'ok');
  } catch (err) {
    toast(err.message, 'err');
  }
}
$('#btn-sidebar-wake-all')?.addEventListener('click', () => runBatchDeviceAction('wakeup'));
$('#btn-sidebar-gboard-all')?.addEventListener('click', () => runBatchDeviceAction('ime_normal'));

/* ---- Quick Phrases ---- */
$('#btn-quick-phrases')?.addEventListener('click', () => $('#dlg-quick-phrases')?.showModal());
$('#btn-qp-close')?.addEventListener('click', () => $('#dlg-quick-phrases')?.close());
$('#btn-qp-cancel')?.addEventListener('click', () => $('#dlg-quick-phrases')?.close());
$('#dlg-quick-phrases')?.addEventListener('click', (e) => {
  const phraseBtn = e.target.closest('.phrase-item[data-phrase]');
  if (!phraseBtn) return;
  const text = phraseBtn.dataset.phrase;
  if ($('#custom-broadcast-text')) $('#custom-broadcast-text').value = text;
  if (state.selected) sendTextToPhone(text, { isPaste: true });
});
$('#btn-send-text-one')?.addEventListener('click', () => {
  const text = ($('#custom-broadcast-text')?.value || '').trim();
  if (!text || !state.selected) { toast('Vui lòng nhập nội dung và chọn thiết bị.', 'warn'); return; }
  sendTextToPhone(text, { isPaste: true });
});
$('#btn-send-text-all')?.addEventListener('click', () => {
  const text = ($('#custom-broadcast-text')?.value || '').trim();
  if (!text) { toast('Vui lòng nhập nội dung.', 'warn'); return; }
  runBatchDeviceAction('text', { text });
});

/* ---- Logs & Topbar ---- */
const logLine = (e) => `<div><time>${fmtTime(e.at)}</time>${esc(e.message)}</div>`;
function appendLog(entry) {
  state.logs.push(entry);
  if (state.logs.length > 800) state.logs.splice(0, state.logs.length - 800);
  const drawer = $('#log-drawer');
  if (!drawer) return;
  if (drawer.hidden) {
    state.unreadLogs++;
    if ($('#log-badge')) {
      $('#log-badge').hidden = false;
      $('#log-badge').textContent = state.unreadLogs > 99 ? '99+' : String(state.unreadLogs);
    }
  } else {
    const list = $('#log-list');
    const stick = list.scrollTop + list.clientHeight >= list.scrollHeight - 30;
    list.insertAdjacentHTML('beforeend', logLine(entry));
    if (stick) list.scrollTop = list.scrollHeight;
  }
}

$('#btn-logs')?.addEventListener('click', () => {
  const drawer = $('#log-drawer');
  drawer.hidden = !drawer.hidden;
  if (!drawer.hidden) {
    state.unreadLogs = 0;
    if ($('#log-badge')) $('#log-badge').hidden = true;
    $('#log-list').innerHTML = state.logs.map(logLine).join('');
    $('#log-list').scrollTop = $('#log-list').scrollHeight;
  }
});
$('#log-close')?.addEventListener('click', () => { $('#log-drawer').hidden = true; });
$('#log-clear')?.addEventListener('click', () => { state.logs = []; $('#log-list').innerHTML = ''; });

$('#btn-top-reload')?.addEventListener('click', () => window.location.reload());
$('#btn-shutdown')?.addEventListener('click', async () => {
  if (!confirm('Tắt tool TikTok Research & Phone Studio?')) return;
  await api('/api/shutdown', { method: 'POST' }).catch(() => { });
  document.body.innerHTML = '<div style="display:grid;place-items:center;height:100vh;color:#8d91a5;font:16px Inter,sans-serif">Tool đã tắt an toàn. Bạn có thể đóng cửa sổ này.</div>';
});

/* ------------------------------------------- Global Keyboard & Ctrl+V */
function isOtherInputFocused() {
  const el = document.activeElement;
  if (!el) return false;
  if (el.id === 'phone' || el.id === 'send-text-input') return false;
  const tag = (el.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select' || el.isContentEditable) return true;
  if (el.closest('dialog[open]')) return true;
  return false;
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    closeDeviceContextMenu();
    return;
  }
  if (isOtherInputFocused()) return;
  const active = document.activeElement;
  const isPhoneOrBody = !active || active === document.body || active.id === 'phone' || active.closest('#screen-panel');
  if (!isPhoneOrBody || active?.id === 'send-text-input' || !screen.canControl()) return;

  if (e.key === 'Backspace') { e.preventDefault(); screen.key(67); return; }
  if (e.key === 'Delete') { e.preventDefault(); screen.key(112); return; }
  if (e.key === 'Enter') {
    e.preventDefault();
    const inp = $('#send-text-input');
    if (inp && inp.value) {
      clearTimeout(liveTypeTimer);
      const t = inp.value;
      inp.value = '';
      sendTextToPhone(t, { silent: false });
    } else {
      screen.key(66);
    }
    return;
  }
  if (e.key === 'ArrowUp') { e.preventDefault(); screen.key(19); return; }
  if (e.key === 'ArrowDown') { e.preventDefault(); screen.key(20); return; }
  if (e.key === 'ArrowLeft') { e.preventDefault(); screen.key(21); return; }
  if (e.key === 'ArrowRight') { e.preventDefault(); screen.key(22); return; }

  if (e.key.length === 1 && !e.ctrlKey && !e.altKey && !e.metaKey) {
    $('#send-text-input')?.focus({ preventScroll: true });
  }
});

document.querySelectorAll('dialog').forEach((dlg) => {
  dlg.addEventListener('click', (e) => {
    if (e.target === dlg) dlg.close();
  });
});

window.addEventListener('paste', async (e) => {
  if (isOtherInputFocused()) return;
  const active = document.activeElement;
  const isPhoneFocused = active && (active.id === 'phone' || active.id === 'send-text-input' || active.closest('#screen-panel'));
  const clipText = e.clipboardData?.getData('text') || '';

  // Nếu không đang focus vào màn hình Phone và clipboard là link sản phẩm TikTok -> nạp vào bảng Research
  if (!isPhoneFocused && clipText.trim() && /https?:\/\/.*(tiktok|douyin)/i.test(clipText)) {
    e.preventDefault();
    try {
      toast('⚡ Đang nạp link sản phẩm từ Clipboard & tải ảnh Cách 1…', 'ok');
      const res = await api('/api/research/batch-add', { method: 'POST', body: { text: clipText, source: 'Dán trực tiếp Ctrl+V' } });
      if (res.added > 0) {
        toast(`✨ Đã thêm ${res.added} sản phẩm vào danh sách Research!`, 'ok');
        loadResearch();
      } else if (res.duplicates > 0) {
        toast('⚠️ Sản phẩm này đã có sẵn trong danh sách Research!', 'warn');
      }
    } catch (err) {
      toast(`Lỗi thêm link: ${err.message}`, 'err');
    }
    return;
  }

  // Mặc định: nhấn Ctrl+V dán thẳng văn bản / ID sản phẩm vào điện thoại!
  if (!state.selected) return;
  e.preventDefault();
  clearTimeout(liveTypeTimer);
  const inp = $('#send-text-input');
  if (inp && active === inp) inp.value = '';
  await pastePcClipboardToPhone(clipText);
});

/* ------------------------------------------- Boot */
let loadingDevices = false;
async function loadDevices() {
  if (loadingDevices) return;
  loadingDevices = true;
  try {
    const res = await api('/api/devices');
    if (res?.devices) setDevices(res.devices);
  } catch { /* ignore */ } finally {
    loadingDevices = false;
  }
}

async function boot() {
  if ($('#chk-auto-paste-id')) $('#chk-auto-paste-id').checked = state.autoPasteId;
  initLayoutResizers();
  screen.init();
  initPhoneDragDrop();

  const modeChip = $('#screen-mode');
  if (modeChip) {
    modeChip.style.cursor = 'pointer';
    modeChip.title = 'Bấm để chuyển đổi giữa Realtime và Ảnh chụp';
    modeChip.addEventListener('click', () => {
      const d = device();
      if (!d || !ready(d)) return;
      if (screen.mode === 'live') {
        screen.preferShot = true;
        screen.setMode('shot');
        toast('Đã chuyển sang Ảnh chụp (bấm lại để bật Realtime).', 'ok');
      } else {
        screen.preferShot = false;
        screen.decodeErrors = 0;
        api('/api/stream/start', { method: 'POST', body: { serial: d.serial } })
          .then(() => { screen.mode = 'none'; screen.syncMode(); })
          .catch(() => toast('Không khởi động được stream.', 'err'));
      }
    });
  }

  try {
    state.health = await api('/api/health');
    if (typeof state.health.autoCapture === 'boolean') {
      state.autoCapture = state.health.autoCapture;
      renderAutoCaptureChip();
    }
  } catch (err) {
    toast(`Không kết nối được máy chủ: ${err.message}`, 'err');
  }

  api('/api/logs').then((r) => r.items.forEach(appendLog)).catch(() => { });
  connectWs();
  loadBlueStacks();
  setInterval(loadBlueStacks, 6000);
  api('/api/wireless/discover').then((r) => renderLan(r.services)).catch(() => { });

  await loadDevices();
  setInterval(loadDevices, 6000);
  await loadResearch();

  state.booted = true;
  screen.syncMode();
}

boot();
