#!/usr/bin/env python3
"""
Polymarket AI Agent 监控面板
展示 Claude 的思考过程、交易决策、学习轨迹
"""

import subprocess
import datetime
import json
import re
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

MEMORY_FILE = '/root/polymarket-bot/agent_memory.json'

HTML = '''<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="30">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Polymarket AI Agent</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",monospace;background:#0d1117;color:#c9d1d9;padding:20px;max-width:1400px;margin:0 auto}
h1{color:#58a6ff;font-size:22px;margin-bottom:4px}
.subtitle{color:#8b949e;font-size:13px;margin-bottom:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:16px 0}
.card{background:#161b22;border-radius:10px;padding:16px;border:1px solid #30363d}
.card .val{font-size:30px;font-weight:700;line-height:1.1}
.card .lbl{font-size:12px;color:#8b949e;margin-top:4px}
.pos{color:#3fb950}.neg{color:#f85149}.neu{color:#58a6ff}.warn{color:#d29922}
.status-bar{background:#161b22;border-radius:8px;padding:12px 16px;margin:12px 0;border-left:4px solid #3fb950;display:flex;align-items:center;gap:12px}
.status-bar.bad{border-left-color:#f85149}
.dot{width:10px;height:10px;border-radius:50%;background:#3fb950}
.dot.bad{background:#f85149}
.section{background:#161b22;border-radius:10px;padding:20px;margin:16px 0;border:1px solid #30363d}
.section h3{color:#c9d1d9;font-size:14px;font-weight:600;margin-bottom:14px;text-transform:uppercase;letter-spacing:.5px}
.decision{background:#0d1117;border-radius:8px;padding:14px;margin:8px 0;border-left:4px solid #58a6ff}
.decision.win{border-left-color:#3fb950}
.decision.loss{border-left-color:#f85149}
.decision.pending{border-left-color:#d29922}
.decision .q{font-size:14px;font-weight:600;color:#c9d1d9;margin-bottom:8px}
.decision .meta{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:8px}
.tag{background:#21262d;border-radius:4px;padding:2px 8px;font-size:11px;color:#8b949e}
.tag.claude{color:#58a6ff;background:#0d2149}
.tag.market{color:#d29922;background:#271d0d}
.tag.yes{color:#3fb950;background:#0d2611}
.tag.no{color:#f85149;background:#1e0d0d}
.tag.skip{color:#8b949e}
.reasoning{font-size:12px;color:#8b949e;line-height:1.6;margin-top:6px}
.factors{margin-top:6px}
.factor{font-size:11px;color:#58a6ff;background:#0d2149;border-radius:4px;padding:2px 7px;display:inline-block;margin:2px}
.chart-wrap{height:240px;position:relative;margin-top:10px}
.logs-box{background:#0d1117;border-radius:8px;padding:12px;font-family:"SF Mono",Menlo,monospace;font-size:11px;line-height:1.6;max-height:50vh;overflow-y:auto;border:1px solid #21262d;white-space:pre-wrap;word-break:break-all}
.log-err{color:#f85149}.log-warn{color:#d29922}.log-ok{color:#3fb950;font-weight:600}
.log-trade{color:#ffd33d;font-weight:700}.log-brain{color:#58a6ff;font-weight:600}.log-dim{color:#484f58}
.notes{font-size:12px;color:#8b949e;line-height:1.7}
.note-item{background:#0d1117;border-radius:6px;padding:10px 12px;margin:6px 0;border-left:3px solid #d29922}
.perf-cat{display:flex;justify-content:space-between;align-items:center;padding:6px 10px;background:#0d1117;border-radius:6px;margin:4px 0;font-size:13px}
.perf-cat .cat-name{color:#c9d1d9}
.empty{text-align:center;color:#8b949e;padding:30px;font-size:14px}
</style>
</head>
<body>

<h1>🤖 Polymarket AI Agent</h1>
<div class="subtitle">Claude 自主交易 · 每 30 秒刷新 · __NOW__</div>

<div class="status-bar __STATUS_CLASS__">
  <div class="dot __STATUS_CLASS__"></div>
  <strong>Agent 状态:</strong>&nbsp;__STATUS__
  &nbsp;|&nbsp; 运行时间: __UPTIME__
</div>

<!-- 核心指标 -->
<div class="grid">
  <div class="card">
    <div class="val neu">$__BUDGET__</div>
    <div class="lbl">初始预算</div>
  </div>
  <div class="card">
    <div class="val __PNL_CLASS__">__PNL_SIGN__$__PNL__</div>
    <div class="lbl">总盈亏</div>
  </div>
  <div class="card">
    <div class="val __PNL_CLASS__">__ROI_SIGN____ROI__%</div>
    <div class="lbl">收益率</div>
  </div>
  <div class="card">
    <div class="val neu">__TOTAL__</div>
    <div class="lbl">总交易次数</div>
  </div>
  <div class="card">
    <div class="val pos">__WINS__</div>
    <div class="lbl">盈利次数</div>
  </div>
  <div class="card">
    <div class="val warn">__PENDING__</div>
    <div class="lbl">持仓中</div>
  </div>
</div>

<!-- PnL 图表 -->
<div class="section">
  <h3>📈 盈亏曲线</h3>
  <div class="chart-wrap">
    <canvas id="pnlChart"></canvas>
  </div>
</div>

<!-- Claude 的决策记录 -->
<div class="section">
  <h3>🧠 Claude 最近决策</h3>
  __DECISIONS__
</div>

<!-- 按类别统计 -->
<div class="section">
  <h3>📊 各类市场表现</h3>
  __CAT_PERF__
</div>

<!-- Claude 的经验笔记 -->
<div class="section">
  <h3>📝 Claude 学习笔记</h3>
  __NOTES__
</div>

<!-- 实时日志 -->
<div class="section">
  <h3>📋 最近日志</h3>
  <div class="logs-box">__LOGS__</div>
</div>

<script>
const pnlData = __PNL_JSON__;
const ctx = document.getElementById('pnlChart');
if (pnlData.length > 1) {
  new Chart(ctx, {
    type:'line',
    data:{
      labels: pnlData.map(d => d.ts ? d.ts.slice(5,16).replace('T',' ') : ''),
      datasets:[{
        label:'累计盈亏 (USDC)',
        data: pnlData.map(d => d.pnl || 0),
        borderColor:'#58a6ff',
        backgroundColor:'rgba(88,166,255,0.1)',
        tension:0.3,fill:true,pointRadius:3,
      },{
        label:'零轴',
        data: pnlData.map(()=>0),
        borderColor:'#30363d',borderDash:[4,4],pointRadius:0,
      }]
    },
    options:{
      responsive:true,maintainAspectRatio:false,
      plugins:{legend:{labels:{color:'#c9d1d9',font:{size:11}}}},
      scales:{
        x:{ticks:{color:'#8b949e',maxTicksLimit:8,font:{size:10}},grid:{color:'#21262d'}},
        y:{ticks:{color:'#8b949e',font:{size:10}},grid:{color:'#21262d'}}
      }
    }
  });
} else {
  const c = ctx.getContext('2d');
  c.fillStyle='#484f58';c.font='13px monospace';
  c.fillText('等待交易数据积累...', 20, 50);
}
</script>
</body></html>'''


