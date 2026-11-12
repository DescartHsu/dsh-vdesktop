"use strict";
(() => {
  var __defProp = Object.defineProperty;
  var __defNormalProp = (obj, key, value) => key in obj ? __defProp(obj, key, { enumerable: true, configurable: true, writable: true, value }) : obj[key] = value;
  var __publicField = (obj, key, value) => __defNormalProp(obj, typeof key !== "symbol" ? key + "" : key, value);

  // src/client/VDesktopPanel.ts
  var SCALE_MODES = ["contain", "cover", "original"];
  var VDesktopPanel = class {
    constructor(root, opts) {
      __publicField(this, "root");
      __publicField(this, "desktop");
      __publicField(this, "frameUrl");
      __publicField(this, "inputUrl");
      __publicField(this, "scaleMode");
      __publicField(this, "pollMinMs");
      __publicField(this, "running", false);
      __publicField(this, "currentBitmap", null);
      __publicField(this, "frameW", 0);
      __publicField(this, "frameH", 0);
      __publicField(this, "srcW", 0);
      __publicField(this, "srcH", 0);
      __publicField(this, "_resizeTimer", null);
      __publicField(this, "_inFlight", false);
      __publicField(this, "_unavailable", false);
      __publicField(this, "modeBar");
      __publicField(this, "container");
      __publicField(this, "canvas");
      __publicField(this, "ctx");
      __publicField(this, "_ro");
      __publicField(this, "_infoLine");
      __publicField(this, "inputEl", null);
      __publicField(this, "lastInputStatusTs", 0);
      // b1（大脑 50bee53c19f6 段 A）：双槽状态 + 画面滞后灰点 + 追加模式
      __publicField(this, "statusFrameEl", null);
      __publicField(this, "statusInputEl", null);
      __publicField(this, "lagDotEl", null);
      __publicField(this, "lastOkFrameTs", 0);
      // M5a: cursor overlay
      __publicField(this, "cursorX", -1);
      __publicField(this, "startMenuEl", null);
      __publicField(this, "cursorY", -1);
      __publicField(this, "cursorVisible", false);
      __publicField(this, "appendMode", true);
      // M7（用户裁决）：完整 Windows 桌面帧（壁纸+任务栏+窗口），默认开；
      // 关闭 = 旧行为（黑底+仅应用窗口，channel=printwindow）
      __publicField(this, "fullDesktop", true);
      __publicField(this, "channelBtn", null);
      __publicField(this, "lagTimer", null);
      // b2（大脑 b1 收口裁决 #7）：dpr 变化（浏览器缩放/跨 DPI）matchMedia 监听
      __publicField(this, "dprMediaQuery", null);
      __publicField(this, "dprListener", null);
      this.root = root;
      this.desktop = opts.desktop;
      this.frameUrl = opts.frameUrl || "/vdesk/frame";
      this.inputUrl = opts.inputUrl || "/vdesk/input";
      this.scaleMode = opts.scaleMode || "contain";
      this.pollMinMs = opts.pollMinMs || 200;
      this._build();
      this._buildStatusPills();
      this._buildInputBar();
      this._installStartMenuKeys();
      this._watchDpr();
    }
    _build() {
      this.root.innerHTML = "";
      Object.assign(this.root.style, {
        display: "flex",
        flexDirection: "column",
        minWidth: "320px",
        minHeight: "240px"
      });
      const bar = document.createElement("div");
      Object.assign(bar.style, {
        display: "flex",
        gap: "6px",
        padding: "4px",
        borderBottom: "1px solid #333",
        background: "#1b1b1b",
        color: "#ddd",
        font: "12px/1.4 system-ui"
      });
      const mk = (label, val) => {
        const b = document.createElement("button");
        b.textContent = label;
        b.dataset.mode = val;
        Object.assign(b.style, {
          padding: "2px 8px",
          background: "#333",
          color: "#eee",
          border: "1px solid #444",
          borderRadius: "3px",
          cursor: "pointer"
        });
        b.onclick = () => {
          this.setScaleMode(val);
        };
        return b;
      };
      bar.appendChild(mk("\u9002\u5E94\u7A97\u53E3", "contain"));
      bar.appendChild(mk("\u586B\u6EE1", "cover"));
      bar.appendChild(mk("\u539F\u59CB\u5C3A\u5BF8", "original"));
      {
        const cb = document.createElement("button");
        cb.textContent = "\u5B8C\u6574\u684C\u9762";
        Object.assign(cb.style, {
          padding: "2px 8px",
          background: "#4a7",
          color: "#111",
          border: "1px solid #4a7",
          borderRadius: "3px",
          cursor: "pointer",
          marginLeft: "auto"
        });
        cb.onclick = () => {
          this.fullDesktop = !this.fullDesktop;
          cb.style.background = this.fullDesktop ? "#4a7" : "#333";
          cb.style.color = this.fullDesktop ? "#111" : "#eee";
          cb.textContent = this.fullDesktop ? "\u5B8C\u6574\u684C\u9762" : "\u4EC5\u7A97\u53E3";
          void this._fetchOne();
        };
        this.channelBtn = cb;
        bar.appendChild(cb);
      }
      this.modeBar = bar;
      this._infoLine = document.createElement("div");
      this._infoLine.textContent = `desktop=${this.desktop}  frame=${this.frameUrl}  input=${this.inputUrl}  shell \u5DF2\u542F\u7528\uFF08\u53D7 DWM \u9650\u5236\uFF0C\u8868\u9762\u6682\u4E0D\u53EF\u50CF\u7D20\u5316\u5448\u73B0\uFF1B\u4EFB\u52A1\u680F\u4ECD\u53EF\u70B9\u51FB\uFF09\uFF1B\u70B9\u51FB=\u903B\u8F91\u5B9A\u4F4D\uFF08\u5149\u6807\u6682\u4E0D\u6E32\u67D3\uFF09\uFF1B\u952E\u76D8\u8F93\u5165\u8BF7\u7528\u5E95\u90E8\u8F93\u5165\u6846\uFF1B\u753B\u9762\u66F4\u65B0\u53D7 DWM \u51BB\u7ED3\u9650\u5236\uFF0C\u8F93\u5165\u751F\u6548\u4EE5\u72B6\u6001\u706F/readback \u4E3A\u51C6`;
      Object.assign(this._infoLine.style, {
        flex: "0 0 auto",
        padding: "2px 4px",
        background: "#141414",
        color: "#666",
        font: "10px/1.3 ui-monospace, monospace",
        whiteSpace: "nowrap",
        overflow: "hidden",
        textOverflow: "ellipsis"
      });
      this.container = document.createElement("div");
      Object.assign(this.container.style, {
        flex: "1 1 auto",
        position: "relative",
        overflow: "hidden",
        background: "#000"
      });
      this.canvas = document.createElement("canvas");
      Object.assign(this.canvas.style, {
        position: "absolute",
        left: "0",
        top: "0",
        width: "100%",
        height: "100%",
        display: "block",
        cursor: "crosshair"
      });
      this.container.appendChild(this.canvas);
      this.startMenuEl = document.createElement("div");
      this.startMenuEl.id = "vdesk-start-menu";
      Object.assign(this.startMenuEl.style, {
        display: "none",
        position: "absolute",
        left: "8px",
        bottom: "48px",
        minWidth: "240px",
        background: "#202020",
        color: "#eee",
        border: "1px solid #444",
        borderRadius: "6px",
        boxShadow: "0 6px 24px rgba(0,0,0,0.55)",
        padding: "6px",
        font: "12px/1.4 system-ui",
        zIndex: "20"
      });
      this.container.appendChild(this.startMenuEl);
      this._buildStartMenuContent();
      this.ctx = this.canvas.getContext("2d");
      this.root.appendChild(bar);
      this.root.appendChild(this._infoLine);
      this.root.appendChild(this.container);
      this._ro = new ResizeObserver(() => {
        if (this._resizeTimer) clearTimeout(this._resizeTimer);
        this._resizeTimer = setTimeout(() => this._redrawCurrent(), 100);
      });
      this._ro.observe(this.container);
      this.canvas.addEventListener("click", (e) => {
        e.stopPropagation();
        this._onClick(e);
      });
      this._refreshModeButtons();
    }
    _refreshModeButtons() {
      for (const b of Array.from(this.modeBar.querySelectorAll("button"))) {
        b.style.background = b.dataset.mode === this.scaleMode ? "#4a7" : "#333";
      }
    }
    setScaleMode(mode) {
      if (!SCALE_MODES.includes(mode)) return;
      this.scaleMode = mode;
      this._refreshModeButtons();
      this._redrawCurrent();
    }
    _buildStatusPills() {
      const wrap = document.createElement("div");
      wrap.style.cssText = [
        "display:flex",
        "align-items:center",
        "gap:8px",
        "font:12px system-ui,sans-serif",
        "margin-left:auto"
      ].join(";");
      const dot = document.createElement("span");
      dot.style.cssText = [
        "display:inline-block",
        "width:8px",
        "height:8px",
        "border-radius:50%",
        "background:#666",
        "flex:0 0 auto"
      ].join(";");
      dot.title = "\u753B\u9762\u72B6\u6001";
      const frameEl = document.createElement("span");
      frameEl.textContent = "\u5E27 --";
      frameEl.style.cssText = "color:#8a8a8a;flex:0 0 auto;";
      const inputEl = document.createElement("span");
      inputEl.textContent = "";
      inputEl.style.cssText = "color:#7bbf7b;flex:0 0 auto;min-width:0;";
      wrap.appendChild(dot);
      wrap.appendChild(frameEl);
      wrap.appendChild(inputEl);
      this.modeBar.appendChild(wrap);
      this.lagDotEl = dot;
      this.statusFrameEl = frameEl;
      this.statusInputEl = inputEl;
      this.lagTimer = window.setInterval(() => this._updateLagDot(), 1e3);
    }
    _updateLagDot() {
      if (!this.lagDotEl) return;
      const age = Date.now() - this.lastOkFrameTs;
      if (age < 5e3 && this.lastOkFrameTs > 0) {
        this.lagDotEl.style.background = "#4caf50";
        this.lagDotEl.title = "\u753B\u9762\u5B9E\u65F6";
      } else {
        this.lagDotEl.style.background = "#666";
        this.lagDotEl.title = "\u753B\u9762\u53EF\u80FD\u6EDE\u540E\uFF08DWM \u51BB\u7ED3\u9650\u5236\uFF09";
      }
    }
    start() {
      if (this.running) return;
      this.running = true;
      void this._loop();
    }
    stop() {
      this.running = false;
    }
    destroy() {
      this.stop();
      if (this._ro) this._ro.disconnect();
      if (this._resizeTimer) clearTimeout(this._resizeTimer);
      if (this.lagTimer !== null) {
        window.clearInterval(this.lagTimer);
        this.lagTimer = null;
      }
      if (this.dprMediaQuery && this.dprListener) {
        this.dprMediaQuery.removeEventListener("change", this.dprListener);
        this.dprMediaQuery = null;
        this.dprListener = null;
      }
      if (this.currentBitmap && this.currentBitmap.close) this.currentBitmap.close();
      this.currentBitmap = null;
      this.root.innerHTML = "";
    }
    async _loop() {
      while (this.running) {
        const t0 = performance.now();
        await this._fetchOne();
        const dt = performance.now() - t0;
        const wait = Math.max(0, this.pollMinMs - dt);
        await new Promise((r) => setTimeout(r, wait));
      }
    }
    async _fetchOne() {
      if (this._inFlight) return;
      this._inFlight = true;
      try {
        const url = this.frameUrl + "?desktop=" + encodeURIComponent(this.desktop) + "&channel=" + (this.fullDesktop ? "desktop" : "printwindow") + "&t=" + Date.now();
        const resp = await fetch(url, { cache: "no-store" });
        if (resp.status === 204) {
          this._unavailable = true;
          this._markUnavailable();
          if (this.statusFrameEl) this.statusFrameEl.textContent = "204 \u89C6\u89C9\u4E0D\u53EF\u7528";
          return;
        }
        if (!resp.ok) {
          if (this.statusFrameEl) this.statusFrameEl.textContent = `\u5E27\u9519\u8BEF HTTP ${resp.status}`;
          this._markUnavailable();
          return;
        }
        this.frameW = parseInt(resp.headers.get("X-Frame-Width") || "0", 10);
        this.frameH = parseInt(resp.headers.get("X-Frame-Height") || "0", 10);
        this.srcW = parseInt(
          resp.headers.get("X-Source-Width") || String(this.frameW),
          10
        );
        this.srcH = parseInt(
          resp.headers.get("X-Source-Height") || String(this.frameH),
          10
        );
        const cxHdr = resp.headers.get("X-Cursor-X");
        const cyHdr = resp.headers.get("X-Cursor-Y");
        const cvHdr = resp.headers.get("X-Cursor-Visible");
        if (cxHdr !== null && cyHdr !== null) {
          this.cursorX = parseInt(cxHdr, 10);
          this.cursorY = parseInt(cyHdr, 10);
          this.cursorVisible = cvHdr === "1";
        } else {
          this.cursorVisible = false;
        }
        const blob = await resp.blob();
        const bmp = await createImageBitmap(blob);
        if (this.currentBitmap && this.currentBitmap.close) {
          this.currentBitmap.close();
        }
        this.currentBitmap = bmp;
        this._unavailable = false;
        if (this.statusFrameEl) this.statusFrameEl.textContent = `\u5E27 OK ${this.frameW}x${this.frameH}`;
        this.lastOkFrameTs = Date.now();
        this.draw();
      } catch (e) {
        if (this.statusFrameEl) this.statusFrameEl.textContent = `\u5E27\u9519\u8BEF\uFF08\u7F51\u7EDC\uFF09${e instanceof Error ? e.message.slice(0, 40) : ""}`;
      } finally {
        this._inFlight = false;
      }
    }
    /**
     * canvas backing store 同步（大脑 def7b21d60b0 根因修复 H4）：
     * canvas 默认 backing store 为 300x150，若从不按显示尺寸设置，CSS
     * 100%x100% 会把 300x150 拉伸到整个舞台，按 clientWidth 计算的目标
     * 矩形落到越界区域被裁 → 整片黑 + 左上角小残影。凡绘制/覆盖层之前
     * 必须先同步 backing store = container 显示尺寸 × dpr。
     */
    syncCanvasBackingSize() {
      const dpr = window.devicePixelRatio || 1;
      const cw = this.container.clientWidth | 0;
      const ch = this.container.clientHeight | 0;
      if (cw <= 0 || ch <= 0) return;
      const w = Math.max(1, Math.round(cw * dpr));
      const h = Math.max(1, Math.round(ch * dpr));
      if (this.canvas.width !== w) this.canvas.width = w;
      if (this.canvas.height !== h) this.canvas.height = h;
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    // b2（大脑 b1 收口裁决 #7）：浏览器缩放/跨 DPI 切换时 devicePixelRatio 变化
    // 不触发 ResizeObserver；用 matchMedia('(resolution: Xdppx)') 监听，
    // 变化时重新注册新 dpr 的监听（旧 mq 只在变化时 fire 一次），
    // 并立即同步 backing store + 重绘当前帧。
    _watchDpr() {
      const setup = () => {
        const dpr = window.devicePixelRatio || 1;
        const mq = window.matchMedia(`(resolution: ${dpr}dppx)`);
        const onchange = () => {
          if (this.dprMediaQuery && this.dprListener) {
            this.dprMediaQuery.removeEventListener("change", this.dprListener);
          }
          this.syncCanvasBackingSize();
          this._redrawCurrent();
          setup();
        };
        this.dprMediaQuery = mq;
        this.dprListener = onchange;
        mq.addEventListener("change", onchange);
      };
      setup();
    }
    _markUnavailable() {
      const c = this.canvas;
      const ctx = this.ctx;
      this.syncCanvasBackingSize();
      const dpr = window.devicePixelRatio || 1;
      const Wc = c.width / dpr;
      const Hc = c.height / dpr;
      ctx.fillStyle = "#111";
      ctx.fillRect(0, 0, Wc, Hc);
      ctx.fillStyle = "#888";
      ctx.font = "14px system-ui";
      ctx.fillText("\u4F1A\u8BDD\u89C6\u89C9\u4E0D\u53EF\u7528\uFF08DWM/\u684C\u9762\u51BB\u7ED3\uFF09", 12, 24);
    }
    _redrawCurrent() {
      if (this.currentBitmap) this._draw();
      else if (this._unavailable) this._markUnavailable();
    }
    _computeScaleOffset(Wc, Hc) {
      const Wd = this.frameW || 1;
      const Hd = this.frameH || 1;
      let scale, ox, oy;
      if (this.scaleMode === "original") {
        scale = 1;
        ox = (Wc - Wd) / 2;
        oy = (Hc - Hd) / 2;
      } else if (this.scaleMode === "cover") {
        scale = Math.max(Wc / Wd, Hc / Hd);
        ox = (Wc - Wd * scale) / 2;
        oy = (Hc - Hd * scale) / 2;
      } else {
        scale = Math.min(Wc / Wd, Hc / Hd);
        ox = (Wc - Wd * scale) / 2;
        oy = (Hc - Hd * scale) / 2;
      }
      return { scale, ox, oy };
    }
    draw() {
      this._draw();
    }
    _draw() {
      this.syncCanvasBackingSize();
      const bmp = this.currentBitmap;
      if (!bmp) return;
      const dpr = window.devicePixelRatio || 1;
      const c = this.canvas;
      const ctx = this.ctx;
      const Wc = c.width / dpr;
      const Hc = c.height / dpr;
      ctx.fillStyle = "#000";
      ctx.fillRect(0, 0, Wc, Hc);
      const { scale, ox, oy } = this._computeScaleOffset(Wc, Hc);
      ctx.imageSmoothingEnabled = true;
      ctx.imageSmoothingQuality = "high";
      ctx.drawImage(bmp, ox, oy, this.frameW * scale, this.frameH * scale);
      this._drawCursor(ctx, scale, ox, oy);
    }
    _drawCursor(ctx, scale, ox, oy) {
      if (!this.cursorVisible || this.cursorX < 0 || this.cursorY < 0) return;
      if (this.srcW <= 0 || this.srcH <= 0 || this.frameW <= 0 || this.frameH <= 0) return;
      const px = this.cursorX * this.frameW / this.srcW * scale + ox;
      const py = this.cursorY * this.frameH / this.srcH * scale + oy;
      ctx.save();
      ctx.shadowColor = "rgba(255,120,0,0.95)";
      ctx.shadowBlur = 14;
      ctx.beginPath();
      ctx.arc(px, py, 10, 0, Math.PI * 2);
      ctx.strokeStyle = "rgba(255,180,40,0.95)";
      ctx.lineWidth = 3;
      ctx.stroke();
      ctx.shadowBlur = 0;
      ctx.strokeStyle = "rgba(255,255,255,0.95)";
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(px - 14, py);
      ctx.lineTo(px + 14, py);
      ctx.moveTo(px, py - 14);
      ctx.lineTo(px, py + 14);
      ctx.stroke();
      ctx.fillStyle = "rgba(255,80,20,0.95)";
      ctx.beginPath();
      ctx.arc(px, py, 4, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    }
    _toggleStartMenu() {
      if (!this.startMenuEl) return;
      if (this.startMenuEl.style.display === "block") {
        this._hideStartMenu();
      } else {
        this.startMenuEl.style.display = "block";
        void this._refreshProcessList();
      }
    }
    _hideStartMenu() {
      if (this.startMenuEl) this.startMenuEl.style.display = "none";
    }
    _buildStartMenuContent() {
      const el = this.startMenuEl;
      if (!el) return;
      el.innerHTML = "";
      const mk = (label, onClick) => {
        const b = document.createElement("div");
        b.textContent = label;
        Object.assign(b.style, { padding: "6px 10px", cursor: "pointer", borderRadius: "3px" });
        b.onmouseenter = () => {
          b.style.background = "#2f5f9f";
        };
        b.onmouseleave = () => {
          b.style.background = "";
        };
        b.onclick = (ev) => {
          ev.stopPropagation();
          onClick();
        };
        return b;
      };
      const header = (t) => {
        const h = document.createElement("div");
        h.textContent = t;
        Object.assign(h.style, { padding: "4px 10px", color: "#888", fontSize: "10px", textTransform: "uppercase", letterSpacing: "0.5px" });
        return h;
      };
      el.appendChild(header("\u8FD0\u884C\u4E2D\u7A0B\u5E8F"));
      const list = document.createElement("div");
      list.id = "vdesk-start-menu-procs";
      list.textContent = "\uFF08\u52A0\u8F7D\u4E2D\u2026\uFF09";
      Object.assign(list.style, { padding: "4px 10px", color: "#aaa", fontSize: "11px" });
      el.appendChild(list);
      el.appendChild(header("\u5E38\u7528\u7A0B\u5E8F"));
      el.appendChild(mk("\u8BB0\u4E8B\u672C", () => this._launchExe("C:\\Windows\\System32\\notepad.exe")));
      el.appendChild(mk("\u8D44\u6E90\u7BA1\u7406\u5668", () => this._launchExe("C:\\Windows\\explorer.exe")));
      el.appendChild(mk("\u547D\u4EE4\u63D0\u793A\u7B26", () => this._launchExe("C:\\Windows\\System32\\cmd.exe")));
      el.appendChild(header("\u64CD\u4F5C"));
      el.appendChild(mk("\u5173\u95ED\u6B64\u865A\u62DF\u684C\u9762", () => this._closeDesktop()));
    }
    async _refreshProcessList() {
      const el = this.startMenuEl;
      if (!el) return;
      const listEl = el.querySelector("#vdesk-start-menu-procs");
      if (!listEl) return;
      listEl.textContent = "\uFF08\u52A0\u8F7D\u4E2D\u2026\uFF09";
      try {
        const url = this.frameUrl.replace("/vdesk/frame", "/vdesk/processes");
        const r = await fetch(url + "?desktop=" + encodeURIComponent(this.desktop) + "&t=" + Date.now(), { cache: "no-store" });
        if (!r.ok) {
          listEl.textContent = "\uFF08HTTP " + r.status + "\uFF09";
          return;
        }
        const j = await r.json();
        const procs = Array.isArray(j.processes) ? j.processes : [];
        if (procs.length === 0) {
          listEl.textContent = "\uFF08\u65E0\uFF09";
          return;
        }
        listEl.innerHTML = "";
        for (const p of procs) {
          const item = document.createElement("div");
          Object.assign(item.style, { padding: "6px 10px", cursor: "pointer", borderRadius: "3px", display: "flex", justifyContent: "space-between", alignItems: "center", gap: "8px" });
          const label = document.createElement("span");
          const _cnMap = { CabinetWClass: "\u8D44\u6E90\u7BA1\u7406\u5668" };
          const _disp = p.title || _cnMap[p.class || ""] || p.class || "(\u65E0\u6807\u9898)";
          label.textContent = _disp + (p.iconic ? " [\u6700\u5C0F\u5316]" : "");
          label.style.flex = "1";
          label.style.overflow = "hidden";
          label.style.textOverflow = "ellipsis";
          label.style.whiteSpace = "nowrap";
          const xbtn = document.createElement("span");
          xbtn.textContent = "\xD7";
          Object.assign(xbtn.style, { padding: "2px 6px", borderRadius: "3px", color: "#ccc", fontWeight: "bold", cursor: "pointer" });
          xbtn.onmouseenter = () => {
            xbtn.style.background = "#a33";
            xbtn.style.color = "#fff";
          };
          xbtn.onmouseleave = () => {
            xbtn.style.background = "";
            xbtn.style.color = "#ccc";
          };
          xbtn.onclick = (ev) => {
            ev.stopPropagation();
            if (xbtn._busy) return;
            xbtn._busy = true;
            xbtn.style.opacity = "0.4";
            window.setTimeout(() => {
              xbtn._busy = false;
              xbtn.style.opacity = "";
            }, 400);
            void this._closeHwnd(p.hwnd, (p.title || p.class || "hwnd=" + p.hwnd).slice(0, 24));
          };
          item.appendChild(label);
          item.appendChild(xbtn);
          item.onmouseenter = () => {
            item.style.background = "#2f5f9f";
          };
          item.onmouseleave = () => {
            item.style.background = "";
          };
          item.onclick = (ev) => {
            ev.stopPropagation();
            this._activateHwnd(p.hwnd);
          };
          listEl.appendChild(item);
        }
      } catch (e) {
        listEl.textContent = "\uFF08\u9519\u8BEF " + String(e).slice(0, 40) + "\uFF09";
      }
    }
    async _launchExe(exe) {
      try {
        const url = this.frameUrl.replace("/vdesk/frame", "/vdesk/launch");
        await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ desktop: this.desktop, exe })
        });
        this._hideStartMenu();
      } catch {
      }
    }
    async _closeHwnd(hwnd, label = "\u7A97") {
      const setStatus = (msg, ttlMs) => {
        if (!this.statusInputEl) return;
        this.statusInputEl.textContent = msg;
        window.setTimeout(() => {
          if (this.statusInputEl && this.statusInputEl.textContent === msg) {
            this.statusInputEl.textContent = "";
          }
        }, ttlMs);
      };
      const attempt = (url, timeoutMs) => {
        const ac = new AbortController();
        const timer = window.setTimeout(() => ac.abort(), timeoutMs);
        return (async () => {
          try {
            const r = await fetch(url, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ desktop: this.desktop, hwnd }),
              signal: ac.signal
            });
            if (!r.ok) return { ok: false, timeout: false, status: r.status };
            let j = {};
            try {
              j = await r.json();
            } catch {
            }
            return { ok: j.ok !== false, timeout: false, status: r.status };
          } catch (e) {
            return { ok: false, timeout: e && e.name === "AbortError", status: void 0 };
          } finally {
            window.clearTimeout(timer);
          }
        })();
      };
      let res = await attempt(this.frameUrl.replace("/vdesk/frame", "/vdesk/close_window"), 8e3);
      let viaPort = 0;
      if (!res.ok) {
        try {
          const cur = new URL(this.frameUrl);
          let tried = 0;
          for (let port = 8790; port <= 8799 && tried < 3; port++) {
            if (String(port) === cur.port) continue;
            const altBase = `${cur.protocol}//${cur.hostname}:${port}`;
            const pac = new AbortController();
            const ptimer = window.setTimeout(() => pac.abort(), 1500);
            let alive = false;
            try {
              const pr = await fetch(`${altBase}/vdesk/desktops`, { cache: "no-store", signal: pac.signal });
              alive = pr.ok;
            } catch {
              alive = false;
            } finally {
              window.clearTimeout(ptimer);
            }
            if (!alive) continue;
            tried++;
            const r2 = await attempt(`${altBase}/vdesk/close_window`, 3e3);
            if (r2.ok) {
              res = r2;
              viaPort = port;
              break;
            }
          }
        } catch {
        }
      }
      if (res.ok) {
        setStatus(viaPort ? `\u5DF2\u5173\u95ED ${label}\uFF08\u5907\u7528\u7AEF\u53E3 ${viaPort}\uFF09` : `\u5DF2\u5173\u95ED ${label}`, 3e3);
      } else if (res.timeout) {
        setStatus(`\u5173\u95ED\u5931\u8D25 \u8D85\u65F6(8s)${viaPort ? "" : "\uFF08\u5907\u7528\u7AEF\u53E3\u4EA6\u5931\u8D25\uFF09"}`, 5e3);
      } else {
        setStatus(`\u5173\u95ED\u5931\u8D25 HTTP ${res.status ?? "?"}\uFF08\u5907\u7528\u7AEF\u53E3\u4EA6\u5931\u8D25\uFF09`, 5e3);
      }
      setTimeout(() => {
        void this._refreshProcessList();
      }, 400);
    }
    async _activateHwnd(hwnd) {
      try {
        const url = this.frameUrl.replace("/vdesk/frame", "/vdesk/activate");
        await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ desktop: this.desktop, hwnd })
        });
        this._hideStartMenu();
      } catch {
      }
    }
    async _closeDesktop() {
      try {
        const url = this.frameUrl.replace("/vdesk/frame", "/vdesk/close_desktop");
        await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ desktop: this.desktop })
        });
        this._hideStartMenu();
      } catch {
      }
    }
    _installStartMenuKeys() {
      window.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && this.startMenuEl && this.startMenuEl.style.display !== "none") {
          this._hideStartMenu();
        }
      });
    }
    _onClick(ev) {
      if (!this.currentBitmap) return;
      const rect = this.canvas.getBoundingClientRect();
      const cx = ev.clientX - rect.left;
      const cy = ev.clientY - rect.top;
      const dpr = window.devicePixelRatio || 1;
      const { scale, ox, oy } = this._computeScaleOffset(
        this.canvas.width / dpr,
        this.canvas.height / dpr
      );
      let fx = (cx - ox) / scale;
      let fy = (cy - oy) / scale;
      fx = Math.max(0, Math.min(this.frameW - 1, fx));
      fy = Math.max(0, Math.min(this.frameH - 1, fy));
      this.cursorX = Math.round(fx * (this.srcW || this.frameW) / (this.frameW || 1));
      this.cursorY = Math.round(fy * (this.srcH || this.frameH) / (this.frameH || 1));
      this.cursorVisible = true;
      this._redrawCurrent();
      const sxHit = fx * (this.srcW || this.frameW) / (this.frameW || 1);
      const syHit = fy * (this.srcH || this.frameH) / (this.frameH || 1);
      if (sxHit < 48 && syHit >= 1030) {
        this._toggleStartMenu();
        return;
      }
      if (this.startMenuEl && this.startMenuEl.style.display !== "none") {
        this._hideStartMenu();
      }
      const payload = {
        desktop: this.desktop,
        kind: "click",
        x: Math.round(fx),
        y: Math.round(fy),
        frameWidth: this.frameW,
        frameHeight: this.frameH,
        srcWidth: this.srcW,
        srcHeight: this.srcH
      };
      const on = ev.button === 2;
      if (on) payload.button = "right";
      fetch(this.inputUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      }).catch(() => {
      });
    }
    _buildInputBar() {
      const bar = document.createElement("div");
      bar.style.cssText = [
        "display:flex",
        "gap:6px",
        "padding:6px 8px",
        "background:#1a1a1a",
        "border-top:1px solid #2a2a2a",
        "flex:0 0 auto"
      ].join(";");
      const input = document.createElement("input");
      input.type = "text";
      input.placeholder = "\u8F93\u5165\u5230\u865A\u62DF\u684C\u9762\uFF08Enter \u53D1\u9001\uFF09";
      input.style.cssText = [
        "flex:1 1 auto",
        "min-width:0",
        "background:#0f0f0f",
        "color:#e0e0e0",
        "border:1px solid #333",
        "border-radius:3px",
        "padding:4px 8px",
        "font:13px system-ui,sans-serif",
        "outline:none"
      ].join(";");
      const btn = document.createElement("button");
      btn.textContent = "\u53D1\u9001";
      btn.style.cssText = [
        "flex:0 0 auto",
        "background:#2a4d7a",
        "color:#fff",
        "border:none",
        "border-radius:3px",
        "padding:4px 12px",
        "cursor:pointer",
        "font:13px system-ui,sans-serif"
      ].join(";");
      const doSend = async () => {
        const text = input.value;
        if (!text) return;
        input.value = "";
        let payload = text;
        if (this.appendMode) {
          try {
            const rr = await fetch(this.inputUrl, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ desktop: this.desktop, kind: "read_text" })
            });
            const rj = await rr.json().catch(() => ({}));
            const oldText = rj && rj.ok && rj.source === "child_edit" && rj.text ? String(rj.text) : "";
            payload = oldText ? oldText + "\r\n" + text : text;
          } catch (_e) {
            payload = text;
          }
        }
        try {
          const r = await fetch(this.inputUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              desktop: this.desktop,
              kind: "type",
              text: payload
            })
          });
          const j = await r.json().catch(() => ({}));
          if (!r.ok || !j.ok) {
            this.lastInputStatusTs = Date.now();
            if (this.statusInputEl) this.statusInputEl.textContent = `\u8F93\u5165\u5931\u8D25 HTTP ${r.status}`;
          } else {
            this.lastInputStatusTs = Date.now();
            if (this.statusInputEl) this.statusInputEl.textContent = `\u8F93\u5165 OK (${text.length} \u5B57\u7B26)`;
            window.setTimeout(() => {
              if (this.statusInputEl) this.statusInputEl.textContent = "";
            }, 3e3);
          }
        } catch (e) {
          this.lastInputStatusTs = Date.now();
          if (this.statusInputEl) this.statusInputEl.textContent = `\u8F93\u5165\u9519\u8BEF ${e.message}`;
        }
      };
      input.addEventListener("keydown", (e) => {
        e.stopPropagation();
        if (e.key === "Enter") {
          e.preventDefault();
          doSend();
        }
      });
      input.addEventListener("pointerdown", (e) => e.stopPropagation());
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        doSend();
      });
      bar.appendChild(input);
      bar.appendChild(btn);
      const cb = document.createElement("label");
      cb.style.cssText = [
        "display:flex",
        "align-items:center",
        "gap:4px",
        "color:#b0b0b0",
        "font:12px system-ui,sans-serif",
        "flex:0 0 auto",
        "cursor:pointer"
      ].join(";");
      const cbox = document.createElement("input");
      cbox.type = "checkbox";
      cbox.checked = true;
      cbox.style.cssText = "margin:0;cursor:pointer;";
      cbox.addEventListener("change", () => {
        this.appendMode = cbox.checked;
      });
      const cbl = document.createElement("span");
      cbl.textContent = "\u8FFD\u52A0";
      cb.appendChild(cbox);
      cb.appendChild(cbl);
      bar.insertBefore(cb, btn);
      this.root.appendChild(bar);
      this.inputEl = input;
    }
  };

  // src/client/launcher.ts
  var positionKey = "dsh-vdesktop.launcher-position";
  function attachLauncher(button, toggle) {
    let position = null;
    let drag = null;
    let moved = false;
    function place(point) {
      const { width, height } = button.getBoundingClientRect();
      const x = Math.max(0, Math.min(point.x, window.innerWidth - width));
      const y = Math.max(0, Math.min(point.y, window.innerHeight - height));
      Object.assign(button.style, { left: x + "px", top: y + "px", right: "auto", bottom: "auto" });
      position = { x, y };
    }
    try {
      const saved = JSON.parse(localStorage.getItem(positionKey) ?? "null");
      if (Number.isFinite(saved?.x) && Number.isFinite(saved?.y)) place(saved);
    } catch {
    }
    function onDown(event) {
      if (drag || !event.isPrimary || event.button !== 0) return;
      const rect = button.getBoundingClientRect();
      drag = { id: event.pointerId, start: { x: event.clientX, y: event.clientY }, origin: rect };
      moved = false;
      button.setPointerCapture(event.pointerId);
    }
    function onMove(event) {
      if (drag?.id !== event.pointerId) return;
      const dx = event.clientX - drag.start.x;
      const dy = event.clientY - drag.start.y;
      if (!moved && Math.hypot(dx, dy) < 5) return;
      moved = true;
      button.style.cursor = "grabbing";
      place({ x: drag.origin.x + dx, y: drag.origin.y + dy });
    }
    function onEnd(event) {
      if (drag?.id !== event.pointerId) return;
      if (event.type === "pointerup") onMove(event);
      drag = null;
      button.style.cursor = "grab";
      if (moved && position) {
        try {
          localStorage.setItem(positionKey, JSON.stringify(position));
        } catch {
        }
      }
    }
    function onClick(event) {
      if (!moved || event.detail === 0) toggle();
    }
    function constrain() {
      if (position) place(position);
    }
    const events = new AbortController();
    const options = { signal: events.signal };
    button.addEventListener("pointerdown", onDown, options);
    button.addEventListener("pointermove", onMove, options);
    button.addEventListener("pointerup", onEnd, options);
    button.addEventListener("pointercancel", onEnd, options);
    button.addEventListener("lostpointercapture", onEnd, options);
    button.addEventListener("click", onClick, options);
    window.addEventListener("resize", constrain, options);
    const observer = new ResizeObserver(constrain);
    observer.observe(button);
    return () => {
      events.abort();
      observer.disconnect();
    };
  }

  // src/client/index.ts
  var FRAME_PORTS = Array.from({ length: 10 }, (_, i) => 8790 + i);
  async function probeFrameServer() {
    for (const port of FRAME_PORTS) {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 1500);
      try {
        const r = await fetch(`http://127.0.0.1:${port}/vdesk/desktops`, {
          cache: "no-store",
          signal: ctrl.signal
        });
        if (!r.ok) continue;
        const j = await r.json();
        return { port, desktops: Array.isArray(j.desktops) ? j.desktops : [] };
      } catch {
      } finally {
        clearTimeout(timer);
      }
    }
    return null;
  }
  function install(loader) {
    loader.load({
      id: "dsh-vdesktop",
      factory: (require2) => {
        const React = require2("react");
        const h = React.createElement;
        return {
          inject: ["slots", "locale"],
          apply(ctx) {
            ctx.effect(
              () => ctx.locale.register("vdesktop", {
                zh: {
                  toggle: "\u865A\u62DF\u684C\u9762",
                  open: "\u5355\u51FB\u5F00\u5173\u865A\u62DF\u684C\u9762\uFF0C\u62D6\u52A8\u53EF\u79FB\u52A8\u6309\u94AE",
                  close: "\u5173\u95ED",
                  refresh: "\u5237\u65B0\u684C\u9762\u5217\u8868",
                  noDesktop: "\uFF08\u65E0\u865A\u62DF\u684C\u9762\uFF0C\u53EF\u5148\u7528 vdesk_desktop_create \u521B\u5EFA\uFF09"
                },
                en: {
                  toggle: "Virtual Desktop",
                  open: "Click to toggle Virtual Desktop, or drag to move the button",
                  close: "Close",
                  refresh: "Refresh desktop list",
                  noDesktop: "(no virtual desktop; create one via vdesk_desktop_create)"
                }
              })
            );
            ctx.slots.inject(
              "shell.overlay",
              () => ctx.slots.register(
                { name: "shell.overlay", id: "dsh-vdesktop", locale: "vdesktop" },
                ({ t }) => {
                  const [open, setOpen] = React.useState(false);
                  const [info, setInfo] = React.useState(null);
                  const [desktop, setDesktop] = React.useState("");
                  const [reprobe, setReprobe] = React.useState(0);
                  const launcherRef = React.useRef(null);
                  const rootRef = React.useRef(null);
                  const panelRef = React.useRef(null);
                  const resizeHandleRef = React.useRef(null);
                  React.useEffect(() => {
                    const button = launcherRef.current;
                    if (button) return attachLauncher(button, () => setOpen((value) => !value));
                  }, []);
                  React.useEffect(() => {
                    if (!open) return;
                    let cancel = false;
                    void probeFrameServer().then((r) => {
                      if (cancel) return;
                      setInfo(r);
                      if (r && r.desktops.length > 0 && !desktop) {
                        setDesktop(r.desktops[0]);
                      }
                    });
                    return () => {
                      cancel = true;
                    };
                  }, [open, reprobe]);
                  React.useEffect(() => {
                    if (!open || !info || !desktop) return;
                    const el = rootRef.current;
                    if (!el) return;
                    const base = `http://127.0.0.1:${info.port}`;
                    const panel = new VDesktopPanel(el, {
                      desktop,
                      frameUrl: `${base}/vdesk/frame`,
                      inputUrl: `${base}/vdesk/input`,
                      scaleMode: "contain",
                      pollMinMs: 200
                    });
                    panel.start();
                    return () => panel.destroy();
                  }, [open, info, desktop]);
                  React.useEffect(() => {
                    if (!open) return;
                    const handle = resizeHandleRef.current;
                    const panel = panelRef.current;
                    if (!handle || !panel) return;
                    let dragging = false;
                    let sx = 0, sy = 0, sw = 0, sh = 0;
                    const onDown = (e) => {
                      dragging = true;
                      sx = e.clientX;
                      sy = e.clientY;
                      sw = panel.offsetWidth;
                      sh = panel.offsetHeight;
                      try {
                        handle.setPointerCapture(e.pointerId);
                      } catch {
                      }
                      e.preventDefault();
                      e.stopPropagation();
                    };
                    const onMove = (e) => {
                      if (!dragging) return;
                      const vw = window.innerWidth;
                      const vh = window.innerHeight;
                      const marginRight = 24;
                      const marginBottom = 64;
                      const marginLeftMin = 8;
                      const marginTopMin = 8;
                      const maxW = Math.max(240, vw - marginRight - marginLeftMin);
                      const maxH = Math.max(160, vh - marginBottom - marginTopMin);
                      const w = Math.max(240, Math.min(sw - (e.clientX - sx), maxW));
                      const h2 = Math.max(160, Math.min(sh - (e.clientY - sy), maxH));
                      panel.style.width = `${w}px`;
                      panel.style.height = `${h2}px`;
                    };
                    const onUp = (e) => {
                      dragging = false;
                      try {
                        handle.releasePointerCapture(e.pointerId);
                      } catch {
                      }
                    };
                    handle.addEventListener("pointerdown", onDown);
                    handle.addEventListener("pointermove", onMove);
                    handle.addEventListener("pointerup", onUp);
                    handle.addEventListener("pointercancel", onUp);
                    return () => {
                      handle.removeEventListener("pointerdown", onDown);
                      handle.removeEventListener("pointermove", onMove);
                      handle.removeEventListener("pointerup", onUp);
                      handle.removeEventListener("pointercancel", onUp);
                    };
                  }, [open]);
                  return h(
                    "div",
                    {
                      style: {
                        position: "fixed",
                        inset: 0,
                        pointerEvents: "none",
                        zIndex: 2147483e3
                      }
                    },
                    h(
                      "button",
                      {
                        type: "button",
                        title: t("open"),
                        ref: launcherRef,
                        style: {
                          position: "fixed",
                          right: 24,
                          bottom: 24,
                          pointerEvents: "auto",
                          padding: "8px 14px",
                          background: "#1b1b1b",
                          color: "#ddd",
                          border: "1px solid #444",
                          borderRadius: 6,
                          cursor: "grab",
                          touchAction: "none",
                          userSelect: "none",
                          font: "13px/1.4 system-ui"
                        }
                      },
                      `\u{1F5A5} ${t("toggle")}`
                    ),
                    open && h(
                      "div",
                      {
                        ref: panelRef,
                        style: {
                          position: "fixed",
                          right: 24,
                          bottom: 64,
                          width: 720,
                          height: 480,
                          overflow: "hidden",
                          border: "1px solid #444",
                          background: "#111",
                          pointerEvents: "auto",
                          display: "flex",
                          flexDirection: "column"
                        }
                      },
                      h(
                        "div",
                        {
                          style: {
                            display: "flex",
                            gap: 8,
                            alignItems: "center",
                            padding: "4px 6px",
                            background: "#1b1b1b",
                            borderBottom: "1px solid #333",
                            color: "#ddd",
                            font: "12px/1.4 system-ui"
                          }
                        },
                        info && info.desktops.length > 0 ? h(
                          "select",
                          {
                            value: desktop,
                            onChange: (e) => setDesktop(e.target.value),
                            style: {
                              background: "#222",
                              color: "#ddd",
                              border: "1px solid #444",
                              borderRadius: 3,
                              padding: "2px 4px"
                            }
                          },
                          info.desktops.map(
                            (d) => h("option", { key: d, value: d }, d)
                          )
                        ) : h("span", { style: { color: "#888" } }, t("noDesktop")),
                        h(
                          "button",
                          {
                            type: "button",
                            title: t("refresh"),
                            onClick: () => setReprobe((n) => n + 1),
                            style: {
                              background: "#333",
                              color: "#eee",
                              border: "1px solid #444",
                              borderRadius: 3,
                              padding: "2px 8px",
                              cursor: "pointer"
                            }
                          },
                          "\u27F3"
                        ),
                        h("span", { style: { flex: 1 } }),
                        h(
                          "button",
                          {
                            type: "button",
                            onClick: () => setOpen(false),
                            style: {
                              background: "#333",
                              color: "#eee",
                              border: "1px solid #444",
                              borderRadius: 3,
                              padding: "2px 8px",
                              cursor: "pointer"
                            }
                          },
                          `\u2715 ${t("close")}`
                        )
                      ),
                      // VDesktopPanel 挂载点（flex 1）
                      h("div", {
                        ref: rootRef,
                        style: {
                          flex: 1,
                          minHeight: 0,
                          minWidth: 0,
                          position: "relative"
                        }
                      }),
                      // 自定义缩放手柄（大脑 20b070b78ff7：迁移左上角方便拖拽；
                      // 用户裁决：光标 nwse-resize；斜纹保持 315deg 原样式）
                      h("div", {
                        ref: resizeHandleRef,
                        style: {
                          position: "absolute",
                          top: 0,
                          left: 0,
                          width: 18,
                          height: 18,
                          cursor: "nwse-resize",
                          pointerEvents: "auto",
                          zIndex: 2147483647,
                          background: "linear-gradient(315deg, transparent 50%, #666 50%)"
                        }
                      })
                    )
                  );
                }
              )
            );
          }
        };
      }
    });
  }
  if (typeof window !== "undefined" && window.__ModuleLoader__) {
    install(window.__ModuleLoader__);
  }
})();
//# sourceMappingURL=client.js.map
