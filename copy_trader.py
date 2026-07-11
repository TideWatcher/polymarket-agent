#!/usr/bin/env python3
"""
Polymarket 跟单策略 v1.0

自动跟随指定聪明钱地址交易：
- 轮询 Data API 监控 leader 的最新成交
- leader 买入 → 按比例（或固定金额）跟买
- leader 卖出 → 按其卖出比例同步减仓
- 滑点保护、单笔上下限、每日限额、持仓数上限
- 市场结算后自动统计每笔跟单盈亏
- Telegram 实时通知 + 每小时心跳

默认 DRY_RUN=1（纸面跟单，不下真单），确认无误后设 DRY_RUN=0 实盘。
"""

import os
import json
import time
import logging
import datetime
import requests
from typing import Optional, Dict, List

# ─── 环境变量（与 agent.py 共用一套凭证）─────────────────────────────────────
PRIVATE_KEY    = os.environ.get("PRIVATE_KEY", "")
FUNDER         = os.environ.get("FUNDER", "0xe2De1150638d9eE4127132BC41c8CF008bC901FF")
API_KEY        = os.environ.get("POLY_API_KEY", "")
API_SECRET     = os.environ.get("POLY_API_SECRET", "")
API_PASSPHRASE = os.environ.get("POLY_API_PASSPHRASE", "")
TG_TOKEN       = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID     = os.environ.get("TG_CHAT_ID", "")

# ─── 跟单参数 ─────────────────────────────────────────────────────────────────
# 要跟的地址，逗号分隔，可带昵称: "0xabc...:whale1,0xdef..."
COPY_LEADERS    = os.environ.get("COPY_LEADERS", "")
# 跟单比例：跟 leader 单笔金额的百分比（0.1 = 10%）
COPY_RATIO      = float(os.environ.get("COPY_RATIO", "0.1"))
# 固定金额模式：设了就忽略比例，每笔都跟这个金额（USDC）
COPY_FIXED_USDC = float(os.environ.get("COPY_FIXED_USDC", "0"))
# 单笔跟单金额下限/上限（USDC）
MIN_COPY_USDC   = float(os.environ.get("MIN_COPY_USDC", "1"))
MAX_COPY_USDC   = float(os.environ.get("MAX_COPY_USDC", "10"))
# leader 单笔低于这个金额视为尘埃单，不跟（USDC）
MIN_LEADER_USDC = float(os.environ.get("MIN_LEADER_USDC", "20"))
# 每日跟单总额上限（USDC）
DAILY_LIMIT     = float(os.environ.get("COPY_DAILY_LIMIT", "50"))
# 最多同时持有的跟单仓位数
MAX_OPEN        = int(os.environ.get("COPY_MAX_OPEN", "10"))
# 滑点保护：当前价比 leader 成交价高出这个值就放弃（绝对概率差）
SLIPPAGE        = float(os.environ.get("COPY_SLIPPAGE", "0.03"))
# 轮询间隔（秒）
POLL_INTERVAL   = int(os.environ.get("COPY_POLL_INTERVAL", "60"))
# 纸面模式：1 = 只记录不下单（默认，安全第一）
DRY_RUN         = os.environ.get("DRY_RUN", "1") == "1"

SETTLE_INTERVAL    = 600    # 每 10 分钟检查结算
HEARTBEAT_INTERVAL = 3600   # 每小时心跳
STATE_FILE         = "copy_state.json"

# ─── API 端点 ─────────────────────────────────────────────────────────────────
CLOB_HOST = "https://clob.polymarket.com"
DATA_API  = "https://data-api.polymarket.com"

# ─── 日志 ─────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("copy_trader.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

sess = requests.Session()