def sh(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=8).stdout
    except Exception:
        return ''


def load_memory():
    try:
        with open(MEMORY_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'decisions': [], 'performance': {}, 'strategy_notes': []}


def render_decisions(decisions):
    if not decisions:
        return '<div class="empty">暂无交易记录，等待 Claude 分析市场...</div>'

    recent = list(reversed(decisions[-10:]))
    parts = []
    for d in recent:
        direction = d.get('direction', 'SKIP')
        outcome   = d.get('outcome')
        pnl       = d.get('pnl')
        my_prob   = d.get('my_probability', 0)
        mkt_price = d.get('market_price', 0)
        confidence = d.get('confidence', '')
        reasoning  = d.get('reasoning', '暂无推理记录')
        factors    = d.get('key_factors', [])
        q          = d.get('question', '未知市场')[:80]
        ts         = d.get('timestamp', '')[:16].replace('T', ' ')
        size       = d.get('size_usdc', 0)
        category   = d.get('category', '')

        if outcome is not None:
            won = (pnl or 0) > 0
            cls = 'win' if won else 'loss'
            outcome_tag = f'<span class="tag {"yes" if won else "no"}">{"✅ 盈利 +$" + str(abs(pnl)) if won else "❌ 亏损 -$" + str(abs(pnl))}</span>'
        else:
            cls = 'pending'
            outcome_tag = '<span class="tag warn">⏳ 持仓中</span>'

        dir_tag = f'<span class="tag {"yes" if direction=="YES" else ("no" if direction=="NO" else "skip")}">{direction}</span>'
        factors_html = ''.join(f'<span class="factor">{f}</span>' for f in factors[:3])

        edge = abs(my_prob - mkt_price)

        parts.append(f'''
<div class="decision {cls}">
  <div class="q">{q}</div>
  <div class="meta">
    {dir_tag}
    <span class="tag claude">Claude: {my_prob:.1%}</span>
    <span class="tag market">市场: {mkt_price:.1%}</span>
    <span class="tag">偏差: {edge:.1%}</span>
    <span class="tag">信心: {confidence}</span>
    <span class="tag">下注: ${size}</span>
    {outcome_tag}
    <span class="tag">{ts}</span>
    {f'<span class="tag">{category}</span>' if category else ''}
  </div>
  <div class="reasoning">💭 {reasoning}</div>
  {f'<div class="factors">{factors_html}</div>' if factors_html else ''}
</div>''')

    return '\n'.join(parts)


