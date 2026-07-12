#!/usr/bin/env python3
"""
Polymarket 跟单策略实时监控面板

读取 copy_state.json + CLOB 实时价格，浏览器打开即看：
- 累计盈亏大数字 + 累计盈亏曲线 + 最近平仓战绩
- 概率格盘：每笔平仓 = 一个小球，按盈亏落进对应柱（Galton board）
- 各 leader 跟单贡献对比
- 在跟持仓列表 + 实时价格 + 浮动盈亏

用法:
    python3 dashboard.py                # 读 copy_state.json，默认端口 8090
    DEMO=1 python3 dashboard.py         # 演示模式：模拟数据流，看动画效果
    COPY_STATE=/path/x.json PORT=9000 python3 dashboard.py
"""

import os
import json
import time
import random
import datetime
import threading
import requests
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE_FILE  = os.environ.get("COPY_STATE", "copy_state.json")
PORT        = int(os.environ.get("PORT", "8090"))
DEMO        = os.environ.get("DEMO", "0") == "1"
DAILY_LIMIT = float(os.environ.get("COPY_DAILY_LIMIT", "50"))
CLOB_HOST   = "https://clob.polymarket.com"

# ─── 实时价格缓存（30s TTL，避免每次刷新都打满 API）──────────────────────────
_price_cache: dict = {}
_price_lock = threading.Lock()

def get_live_price(token_id: str):
    now = time.time()
    with _price_lock:
        hit = _price_cache.get(token_id)
        if hit and now - hit[1] < 30:
            return hit[0]
    price = None
    try:
        r = requests.get(
            f"{CLOB_HOST}/midpoint",
            params={"token_id": token_id},
            timeout=4,
        )
        if r.ok:
            price = float(r.json().get("mid", 0)) or None
    except Exception:
        price = None
    with _price_lock:
        _price_cache[token_id] = (price, now)
    return price


# ═══════════════════════════════════════════════════════════════════════════════
# 真实数据：从 copy_state.json 汇总
# ═══════════════════════════════════════════════════════════════════════════════