# ═══════════════════════════════════════════════════════════════════════════════
# 状态持久化 — 水位线、持仓、战绩
# ═══════════════════════════════════════════════════════════════════════════════

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {
        "leaders": {},      # addr -> {alias, last_ts, seen}
        "positions": [],    # 跟单持仓记录
        "performance": {
            "copied_buys": 0,
            "copied_sells": 0,
            "wins": 0,
            "losses": 0,
            "realized_pnl": 0.0,
        },
        "daily": {"date": "", "spent": 0.0},
    }


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def parse_leaders() -> Dict[str, str]:
    """解析 COPY_LEADERS，返回 {地址(小写): 昵称}"""
    leaders = {}
    for item in COPY_LEADERS.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            addr, alias = item.split(":", 1)
        else:
            addr, alias = item, item[:8]
        leaders[addr.strip().lower()] = alias.strip()
    return leaders


def today_str() -> str:
    return datetime.date.today().isoformat()


def daily_spent(state: dict) -> float:
    """今日已花费，跨天自动清零"""
    if state["daily"].get("date") != today_str():
        state["daily"] = {"date": today_str(), "spent": 0.0}
    return state["daily"]["spent"]


# ═══════════════════════════════════════════════════════════════════════════════
# 行情与账户数据
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_leader_trades(addr: str, limit: int = 50) -> List[dict]:
    """拉取 leader 最近成交，按时间升序返回"""
    try:
        r = sess.get(
            f"{DATA_API}/trades",
            params={"user": addr, "limit": limit, "takerOnly": "false"},
            timeout=15,
        )
        r.raise_for_status()
        trades = r.json()
        if not isinstance(trades, list):
            return []
        for t in trades:
            ts = int(t.get("timestamp") or 0)
            if ts > 10**12:  # 毫秒转秒
                ts //= 1000
            t["_ts"] = ts
        return sorted(trades, key=lambda t: t["_ts"])
    except Exception as e:
        log.warning(f"拉取 {addr[:10]} 成交失败: {e}")
        return []


def fetch_positions(addr: str) -> Dict[str, dict]:
    """某地址当前持仓，返回 {token_id: position}"""
    try:
        r = sess.get(
            f"{DATA_API}/positions",
            params={"user": addr, "sizeThreshold": "0.01"},
            timeout=15,
        )
        if not r.ok:
            return {}
        return {p.get("asset"): p for p in r.json()}
    except Exception as e:
        log.warning(f"拉取 {addr[:10]} 持仓失败: {e}")
        return {}


def get_midpoint(token_id: str) -> float:
    try:
        r = sess.get(
            f"{CLOB_HOST}/midpoint",
            params={"token_id": token_id},
            timeout=8,
        )
        if r.ok:
            return float(r.json().get("mid", 0))
    except Exception:
        pass
    return 0.0


_market_cache: Dict[str, dict] = {}