def render_cat_perf(perf):
    by_cat = perf.get('by_category', {})
    if not by_cat:
        return '<div class="empty">等待更多交易数据...</div>'

    parts = []
    for cat, stats in sorted(by_cat.items(), key=lambda x: -x[1].get('trades', 0)):
        t = stats.get('trades', 0)
        w = stats.get('wins', 0)
        p = stats.get('pnl', 0)
        wr = f'{w/t*100:.0f}%' if t > 0 else '-'
        pnl_cls = 'pos' if p > 0 else ('neg' if p < 0 else 'neu')
        parts.append(f'''
<div class="perf-cat">
  <span class="cat-name">{cat}</span>
  <span>{t} 笔</span>
  <span>胜率 {wr}</span>
  <span class="{pnl_cls}">盈亏 {"+$" if p>0 else "-$"}{abs(p):.2f}</span>
</div>''')

    return '\n'.join(parts) if parts else '<div class="empty">暂无分类数据</div>'


def render_notes(notes):
    if not notes:
        return '<div class="empty">等待 Claude 第一次复盘（每小时一次）...</div>'

    parts = []
    for note in reversed(notes[-5:]):
        parts.append(f'<div class="note-item">{note}</div>')
    return '\n'.join(parts)


def build_pnl_series(decisions):
    """从决策记录构建 PnL 时序"""
    series = []
    cum_pnl = 0.0
    for d in decisions:
        if d.get('pnl') is not None:
            cum_pnl += d['pnl']
            series.append({'ts': d.get('timestamp', ''), 'pnl': round(cum_pnl, 2)})
    return series


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # 基本状态
        status_raw = sh(['systemctl', 'is-active', 'polymarket-bot']).strip() or 'unknown'
        is_active  = (status_raw == 'active')
        uptime_raw = sh(['systemctl', 'show', 'polymarket-bot', '--property=ActiveEnterTimestamp'])
        uptime_str = ''
        try:
            ts_str = uptime_raw.split('=', 1)[1].strip()
            from datetime import timezone
            started = datetime.datetime.strptime(ts_str[:19], '%a %Y-%m-%d %H:%M:%S')
            diff    = datetime.datetime.utcnow() - started
            h, rem  = divmod(int(diff.total_seconds()), 3600)
            m       = rem // 60
            uptime_str = f'{h}h {m}m'
        except Exception:
            uptime_str = '-'

        # 记忆数据
        memory    = load_memory()
        decisions = memory.get('decisions', [])
        perf      = memory.get('performance', {})
        notes     = memory.get('strategy_notes', [])

        total   = perf.get('total_trades', 0)
        wins    = perf.get('wins', 0)
        pnl_val = perf.get('total_pnl', 0.0)
        budget  = 50.0  # 初始预算
        pending = sum(1 for d in decisions if d.get('outcome') is None)
        roi     = (pnl_val / budget * 100) if budget > 0 else 0

        pnl_class = 'pos' if pnl_val > 0 else ('neg' if pnl_val < 0 else 'neu')
        pnl_sign  = '+' if pnl_val > 0 else ''
        roi_sign  = '+' if roi > 0 else ''

        # 日志渲染
        raw_log = sh(['journalctl', '-u', 'polymarket-bot', '-n', '150', '--no-pager', '--output=short'])
        log_lines = []
        for ln in reversed(raw_log.split('\n')[-150:]):
            esc = ln.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            if '✅' in ln:
                cls = 'log-trade'
            elif '❌' in ln or '[ERROR]' in ln:
                cls = 'log-err'
            elif '🧠' in ln or 'Claude' in ln:
                cls = 'log-brain'
            elif '📊' in ln or '📝' in ln:
                cls = 'log-ok'
            elif '[WARNING]' in ln:
                cls = 'log-warn'
            elif 'HTTP Request' in ln or 'systemd' in ln:
                cls = 'log-dim'
            else:
                cls = ''
            log_lines.append(f'<span class="{cls}">{esc}</span>' if cls else esc)

        pnl_series = build_pnl_series(decisions)

        repl = {
            '__NOW__':         datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC'),
            '__STATUS__':      '🟢 运行中' if is_active else '🔴 已停止',
            '__STATUS_CLASS__': '' if is_active else 'bad',
            '__UPTIME__':      uptime_str,
            '__BUDGET__':      f'{budget:.0f}',
            '__PNL__':         f'{abs(pnl_val):.2f}',
            '__PNL_SIGN__':    pnl_sign,
            '__PNL_CLASS__':   pnl_class,
            '__ROI__':         f'{abs(roi):.1f}',
            '__ROI_SIGN__':    roi_sign,
            '__TOTAL__':       str(total),
            '__WINS__':        str(wins),
            '__PENDING__':     str(pending),
            '__DECISIONS__':   render_decisions(decisions),
            '__CAT_PERF__':    render_cat_perf(perf),
            '__NOTES__':       render_notes(notes),
            '__LOGS__':        '\n'.join(log_lines),
            '__PNL_JSON__':    json.dumps(pnl_series),
        }

        html = HTML
        for k, v in repl.items():
            html = html.replace(k, v)

        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    print(f'AI Agent 监控面板运行在 :{port}')
    HTTPServer(('0.0.0.0', port), Handler).serve_forever()