def build_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        raw = {"positions": [], "performance": {}, "daily": {}, "leaders": {}}

    positions = raw.get("positions", [])
    perf      = raw.get("performance", {})
    closed    = sorted(
        [p for p in positions if p.get("status") == "closed"],
        key=lambda p: p.get("closed_at", ""),
    )
    open_pos  = [p for p in positions if p.get("status") == "open"]

    # 累计盈亏曲线
    series, cum = [], 0.0
    for p in closed:
        cum += p.get("realized_pnl", 0)
        series.append({"t": p.get("closed_at", "")[:16], "v": round(cum, 2)})

    # 各 leader 汇总
    agg = {}
    for p in positions:
        a = agg.setdefault(p.get("leader", "?"), {"copied": 0, "wins": 0, "losses": 0, "open": 0, "pnl": 0.0})
        a["copied"] += 1
        if p.get("status") == "closed":
            a["pnl"] = round(a["pnl"] + p.get("realized_pnl", 0), 2)
            if p.get("realized_pnl", 0) >= 0:
                a["wins"] += 1
            else:
                a["losses"] += 1
        else:
            a["open"] += 1
    leaders = [{"name": k, **v} for k, v in sorted(agg.items(), key=lambda x: -x[1]["pnl"])]

    # 在跟持仓 + 实时价
    open_rows = []
    for p in open_pos[:15]:
        cur = get_live_price(p.get("token_id", ""))
        upnl = round(p["shares"] * (cur - p["avg_price"]), 2) if cur else None
        open_rows.append({
            "title":     p.get("title", ""),
            "outcome":   p.get("outcome", ""),
            "leader":    p.get("leader", ""),
            "shares":    p.get("shares", 0),
            "avg_price": p.get("avg_price", 0),
            "cur_price": cur,
            "cost":      p.get("cost_usdc", 0),
            "upnl":      upnl,
            "opened_at": p.get("opened_at", "")[:16],
        })

    dry = positions[-1].get("dry_run", True) if positions else True
    daily = raw.get("daily", {})
    return {
        "mode": "PAPER" if dry else "LIVE",
        "performance": {
            "realized_pnl": perf.get("realized_pnl", 0.0),
            "wins":         perf.get("wins", 0),
            "losses":       perf.get("losses", 0),
            "copied_buys":  perf.get("copied_buys", 0),
        },
        "daily": {"spent": daily.get("spent", 0.0), "limit": DAILY_LIMIT},
        "leaders": leaders,
        "closed_trades": [
            {
                "id":     f'{p.get("token_id","")[:10]}_{p.get("closed_at","")}',
                "title":  p.get("title", ""),
                "leader": p.get("leader", ""),
                "pnl":    round(p.get("realized_pnl", 0), 2),
            }
            for p in closed
        ],
        "open_positions": open_rows,
        "pnl_series": series,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 演示模式：模拟一个正在运行的跟单账户（仅用于看界面效果）
# ═══════════════════════════════════════════════════════════════════════════════

class DemoFeed:
    TITLES = [
        "Will BTC close above $75k this week?",
        "Fed rate cut announced before September?",
        "Will ETH flip $4,000 by end of month?",
        "US CPI print below 2.8% this quarter?",
        "Will SpaceX Starship reach orbit this month?",
        "Champions League: Real Madrid to win?",
        "Will TikTok divest US operations this year?",
        "OPEC+ extends production cuts?",
    ]
    LEADERS = ["鲸鱼A", "老练B", "事件C"]

    def __init__(self):
        random.seed(7)
        self.trades, self.cum = [], 0.0
        base = time.time() - 86400
        for i in range(28):
            self._close_one(base + i * 3000)
        self.open_pos = [self._new_open() for _ in range(4)]
        self.last_tick = time.time()

    def _close_one(self, ts=None):
        win = random.random() < 0.61
        pnl = round(random.uniform(0.4, 5.8), 2) if win else round(-random.uniform(0.4, 4.6), 2)
        self.cum = round(self.cum + pnl, 2)
        self.trades.append({
            "id":     f"demo_{len(self.trades)}",
            "title":  random.choice(self.TITLES),
            "leader": random.choice(self.LEADERS),
            "pnl":    pnl,
            "t":      datetime.datetime.utcfromtimestamp(ts or time.time()).isoformat()[:16],
            "cum":    self.cum,
        })

    def _new_open(self):
        avg = round(random.uniform(0.25, 0.75), 2)
        return {
            "title":     random.choice(self.TITLES),
            "outcome":   random.choice(["Yes", "No"]),
            "leader":    random.choice(self.LEADERS),
            "shares":    round(random.uniform(5, 30), 1),
            "avg_price": avg,
            "cur_price": avg,
            "cost":      0,
            "opened_at": datetime.datetime.utcnow().isoformat()[:16],
        }

    def state(self) -> dict:
        now = time.time()
        if now - self.last_tick > 7:          # 每 ~7 秒可能有一笔新平仓
            self.last_tick = now
            if random.random() < 0.75:
                self._close_one()
            if random.random() < 0.25 and len(self.open_pos) < 7:
                self.open_pos.append(self._new_open())
        for p in self.open_pos:               # 价格随机游走
            p["cur_price"] = round(min(0.97, max(0.03, p["cur_price"] + random.uniform(-0.012, 0.012))), 3)
            p["cost"] = round(p["shares"] * p["avg_price"], 2)
            p["upnl"] = round(p["shares"] * (p["cur_price"] - p["avg_price"]), 2)

        wins   = sum(1 for t in self.trades if t["pnl"] >= 0)
        losses = len(self.trades) - wins
        agg = {}
        for t in self.trades:
            a = agg.setdefault(t["leader"], {"copied": 0, "wins": 0, "losses": 0, "open": 0, "pnl": 0.0})
            a["copied"] += 1
            a["pnl"] = round(a["pnl"] + t["pnl"], 2)
            a["wins" if t["pnl"] >= 0 else "losses"] += 1
        for p in self.open_pos:
            agg.setdefault(p["leader"], {"copied": 0, "wins": 0, "losses": 0, "open": 0, "pnl": 0.0})
            agg[p["leader"]]["open"] += 1
            agg[p["leader"]]["copied"] += 1

        return {
            "mode": "DEMO",
            "performance": {
                "realized_pnl": self.cum,
                "wins": wins, "losses": losses,
                "copied_buys": len(self.trades) + len(self.open_pos),
            },
            "daily": {"spent": round(sum(abs(t["pnl"]) for t in self.trades[-6:]), 2), "limit": DAILY_LIMIT},
            "leaders": [{"name": k, **v} for k, v in sorted(agg.items(), key=lambda x: -x[1]["pnl"])],
            "closed_trades": [{k: t[k] for k in ("id", "title", "leader", "pnl")} for t in self.trades],
            "open_positions": self.open_pos,
            "pnl_series": [{"t": t["t"], "v": t["cum"]} for t in self.trades],
        }


_demo = DemoFeed() if DEMO else None


# ═══════════════════════════════════════════════════════════════════════════════
# 前端页面（自包含，无外部依赖）
# ═══════════════════════════════════════════════════════════════════════════════

HTML = r'''<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>跟单监控 · Copy Trader</title>
<style>
:root{
  --plane:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --baseline:#c3c2b7; --ring:rgba(11,11,11,.10);
  --up:#2a78d6; --up-wash:rgba(42,120,214,.10); --dn:#e34948; --dn-wash:rgba(227,73,72,.08);
  --dn-ink:#d03b3b; --good:#0ca30c; --warn:#eda100;
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--plane);color:var(--ink);font-family:var(--mono);font-size:13px;
     max-width:1060px;margin:0 auto;padding:14px 16px 40px}
.panel{background:var(--surface);border:1px solid var(--ring);border-radius:12px;
       padding:16px 18px;margin-bottom:14px;box-shadow:0 1px 2px rgba(11,11,11,.04)}
.hdr{display:flex;align-items:center;gap:12px;padding:10px 4px 14px;flex-wrap:wrap}
.hdr .logo{width:34px;height:34px;border:1.5px solid var(--ink);border-radius:50%;
           display:flex;align-items:center;justify-content:center;font-size:15px}
.hdr .t1{font-size:10px;letter-spacing:.14em;color:var(--ink2);text-transform:uppercase}
.hdr .t2{font-size:17px;font-weight:700;letter-spacing:.04em}
.hdr .sp{flex:1}
.badge{border:1px solid var(--ring);border-radius:6px;padding:4px 10px;font-size:10px;
       letter-spacing:.1em;display:flex;align-items:center;gap:6px}
.badge .dot{width:7px;height:7px;border-radius:50%}
.badge.live .dot{background:var(--good)} .badge.live{color:var(--good)}
.badge.paper .dot{background:var(--up)} .badge.paper{color:var(--up)}
.badge.demo .dot{background:var(--warn)} .badge.demo{color:#8a5c00;border-color:#eda10055;background:#eda1000f}
.clock{font-size:12px;color:var(--ink2);letter-spacing:.08em}
.lbl{font-size:9px;letter-spacing:.16em;color:var(--muted);text-transform:uppercase;margin-bottom:6px}
.row{display:grid;gap:14px}
@media(min-width:840px){.row.two{grid-template-columns:7fr 5fr}}
.hero-num{font-size:44px;font-weight:700;letter-spacing:-.01em;line-height:1.05;font-variant-numeric:tabular-nums}
.hero-num.up{color:var(--up)} .hero-num.dn{color:var(--dn-ink)}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.chip{border:1px solid var(--ring);border-radius:6px;padding:4px 9px;font-size:11px;color:var(--ink2)}
.chip b{color:var(--ink);font-variant-numeric:tabular-nums}
.recent{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin-top:14px}
.rc{border:1px solid var(--ring);border-radius:8px;padding:8px 10px}
.rc .v{font-size:16px;font-weight:700;font-variant-numeric:tabular-nums}
.rc .v.up{color:var(--up)} .rc .v.dn{color:var(--dn-ink)}
.rc .m{font-size:10px;color:var(--muted);margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
canvas{display:block;width:100%}
.split{display:grid;gap:16px}
@media(min-width:840px){.split{grid-template-columns:170px 1fr}}
.kv{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px dashed var(--grid);font-size:12px}
.kv:last-child{border-bottom:0}
.kv .k{color:var(--ink2)} .kv .v{font-weight:700;font-variant-numeric:tabular-nums}
.kv .v.up{color:var(--up)} .kv .v.dn{color:var(--dn-ink)}
.note{font-size:10px;color:var(--muted);line-height:1.7;margin-top:10px}
.leader-row{display:grid;grid-template-columns:150px 1fr 80px;gap:12px;align-items:center;padding:9px 0;
            border-bottom:1px solid var(--grid)}
.leader-row:last-child{border-bottom:0}
.leader-name{display:flex;align-items:center;gap:8px;font-size:12px;overflow:hidden}
.leader-name .dot{width:9px;height:9px;border-radius:50%;flex:none}
.leader-name .sub{font-size:10px;color:var(--muted)}
.bar-track{position:relative;height:20px}
.bar-track .zero{position:absolute;left:50%;top:-3px;bottom:-3px;width:1px;background:var(--baseline)}
.bar{position:absolute;top:3px;height:14px}
.bar.pos{left:50%;background:var(--up);border-radius:0 4px 4px 0}
.bar.neg{right:50%;background:var(--dn);border-radius:4px 0 0 4px}
.leader-pnl{text-align:right;font-weight:700;font-variant-numeric:tabular-nums;font-size:12px}
.leader-pnl.up{color:var(--up)} .leader-pnl.dn{color:var(--dn-ink)}
table{width:100%;border-collapse:collapse;font-size:12px}
th{font-size:9px;letter-spacing:.14em;color:var(--muted);text-transform:uppercase;text-align:left;
   padding:6px 8px;border-bottom:1px solid var(--grid)}
td{padding:8px;border-bottom:1px solid var(--grid);font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:0}
tr:hover td{background:var(--up-wash)}
td.num,th.num{text-align:right}
.up-t{color:var(--up);font-weight:700} .dn-t{color:var(--dn-ink);font-weight:700}
.empty{text-align:center;color:var(--muted);padding:26px;font-size:12px;line-height:1.8}
.foot{display:flex;gap:14px;flex-wrap:wrap;font-size:10px;color:var(--muted);padding:2px 6px}
.tip{position:fixed;pointer-events:none;background:var(--ink);color:#fff;font-size:11px;
     padding:6px 9px;border-radius:6px;z-index:9;display:none;font-variant-numeric:tabular-nums;line-height:1.5}
</style>
</head>
<body>

<div class="hdr">
  <div class="logo">🐑</div>
  <div>
    <div class="t1">Polymarket · Copy Trading · <span id="h-sub">-</span></div>
    <div class="t2">跟单监控 · COPY TRADER</div>
  </div>
  <div class="sp"></div>
  <div class="badge" id="badge"><span class="dot"></span><span id="badge-t">…</span></div>
  <div class="clock" id="clock">--:--:-- UTC</div>
</div>

<div class="row two">
  <div class="panel">
    <div class="lbl">● 已实现总盈亏 · Realized PnL</div>
    <div class="hero-num" id="pnl">$0.00</div>
    <div class="chips">
      <span class="chip">平仓 <b id="c-closed">0</b> 笔</span>
      <span class="chip">胜率 <b id="c-wr">–</b></span>
      <span class="chip">EV/笔 <b id="c-ev">–</b></span>
      <span class="chip">今日已用 <b id="c-daily">–</b></span>
    </div>
    <div class="recent" id="recent"></div>
  </div>
  <div class="panel">
    <div class="lbl">累计盈亏曲线 · Cumulative</div>
    <canvas id="spark" height="170"></canvas>
  </div>
</div>

<div class="panel">
  <div class="lbl">◆ 概率格盘 · 每笔平仓一个球，落进盈亏分布</div>
  <div class="split">
    <div>
      <div class="kv"><span class="k">BALLS DROPPED</span><span class="v" id="g-balls">0</span></div>
      <div class="kv"><span class="k">LANDED BLUE</span><span class="v" id="g-green">–</span></div>
      <div class="kv"><span class="k">EV / TRADE</span><span class="v" id="g-ev">–</span></div>
      <div class="kv"><span class="k">REALIZED</span><span class="v" id="g-pnl">–</span></div>
      <div class="note">大数定律 — 优势只需要重复。<br>左侧红柱为亏损区间，右侧蓝柱为盈利区间。</div>
    </div>
    <canvas id="galton" height="300"></canvas>
  </div>
</div>

<div class="panel">
  <div class="lbl">▲ LEADER 贡献对比 · 已实现盈亏</div>
  <div id="leaders"><div class="empty">暂无数据</div></div>
</div>

<div class="panel">
  <div class="lbl">◉ 在跟持仓 · 实时价格</div>
  <div id="positions"><div class="empty">暂无持仓</div></div>
</div>

<div class="foot">
  <span id="f-mode">–</span><span id="f-src">–</span><span id="f-upd">–</span><span>刷新间隔 10s</span>
</div>
<div class="tip" id="tip"></div>

<script>
const $ = id => document.getElementById(id);
const fmt$ = v => (v >= 0 ? "+$" : "-$") + Math.abs(v).toFixed(2);
const UP = "#2a78d6", DN = "#e34948", DN_INK = "#d03b3b",
      SLOTS = ["#2a78d6","#1baf7a","#eda100","#008300","#4a3aa7","#e34948","#e87ba4","#eb6834"];
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

setInterval(() => {
  $("clock").textContent = new Date().toISOString().slice(11,19) + " UTC";
}, 1000);

/* ── 概率格盘 ─────────────────────────────────────────────────────────── */
const BINS = [
  {lo:-1e9, hi:-5,  lab:"≤-5"}, {lo:-5, hi:-2, lab:"-5~-2"},
  {lo:-2, hi:-0.5, lab:"-2~-.5"}, {lo:-0.5, hi:0, lab:"-.5~0"},
  {lo:0, hi:0.5, lab:"0~.5"}, {lo:0.5, hi:2, lab:".5~2"},
  {lo:2, hi:5, lab:"2~5"}, {lo:5, hi:1e9, lab:">5"},
];
const binOf = p => BINS.findIndex(b => p >= b.lo && p < b.hi);
let counts = BINS.map(() => 0), balls = [], seen = new Set(), firstLoad = true;

function dropBall(pnl) {
  const bi = binOf(pnl);
  balls.push({bi, t: 0, jseed: Math.random() * 99, pnl});
}

function drawGalton() {
  const cv = $("galton"), dpr = devicePixelRatio || 1;
  const W = cv.clientWidth, H = 300;
  if (cv.width !== W * dpr) { cv.width = W * dpr; cv.height = H * dpr; }
  const g = cv.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, W, H);

  const padL = 10, padR = 10, binTop = H - 92, baseY = H - 26;
  const bw = (W - padL - padR) / BINS.length;

  /* 钉板 */
  g.fillStyle = "#c3c2b7";
  for (let r = 0; r < 6; r++) {
    const n = 5 + r * 2, y = 24 + r * 22;
    for (let i = 0; i < n; i++) {
      const x = W/2 + (i - (n-1)/2) * 26;
      if (x > padL && x < W - padR) { g.beginPath(); g.arc(x, y, 1.6, 0, 7); g.fill(); }
    }
  }
  /* 中线 */
  g.strokeStyle = "#c3c2b7"; g.setLineDash([3,4]); g.beginPath();
  g.moveTo(W/2, 12); g.lineTo(W/2, baseY); g.stroke(); g.setLineDash([]);
  g.fillStyle = "#898781"; g.font = "9px ui-monospace,monospace"; g.textAlign = "left";
  g.fillText("LOSS", padL + 2, 16);
  g.textAlign = "right"; g.fillText("PROFIT", W - padR - 2, 16);

  /* 柱 */
  const maxC = Math.max(1, ...counts);
  counts.forEach((c, i) => {
    const x = padL + i * bw + 2, w = bw - 4;
    const h = c === 0 ? 0 : Math.max(4, (binTop - 40) * c / maxC);
    g.fillStyle = i < 4 ? DN : UP;
    if (h > 0) {
      g.beginPath();
      g.roundRect(x, baseY - h, w, h, [4,4,0,0]);
      g.fill();
    }
    g.fillStyle = "#52514e"; g.font = "10px ui-monospace,monospace"; g.textAlign = "center";
    if (c > 0) g.fillText(c, x + w/2, baseY - h - 5);
    g.fillStyle = "#898781"; g.font = "8px ui-monospace,monospace";
    g.fillText(BINS[i].lab, x + w/2, baseY + 13);
  });
  /* 基线 */
  g.strokeStyle = "#c3c2b7"; g.beginPath(); g.moveTo(padL, baseY); g.lineTo(W - padR, baseY); g.stroke();

  /* 下落的球 */
  for (let i = balls.length - 1; i >= 0; i--) {
    const b = balls[i];
    b.t += 0.016;
    const dur = 1.35, p = Math.min(1, b.t / dur);
    const targetX = padL + b.bi * bw + bw/2;
    const x = W/2 + (targetX - W/2) * (p*p) + Math.sin(p * 22 + b.jseed) * 9 * (1 - p);
    const y = 10 + (baseY - 18) * (p*p*0.9 + p*0.1);
    g.fillStyle = b.bi < 4 ? DN : UP;
    g.strokeStyle = "#fcfcfb"; g.lineWidth = 2;
    g.beginPath(); g.arc(x, y, 4.4, 0, 7); g.fill(); g.stroke();
    if (p >= 1) { counts[b.bi]++; balls.splice(i, 1); }
  }
  requestAnimationFrame(drawGalton);
}
requestAnimationFrame(drawGalton);

/* ── 累计盈亏曲线（带十字线 tooltip）──────────────────────────────────── */
let sparkData = [];
function drawSpark(hoverX) {
  const cv = $("spark"), dpr = devicePixelRatio || 1;
  const W = cv.clientWidth, H = 170;
  if (cv.width !== W * dpr) { cv.width = W * dpr; cv.height = H * dpr; }
  const g = cv.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, W, H);
  if (sparkData.length < 2) {
    g.fillStyle = "#898781"; g.font = "11px ui-monospace,monospace";
    g.fillText("等待平仓数据…", 12, 30); return null;
  }
  const vs = sparkData.map(d => d.v);
  const lo = Math.min(0, ...vs), hi = Math.max(0.01, ...vs);
  const px = i => 6 + (W - 12) * i / (sparkData.length - 1);
  const py = v => 8 + (H - 30) * (1 - (v - lo) / (hi - lo));
  /* 零线 */
  g.strokeStyle = "#e1e0d9"; g.setLineDash([3,4]);
  g.beginPath(); g.moveTo(6, py(0)); g.lineTo(W - 6, py(0)); g.stroke(); g.setLineDash([]);
  /* 面积 + 线 */
  g.beginPath(); sparkData.forEach((d,i) => i ? g.lineTo(px(i), py(d.v)) : g.moveTo(px(i), py(d.v)));
  g.strokeStyle = UP; g.lineWidth = 2; g.stroke();
  g.lineTo(px(sparkData.length-1), py(0)); g.lineTo(px(0), py(0)); g.closePath();
  g.fillStyle = "rgba(42,120,214,.10)"; g.fill();
  /* 末端标注 */
  const last = sparkData[sparkData.length-1];
  g.fillStyle = last.v >= 0 ? UP : DN_INK; g.font = "bold 11px ui-monospace,monospace";
  g.textAlign = "right"; g.fillText(fmt$(last.v), W - 8, py(last.v) - 8);

  if (hoverX != null) {
    const i = Math.round((hoverX - 6) / (W - 12) * (sparkData.length - 1));
    if (i >= 0 && i < sparkData.length) {
      const d = sparkData[i];
      g.strokeStyle = "#898781"; g.beginPath(); g.moveTo(px(i), 6); g.lineTo(px(i), H - 20); g.stroke();
      g.fillStyle = "#fcfcfb"; g.strokeStyle = UP; g.lineWidth = 2;
      g.beginPath(); g.arc(px(i), py(d.v), 4.5, 0, 7); g.fill(); g.stroke();
      return {x: px(i), d};
    }
  }
  return null;
}
$("spark").addEventListener("mousemove", e => {
  const r = e.target.getBoundingClientRect();
  const hit = drawSpark(e.clientX - r.left);
  const tip = $("tip");
  if (hit) {
    tip.style.display = "block";
    tip.style.left = (e.clientX + 12) + "px"; tip.style.top = (e.clientY - 10) + "px";
    tip.innerHTML = esc(hit.d.t) + "<br><b>" + fmt$(hit.d.v) + "</b>";
  } else tip.style.display = "none";
});
$("spark").addEventListener("mouseleave", () => { $("tip").style.display = "none"; drawSpark(); });

/* ── 数据刷新 ─────────────────────────────────────────────────────────── */
let lastPnl = 0;
function animateNum(el, from, to) {
  const t0 = performance.now();
  (function step(t) {
    const p = Math.min(1, (t - t0) / 700), v = from + (to - from) * p;
    el.textContent = (v >= 0 ? "+$" : "-$") + Math.abs(v).toFixed(2);
    el.className = "hero-num " + (to >= 0 ? "up" : "dn");
    if (p < 1) requestAnimationFrame(step);
  })(t0);
}

async function refresh() {
  let s;
  try { s = await (await fetch("/api/state")).json(); }
  catch (e) { return; }

  /* 徽章 & 页脚 */
  const modes = {LIVE: ["live","● LIVE · MAINNET"], PAPER: ["paper","● PAPER · 纸面模式"], DEMO: ["demo","● DEMO · 演示数据"]};
  const [cls, txt] = modes[s.mode] || modes.PAPER;
  $("badge").className = "badge " + cls; $("badge-t").textContent = txt;
  $("h-sub").textContent = s.mode === "DEMO" ? "DEMO FEED" : (s.leaders.length + " LEADERS");
  $("f-mode").textContent = "模式: " + s.mode;
  $("f-src").textContent = s.mode === "DEMO" ? "数据源: 内置模拟器" : "数据源: copy_state.json";
  $("f-upd").textContent = "更新: " + new Date().toISOString().slice(11,19) + " UTC";

  /* 英雄区 */
  const P = s.performance, closed = P.wins + P.losses;
  animateNum($("pnl"), lastPnl, P.realized_pnl); lastPnl = P.realized_pnl;
  $("c-closed").textContent = closed;
  $("c-wr").textContent = closed ? (100 * P.wins / closed).toFixed(0) + "%" : "–";
  $("c-ev").textContent = closed ? fmt$(P.realized_pnl / closed) : "–";
  $("c-daily").textContent = "$" + s.daily.spent.toFixed(2) + "/" + s.daily.limit.toFixed(0);

  /* 最近 4 笔 */
  const rec = s.closed_trades.slice(-4).reverse();
  $("recent").innerHTML = rec.length ? rec.map(t =>
    `<div class="rc"><div class="v ${t.pnl >= 0 ? "up" : "dn"}">${fmt$(t.pnl)}</div>
     <div class="m" title="${esc(t.title)}">${esc(t.leader)} · ${esc(t.title)}</div></div>`).join("")
    : '<div class="empty" style="grid-column:1/-1">还没有平仓记录</div>';

  /* 格盘：新平仓 → 掉球 */
  if (firstLoad) {
    s.closed_trades.forEach(t => { seen.add(t.id); counts[binOf(t.pnl)]++; });
    /* 开屏掉几颗球活跃气氛（从已计数的柱里"借"出来重放）*/
    s.closed_trades.slice(-5).forEach((t, i) => {
      counts[binOf(t.pnl)]--;
      setTimeout(() => dropBall(t.pnl), 350 * i);
    });
    firstLoad = false;
  } else {
    s.closed_trades.forEach(t => {
      if (!seen.has(t.id)) { seen.add(t.id); dropBall(t.pnl); }
    });
  }
  $("g-balls").textContent = s.closed_trades.length;
  $("g-green").textContent = closed ? (100 * P.wins / closed).toFixed(1) + "%" : "–";
  $("g-ev").textContent = closed ? fmt$(P.realized_pnl / closed) : "–";
  const gp = $("g-pnl"); gp.textContent = fmt$(P.realized_pnl);
  gp.className = "v " + (P.realized_pnl >= 0 ? "up" : "dn");

  /* leaders */
  if (s.leaders.length) {
    const mx = Math.max(0.01, ...s.leaders.map(l => Math.abs(l.pnl)));
    $("leaders").innerHTML = s.leaders.map((l, i) => {
      const w = Math.abs(l.pnl) / mx * 50;
      const done = l.wins + l.losses;
      const wr = done ? (100 * l.wins / done).toFixed(0) + "%" : "–";
      return `<div class="leader-row" title="${esc(l.name)}: 跟 ${l.copied} 笔, 胜率 ${wr}, 在跟 ${l.open}">
        <div class="leader-name"><span class="dot" style="background:${SLOTS[i % 8]}"></span>
          <span>${esc(l.name)}<br><span class="sub">跟 ${l.copied} 笔 · 胜率 ${wr} · 在跟 ${l.open}</span></span></div>
        <div class="bar-track"><span class="zero"></span>
          <span class="bar ${l.pnl >= 0 ? "pos" : "neg"}" style="width:${w}%"></span></div>
        <div class="leader-pnl ${l.pnl >= 0 ? "up" : "dn"}">${fmt$(l.pnl)}</div>
      </div>`;
    }).join("");
  }

  /* 持仓表 */
  if (s.open_positions.length) {
    $("positions").innerHTML = `<div style="overflow-x:auto"><table>
      <tr><th>市场</th><th>方向</th><th>Leader</th><th class="num">持仓</th>
      <th class="num">成本价</th><th class="num">现价</th><th class="num">浮动盈亏</th></tr>` +
      s.open_positions.map(p => {
        const cur = p.cur_price != null ? p.cur_price.toFixed(3) : "—";
        const up  = p.upnl != null ? `<span class="${p.upnl >= 0 ? "up-t" : "dn-t"}">${fmt$(p.upnl)}</span>` : "—";
        return `<tr><td title="${esc(p.title)}">${esc(p.title.slice(0, 46))}</td>
          <td>${esc(p.outcome)}</td><td>${esc(p.leader)}</td>
          <td class="num">${p.shares}</td><td class="num">${p.avg_price.toFixed(3)}</td>
          <td class="num">${cur}</td><td class="num">${up}</td></tr>`;
      }).join("") + "</table></div>";
  } else {
    $("positions").innerHTML = '<div class="empty">暂无在跟持仓 — copy_trader 跑起来后这里会实时更新</div>';
  }

  sparkData = s.pnl_series;
  drawSpark();
}
refresh();
setInterval(refresh, 10000);
</script>
</body></html>'''


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP 服务
# ═══════════════════════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/state"):
            body = json.dumps(_demo.state() if _demo else build_state(), ensure_ascii=False).encode()
            ctype = "application/json"
        else:
            body = HTML.encode()
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    mode = "DEMO 演示模式" if DEMO else f"读取 {STATE_FILE}"
    print(f"🐑 跟单监控面板: http://localhost:{PORT}  ({mode})")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