def get_market_info(condition_id: str, fresh: bool = False) -> Optional[dict]:
    """CLOB 市场信息（neg_risk / closed / tokens / 最小下单量），带缓存"""
    if not fresh and condition_id in _market_cache:
        return _market_cache[condition_id]
    try:
        r = sess.get(f"{CLOB_HOST}/markets/{condition_id}", timeout=10)
        if not r.ok:
            return None
        info = r.json()
        _market_cache[condition_id] = info
        return info
    except Exception as e:
        log.warning(f"获取市场 {condition_id[:14]} 失败: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# 下单封装 — DRY_RUN 时只记录不下单
# ═══════════════════════════════════════════════════════════════════════════════

_clob_client = None

def init_clob_client():
    """实盘模式才初始化 CLOB 客户端（延迟导入，纸面模式无需私钥）"""
    global _clob_client
    from py_clob_client_v2 import ClobClient, SignatureTypeV2
    from py_clob_client_v2.clob_types import ApiCreds

    raw_key = PRIVATE_KEY if PRIVATE_KEY.startswith("0x") else "0x" + PRIVATE_KEY
    _clob_client = ClobClient(
        CLOB_HOST,
        key=raw_key,
        chain_id=137,
        creds=ApiCreds(
            api_key=API_KEY,
            api_secret=API_SECRET,
            api_passphrase=API_PASSPHRASE,
        ),
        signature_type=SignatureTypeV2.POLY_1271,
        funder=FUNDER,
    )


def place_order(token_id: str, side: str, price: float, shares: float, neg_risk: bool) -> bool:
    """挂 GTC 限价单。side: 'BUY' / 'SELL'"""
    if DRY_RUN:
        log.info(f"  [纸面] {side} {shares} shares @ {price:.4f}（未实际下单）")
        return True
    try:
        from py_clob_client_v2.clob_types import OrderArgs, PartialCreateOrderOptions
        from py_clob_client_v2.order_builder.constants import BUY, SELL

        tick = float(_clob_client.get_tick_size(token_id))
        aligned = round(round(price / tick) * tick, 6)
        _clob_client.create_and_post_order(
            OrderArgs(
                token_id=token_id,
                price=aligned,
                size=shares,
                side=BUY if side == "BUY" else SELL,
            ),
            PartialCreateOrderOptions(tick_size=str(tick), neg_risk=neg_risk),
        )
        log.info(f"  ✅ 已下单 {side} {shares} shares @ {aligned:.4f}")
        return True
    except Exception as e:
        log.error(f"  下单失败: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# 跟单核心 — 买入
# ═══════════════════════════════════════════════════════════════════════════════

def decide_copy_usdc(leader_usdc: float) -> float:
    """根据 leader 金额决定跟单金额"""
    if COPY_FIXED_USDC > 0:
        usdc = COPY_FIXED_USDC
    else:
        usdc = leader_usdc * COPY_RATIO
    return round(min(max(usdc, MIN_COPY_USDC), MAX_COPY_USDC), 2)


def find_open_position(state: dict, token_id: str) -> Optional[dict]:
    for p in state["positions"]:
        if p["token_id"] == token_id and p["status"] == "open":
            return p
    return None


def copy_buy(trade: dict, alias: str, state: dict):
    token_id     = trade.get("asset", "")
    condition_id = trade.get("conditionId", "")
    title        = trade.get("title", "")
    outcome      = trade.get("outcome", "")
    leader_price = float(trade.get("price") or 0)
    leader_size  = float(trade.get("size") or 0)
    leader_usdc  = leader_price * leader_size

    prefix = f"👀 [{alias}] BUY {outcome} ${leader_usdc:.0f} @ {leader_price:.3f} | {title[:40]}"

    # ── 过滤 ──────────────────────────────────────────────────────────────
    if leader_usdc < MIN_LEADER_USDC:
        log.info(f"{prefix}\n  ⏭  金额低于 {MIN_LEADER_USDC}，视为尘埃单，不跟")
        return

    open_count = sum(1 for p in state["positions"] if p["status"] == "open")
    existing = find_open_position(state, token_id)
    if existing is None and open_count >= MAX_OPEN:
        log.info(f"{prefix}\n  ⏭  持仓已满 ({open_count}/{MAX_OPEN})，不跟")
        return

    usdc = decide_copy_usdc(leader_usdc)
    if daily_spent(state) + usdc > DAILY_LIMIT:
        log.info(f"{prefix}\n  ⏭  超出每日限额 (已用 ${daily_spent(state):.2f}/{DAILY_LIMIT})，不跟")
        return

    # ── 价格检查 ──────────────────────────────────────────────────────────
    mid = get_midpoint(token_id)
    if mid <= 0:
        mid = leader_price
    if not (0.02 < mid < 0.98):
        log.info(f"{prefix}\n  ⏭  当前价 {mid:.3f} 接近结算，不跟")
        return
    if mid > leader_price + SLIPPAGE:
        log.info(f"{prefix}\n  ⏭  当前价 {mid:.3f} 已比 leader 高 {mid-leader_price:.3f}，滑点超限")
        return

    # ── 计算数量（满足市场最小下单量）──────────────────────────────────────
    info = get_market_info(condition_id) or {}
    neg_risk = bool(info.get("neg_risk", False))
    min_size = float(info.get("minimum_order_size") or 5)

    shares = round(usdc / mid, 2)
    if shares < min_size:
        shares = min_size
        usdc = round(shares * mid, 2)
        if usdc > MAX_COPY_USDC * 1.5:
            log.info(f"{prefix}\n  ⏭  最小下单量 {min_size} shares 需 ${usdc:.2f}，超预算，不跟")
            return

    # ── 执行 ──────────────────────────────────────────────────────────────
    log.info(f"{prefix}\n  🎯 跟单 ${usdc:.2f} ({shares} shares @ {mid:.4f})")
    if not place_order(token_id, "BUY", mid, shares, neg_risk):
        return

    if existing:
        # 加仓：合并到已有记录，更新均价
        total_cost = existing["cost_usdc"] + usdc
        total_shares = existing["shares"] + shares
        existing["avg_price"] = round(total_cost / total_shares, 4)
        existing["shares"] = round(total_shares, 2)
        existing["cost_usdc"] = round(total_cost, 2)
    else:
        state["positions"].append({
            "token_id":     token_id,
            "condition_id": condition_id,
            "title":        title,
            "outcome":      outcome,
            "leader":       alias,
            "leader_price": leader_price,
            "avg_price":    mid,
            "shares":       shares,
            "cost_usdc":    usdc,
            "opened_at":    datetime.datetime.utcnow().isoformat(),
            "status":       "open",
            "realized_pnl": 0.0,
            "dry_run":      DRY_RUN,
        })

    state["daily"]["spent"] = round(daily_spent(state) + usdc, 2)
    state["performance"]["copied_buys"] += 1
    save_state(state)

    tg(
        f"🐑 *跟单买入* {'(纸面)' if DRY_RUN else ''}\n"
        f"跟随: {alias}\n{title[:60]}\n"
        f"{outcome} ${usdc:.2f} @ {mid:.3f}"
        f"（leader ${leader_usdc:.0f} @ {leader_price:.3f}）"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 跟单核心 — 卖出（按 leader 卖出比例同步减仓）
# ═══════════════════════════════════════════════════════════════════════════════

def copy_sell(trade: dict, alias: str, leader_addr: str, state: dict):
    token_id     = trade.get("asset", "")
    title        = trade.get("title", "")
    outcome      = trade.get("outcome", "")
    sell_price   = float(trade.get("price") or 0)
    sell_size    = float(trade.get("size") or 0)

    pos = find_open_position(state, token_id)
    if pos is None:
        return  # 我们没有这个仓位，与我们无关

    # leader 卖出占其原持仓的比例：卖出量 / (剩余 + 卖出量)
    leader_pos = fetch_positions(leader_addr).get(token_id)
    remaining = float(leader_pos.get("size") or 0) if leader_pos else 0.0
    fraction = 1.0 if remaining < 1 else sell_size / (remaining + sell_size)
    fraction = min(max(fraction, 0.0), 1.0)

    my_shares = round(pos["shares"] * fraction, 2)
    if my_shares <= 0:
        return

    mid = get_midpoint(token_id)
    if mid <= 0:
        mid = sell_price

    log.info(
        f"👀 [{alias}] SELL {outcome} {sell_size} shares（清仓比例 {fraction:.0%}）| {title[:40]}\n"
        f"  🎯 同步卖出 {my_shares}/{pos['shares']} shares @ {mid:.4f}"
    )

    info = get_market_info(pos["condition_id"]) or {}
    if not place_order(token_id, "SELL", mid, my_shares, bool(info.get("neg_risk", False))):
        return

    pnl = round(my_shares * (mid - pos["avg_price"]), 2)
    pos["shares"] = round(pos["shares"] - my_shares, 2)
    pos["realized_pnl"] = round(pos["realized_pnl"] + pnl, 2)
    state["performance"]["realized_pnl"] = round(
        state["performance"]["realized_pnl"] + pnl, 2
    )
    state["performance"]["copied_sells"] += 1

    if pos["shares"] < 0.01:
        pos["status"] = "closed"
        pos["closed_at"] = datetime.datetime.utcnow().isoformat()
        pos["close_reason"] = "follow_sell"
        if pos["realized_pnl"] >= 0:
            state["performance"]["wins"] += 1
        else:
            state["performance"]["losses"] += 1

    save_state(state)

    emoji = "✅" if pnl >= 0 else "❌"
    tg(
        f"{emoji} *跟单卖出* {'(纸面)' if DRY_RUN else ''}\n"
        f"跟随: {alias}\n{title[:60]}\n"
        f"卖出 {my_shares} shares @ {mid:.3f} | 本次盈亏: ${pnl:+.2f}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 轮询 leader 成交
# ═══════════════════════════════════════════════════════════════════════════════

def poll_leaders(leaders: Dict[str, str], state: dict):
    for addr, alias in leaders.items():
        ls = state["leaders"].setdefault(
            addr, {"alias": alias, "last_ts": 0, "seen": []}
        )

        # 首次见到该 leader：水位线设为当前时间，只跟之后的新单，不补历史
        if ls["last_ts"] == 0:
            ls["last_ts"] = int(time.time())
            log.info(f"开始监控 [{alias}] {addr}（只跟从现在起的新交易）")
            save_state(state)
            continue

        trades = fetch_leader_trades(addr)
        seen_list = ls["seen"]
        seen = set(seen_list)

        for t in trades:
            if t["_ts"] <= ls["last_ts"] - 120:  # 留 2 分钟重叠窗口防漏单
                continue
            key = f"{t.get('transactionHash','')[:18]}:{t.get('asset','')[:12]}:{t.get('side','')}"
            if key in seen:
                continue
            seen.add(key)
            seen_list.append(key)

            side = str(t.get("side", "")).upper()
            if side == "BUY":
                copy_buy(t, alias, state)
            elif side == "SELL":
                copy_sell(t, alias, addr, state)

            ls["last_ts"] = max(ls["last_ts"], t["_ts"])

        ls["seen"] = seen_list[-500:]  # 防止无限增长
        save_state(state)


# ═══════════════════════════════════════════════════════════════════════════════
# 结算追踪
# ═══════════════════════════════════════════════════════════════════════════════

def check_settlements(state: dict):
    """检查未平仓跟单的市场是否已结算，统计盈亏"""
    open_pos = [p for p in state["positions"] if p["status"] == "open"]
    for pos in open_pos:
        info = get_market_info(pos["condition_id"], fresh=True)
        if not info or not info.get("closed"):
            continue

        my_token = next(
            (tk for tk in info.get("tokens", []) if tk.get("token_id") == pos["token_id"]),
            None,
        )
        if my_token is None:
            continue

        won = bool(my_token.get("winner"))
        if won:
            pnl = round(pos["shares"] * (1.0 - pos["avg_price"]), 2)
            state["performance"]["wins"] += 1
        else:
            pnl = round(-pos["shares"] * pos["avg_price"], 2)
            state["performance"]["losses"] += 1

        pos["realized_pnl"] = round(pos["realized_pnl"] + pnl, 2)
        pos["status"] = "closed"
        pos["closed_at"] = datetime.datetime.utcnow().isoformat()
        pos["close_reason"] = "resolved"
        state["performance"]["realized_pnl"] = round(
            state["performance"]["realized_pnl"] + pnl, 2
        )

        emoji = "✅" if won else "❌"
        log.info(f"{emoji} 结算 | {pos['title'][:40]} | {pos['outcome']} | PnL ${pnl:+.2f}")
        tg(
            f"{emoji} *跟单结算* {'(纸面)' if pos.get('dry_run') else ''}\n"
            f"{pos['title'][:60]}\n"
            f"{pos['outcome']} {'胜' if won else '负'} | 盈亏: ${pnl:+.2f}\n"
            f"跟随: {pos['leader']}"
        )

    save_state(state)


# ═══════════════════════════════════════════════════════════════════════════════
# Telegram 通知
# ═══════════════════════════════════════════════════════════════════════════════

def tg(msg: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=8,
        )
    except Exception:
        pass


def heartbeat(leaders: Dict[str, str], state: dict):
    perf = state["performance"]
    open_pos = [p for p in state["positions"] if p["status"] == "open"]
    open_cost = sum(p["cost_usdc"] for p in open_pos)
    tg(
        f"💓 *跟单心跳* {'(纸面模式)' if DRY_RUN else ''}\n"
        f"监控 {len(leaders)} 个地址 | 持仓 {len(open_pos)} 笔 (${open_cost:.2f})\n"
        f"今日已用: ${daily_spent(state):.2f}/{DAILY_LIMIT}\n"
        f"战绩: {perf['wins']}胜{perf['losses']}负 | 已实现盈亏: ${perf['realized_pnl']:+.2f}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 主循环
# ═══════════════════════════════════════════════════════════════════════════════

def run():
    leaders = parse_leaders()
    if not leaders:
        log.error(
            "未设置 COPY_LEADERS。示例：\n"
            '  export COPY_LEADERS="0x1234...abcd:whale1,0x5678...efgh"\n'
            "可在 https://polymarket.com/leaderboard 找到值得跟的地址"
        )
        return

    if not DRY_RUN:
        if not PRIVATE_KEY:
            log.error("实盘模式需要 PRIVATE_KEY（或设 DRY_RUN=1 用纸面模式）")
            return
        init_clob_client()

    mode = "📝 纸面模式（不下真单）" if DRY_RUN else "💰 实盘模式"
    log.info("=" * 65)
    log.info("🐑 Polymarket 跟单策略 v1.0 启动")
    log.info(f"   {mode}")
    for addr, alias in leaders.items():
        log.info(f"   跟随: [{alias}] {addr}")
    if COPY_FIXED_USDC > 0:
        log.info(f"   跟单金额: 固定 ${COPY_FIXED_USDC}/笔")
    else:
        log.info(f"   跟单比例: {COPY_RATIO:.0%}（单笔 ${MIN_COPY_USDC}~${MAX_COPY_USDC}）")
    log.info(f"   每日限额: ${DAILY_LIMIT} | 最大持仓: {MAX_OPEN} 笔 | 滑点: {SLIPPAGE}")
    log.info(f"   忽略 leader < ${MIN_LEADER_USDC} 的单 | 轮询: {POLL_INTERVAL}s")
    log.info("=" * 65)

    state = load_state()
    perf = state["performance"]
    tg(
        f"🐑 *跟单策略上线* {'(纸面模式)' if DRY_RUN else ''}\n"
        f"监控 {len(leaders)} 个地址: {', '.join(leaders.values())}\n"
        f"历史战绩: {perf['wins']}胜{perf['losses']}负 | "
        f"已实现盈亏: ${perf['realized_pnl']:+.2f}"
    )

    last_settle    = 0.0
    last_heartbeat = time.time()  # 启动时已发上线通知，1 小时后再心跳

    while True:
        try:
            poll_leaders(leaders, state)

            now = time.time()
            if now - last_settle >= SETTLE_INTERVAL:
                check_settlements(state)
                last_settle = now
            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                heartbeat(leaders, state)
                last_heartbeat = now

        except KeyboardInterrupt:
            log.info("跟单策略手动停止")
            tg("🛑 跟单策略已停止")
            break
        except Exception as e:
            log.error(f"主循环异常: {e}", exc_info=True)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
