#!/usr/bin/env python3
"""
Polymarket AI Agent v1.0

真正的 AI 交易智能体。Claude 是决策核心：
- 自主扫描和研究市场
- 基于真实世界知识形成概率判断
- 发现市场定价偏差并交易
- 记录每次决策和推理
- 从结算结果中学习
- 定期复盘，发现规律，进化策略

这不是规则机器人，是会独立思考的 AI 交易员。
"""

import os
import json
import time
import logging
import datetime
import requests
from typing import Optional, Dict, List
import anthropic
from eth_account import Account
from py_clob_client_v2 import ClobClient, SignatureTypeV2
from py_clob_client_v2.clob_types import OrderArgs, PartialCreateOrderOptions, ApiCreds
from py_clob_client_v2.order_builder.constants import BUY

# ─── 环境变量 ─────────────────────────────────────────────────────────────────
PRIVATE_KEY       = os.environ.get("PRIVATE_KEY", "")
FUNDER            = os.environ.get("FUNDER", "0xe2De1150638d9eE4127132BC41c8CF008bC901FF")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TG_TOKEN          = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID        = os.environ.get("TG_CHAT_ID", "")
BUDGET_USDC       = float(os.environ.get("MY_BUDGET", "50"))

# ─── API 端点 ─────────────────────────────────────────────────────────────────
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API  = "https://data-api.polymarket.com"

# ─── API 凭证（从环境变量加载）──────────────────────────────────────────────
API_KEY        = os.environ.get("POLY_API_KEY", "")
API_SECRET     = os.environ.get("POLY_API_SECRET", "")
API_PASSPHRASE = os.environ.get("POLY_API_PASSPHRASE", "")

# ─── Agent 参数 ───────────────────────────────────────────────────────────────
MEMORY_FILE        = "agent_memory.json"
MODEL              = "claude-opus-4-5"
SCAN_INTERVAL      = 300    # 每 5 分钟扫描市场
REFLECT_INTERVAL   = 3600   # 每小时复盘
MIN_EDGE           = 0.08   # 最小定价偏差才值得交易
MAX_OPEN_POSITIONS = 5      # 最多同时持仓数
MAX_BET_USDC       = 10.0   # 单笔最大下注

# ─── 日志 ─────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("agent.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# 记忆系统 — Agent 的知识库和学习日志
# ═══════════════════════════════════════════════════════════════════════════════

def load_memory() -> dict:
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {
        "decisions": [],
        "performance": {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl": 0.0,
            "by_category": {}
        },
        "strategy_notes": [],
        "last_reflection": None,
    }


def save_memory(memory: dict):
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(memory, f, indent=2, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════════════
# 市场扫描 — 找到值得 Claude 分析的候选市场
# ═══════════════════════════════════════════════════════════════════════════════

def scan_markets() -> List[dict]:
    """
    扫描 Polymarket 活跃市场，返回候选列表。
    直接使用 CLOB API（包含完整 token 数据），逐个获取价格。
    """
    try:
        # 从 CLOB API 获取活跃市场（包含 token 数据）
        r = requests.get(
            f"{CLOB_HOST}/markets",
            params={"active": "true", "closed": "false", "limit": 30},
            timeout=15,
        )
        r.raise_for_status()
        clob_markets = r.json().get("data", [])

        candidates = []
        for m in clob_markets:
            tokens = m.get("tokens", [])
            if len(tokens) < 2:
                continue

            # 找 YES / NO token
            yes_tok = next(
                (t for t in tokens if str(t.get("outcome", "")).upper() == "YES"),
                tokens[0],
            )
            no_tok = next(
                (t for t in tokens if str(t.get("outcome", "")).upper() == "NO"),
                tokens[1] if len(tokens) > 1 else None,
            )

            yes_tid = yes_tok.get("token_id", "")
            no_tid  = no_tok.get("token_id", "") if no_tok else ""
            if not yes_tid:
                continue

            # 获取当前价格
            try:
                pr = requests.get(
                    f"{CLOB_HOST}/midpoint",
                    params={"token_id": yes_tid},
                    timeout=5,
                )
                if not pr.ok:
                    continue
                yes_p = float(pr.json().get("mid", 0))
            except Exception:
                continue

            # 过滤极端价格（已基本确定的市场）
            if yes_p < 0.04 or yes_p > 0.96:
                continue

            m["_yes_price"] = yes_p
            m["_no_price"]  = round(1 - yes_p, 6)
            m["_yes_token"] = yes_tid
            m["_no_token"]  = no_tid
            candidates.append(m)

        log.info(f"扫描到 {len(candidates)} 个候选市场")
        return candidates[:10]

    except Exception as e:
        log.error(f"扫描市场失败: {e}")
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# Claude 大脑 — 市场分析与决策
# ═══════════════════════════════════════════════════════════════════════════════

def claude_analyze(market: dict, memory: dict, ai: anthropic.Anthropic) -> Optional[dict]:
    """
    Claude 深度分析单个市场，给出交易决策。

    Claude 会：
    1. 用世界知识评估事件真实概率
    2. 与市场定价比较，发现偏差
    3. 决定是否下注、方向、金额
    4. 完整记录推理过程（用于未来学习）
    """
    question  = market.get("question", "")
    yes_price = market["_yes_price"]
    end_date  = market.get("endDate", "未知")
    vol24h    = float(market.get("volume24hr") or 0)
    category  = market.get("category", "未知类别")
    neg_risk  = bool(market.get("negRisk", False))

    # 准备 Claude 的背景信息
    perf      = memory.get("performance", {})
    wins      = perf.get("wins", 0)
    total     = perf.get("total_trades", 0)
    pnl       = perf.get("total_pnl", 0.0)
    win_rate  = f"{wins}/{total}" if total > 0 else "无记录"

    notes = memory.get("strategy_notes", [])[-3:]
    notes_text = "\n".join(f"• {n}" for n in notes) if notes else "暂无经验积累"

    today = datetime.date.today().isoformat()

    prompt = f"""今天是 {today}。你是一位专业的预测市场交易员，拥有广博的世界知识。

## 你的历史表现
胜率: {win_rate} | 总盈亏: ${pnl:.2f}

## 你积累的经验
{notes_text}

## 当前市场
**问题**: {question}
**类别**: {category}
**市场当前 YES 概率**: {yes_price:.1%}（即市场认为发生概率是 {yes_price*100:.1f}%）
**截止日期**: {end_date}
**24小时交易量**: ${vol24h:,.0f}
**是否 NegRisk 市场**: {neg_risk}

## 你的分析任务

用你对世界的真实了解，独立判断这个事件发生的概率。

思考步骤：
1. 这个问题涉及什么事件？现在的进展和状态如何？
2. 哪些公开信息和逻辑支持你的概率判断？
3. 市场价格合理吗？是否存在高估或低估？
4. 主要的不确定性和风险是什么？

交易规则：
- 只有 |你的概率 - 市场价格| > 0.08 时才值得交易
- confidence=low → size_usdc 最多 3
- confidence=medium → size_usdc 最多 6
- confidence=high → size_usdc 最多 10
- 宁可 SKIP 也不要勉强

严格按 JSON 格式回复（无其他内容）：
{{
  "my_probability": 0.XX,
  "direction": "YES 或 NO 或 SKIP",
  "confidence": "high 或 medium 或 low",
  "size_usdc": X.X,
  "reasoning": "你的完整分析（150字以内）",
  "key_factors": ["关键依据1", "关键依据2"],
  "uncertainty": "主要不确定性"
}}"""

    try:
        msg = ai.messages.create(
            model=MODEL,
            max_tokens=500,
            temperature=0.2,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()

        # 清理可能的 markdown 格式
        if "```" in text:
            text = text.split("```")[1].lstrip("json").strip()

        result = json.loads(text)

        # 注入市场元数据
        result["question"]   = question
        result["market_id"]  = market.get("id", "")
        result["yes_token"]  = market["_yes_token"]
        result["no_token"]   = market["_no_token"]
        result["market_price"] = yes_price
        result["neg_risk"]   = neg_risk
        result["end_date"]   = end_date
        result["edge"]       = abs(result.get("my_probability", yes_price) - yes_price)
        result["category"]   = category

        direction = result.get("direction", "SKIP")
        my_prob   = result.get("my_probability", yes_price)

        log.info(
            f"🧠 {question[:45]}\n"
            f"   Claude: {my_prob:.1%} | 市场: {yes_price:.1%} | "
            f"决策: {direction} | 信心: {result.get('confidence','?')}"
        )

        return result

    except Exception as e:
        log.warning(f"Claude 分析失败 [{question[:30]}]: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Claude 大脑 — 复盘与进化
# ═══════════════════════════════════════════════════════════════════════════════

def claude_reflect(memory: dict, ai: anthropic.Anthropic) -> str:
    """
    让 Claude 复盘自己的表现，发现规律，更新策略。
    这是 Agent 真正「学习」的地方。
    """
    settled = [d for d in memory.get("decisions", []) if d.get("outcome") is not None]

    if len(settled) < 3:
        log.info("已结算交易不足 3 笔，暂不复盘")
        return ""

    recent = settled[-15:]

    review = []
    for d in recent:
        review.append({
            "问题": d.get("question", "")[:50],
            "我的判断": f"{d.get('my_probability', 0):.1%}",
            "市场价格": f"{d.get('market_price', 0):.1%}",
            "方向": d.get("direction"),
            "实际结果": d.get("outcome"),
            "盈亏": f"${d.get('pnl', 0):.2f}",
            "我的推理摘要": d.get("reasoning", "")[:60],
        })

    perf = memory.get("performance", {})

    prompt = f"""你是 Polymarket AI Agent，正在进行深度复盘。

## 总体战绩
总交易: {perf.get('total_trades', 0)} 笔
盈利次数: {perf.get('wins', 0)}
总盈亏: ${perf.get('total_pnl', 0):.2f}

## 最近 {len(recent)} 笔交易记录
{json.dumps(review, ensure_ascii=False, indent=2)}

## 请诚实地复盘以下问题：
1. 哪些类型的市场你判断最准？为什么？
2. 你在哪里犯了系统性错误？是什么导致的？
3. 你的哪些推理方式有效，哪些无效？
4. 下一阶段应该专注什么类型的市场？
5. 给出 2-3 条具体可执行的改进策略

要求：诚实、具体、可操作。200字以内。"""

    try:
        msg = ai.messages.create(
            model=MODEL,
            max_tokens=400,
            temperature=0.4,
            messages=[{"role": "user", "content": prompt}],
        )
        reflection = msg.content[0].text.strip()
        log.info(f"\n📊 Agent 复盘:\n{reflection}\n")
        return reflection

    except Exception as e:
        log.error(f"复盘失败: {e}")
        return ""


# ═══════════════════════════════════════════════════════════════════════════════
# 交易执行
# ═══════════════════════════════════════════════════════════════════════════════

def execute_trade(client: ClobClient, decision: dict, memory: dict) -> bool:
    """执行 Claude 的交易决策，记录到记忆"""

    direction = decision.get("direction")
    if direction not in ("YES", "NO"):
        return False

    # 根据方向选择 token
    if direction == "YES":
        token_id = decision.get("yes_token", "")
    else:
        token_id = decision.get("no_token", "")

    if not token_id:
        log.warning("找不到对应方向的 token_id")
        return False

    size_usdc = min(float(decision.get("size_usdc", 3.0)), MAX_BET_USDC)
    neg_risk  = decision.get("neg_risk", False)

    try:
        # 获取当前价格
        r = requests.get(
            f"{CLOB_HOST}/midpoint",
            params={"token_id": token_id},
            timeout=10,
        )
        price = float(r.json().get("mid", 0))

        if not (0.01 < price < 0.99):
            log.warning(f"价格异常 {price:.4f}，跳过")
            return False

        tick = float(client.get_tick_size(token_id))
        aligned_price = round(round(price / tick) * tick, 6)
        shares = round(size_usdc / aligned_price, 2)

        if shares <= 0:
            return False

        resp = client.create_and_post_order(
            OrderArgs(
                token_id=token_id,
                price=aligned_price,
                size=shares,
                side=BUY,
            ),
            PartialCreateOrderOptions(tick_size=str(tick), neg_risk=neg_risk),
        )

        log.info(
            f"✅ 下单成功 | {direction} ${size_usdc:.2f} @ {aligned_price:.4f} "
            f"| {shares} shares"
        )

        # 写入记忆
        record = {
            "id":           f"{token_id[:12]}_{int(time.time())}",
            "question":     decision.get("question", ""),
            "category":     decision.get("category", ""),
            "market_id":    decision.get("market_id", ""),
            "token_id":     token_id,
            "direction":    direction,
            "my_probability": decision.get("my_probability"),
            "market_price": decision.get("market_price"),
            "edge":         decision.get("edge"),
            "confidence":   decision.get("confidence"),
            "size_usdc":    size_usdc,
            "actual_price": aligned_price,
            "reasoning":    decision.get("reasoning", ""),
            "key_factors":  decision.get("key_factors", []),
            "uncertainty":  decision.get("uncertainty", ""),
            "timestamp":    datetime.datetime.utcnow().isoformat(),
            "end_date":     decision.get("end_date", ""),
            "outcome":      None,
            "pnl":          None,
        }

        memory["decisions"].append(record)
        memory["performance"]["total_trades"] += 1
        save_memory(memory)

        tg(
            f"🧠 *AI Agent 下单*\n"
            f"{decision.get('question','')[:50]}\n"
            f"方向: {direction} | ${size_usdc:.2f} | "
            f"我的判断: {decision.get('my_probability',0):.1%} vs 市场: {decision.get('market_price',0):.1%}\n"
            f"理由: {decision.get('reasoning','')[:100]}"
        )

        return True

    except Exception as e:
        log.error(f"下单失败: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# 结果追踪 — 检查持仓是否结算
# ═══════════════════════════════════════════════════════════════════════════════

def check_outcomes(memory: dict, my_addr: str):
    """检查未结算持仓，更新记忆和盈亏"""
    pending = [d for d in memory["decisions"] if d.get("outcome") is None]
    if not pending:
        return

    try:
        r = requests.get(
            f"{DATA_API}/positions",
            params={"user": my_addr, "sizeThreshold": "0.01"},
            timeout=15,
        )
        if not r.ok:
            return

        held_tokens = {p.get("asset"): p for p in r.json()}

        for d in pending:
            token_id = d.get("token_id", "")

            # 如果这个 token 不在当前持仓里，可能已结算
            if token_id not in held_tokens:
                # 查市场状态
                market_id = d.get("market_id", "")
                if not market_id:
                    continue

                try:
                    mr = requests.get(
                        f"{GAMMA_API}/markets/{market_id}",
                        timeout=10,
                    )
                    if not mr.ok:
                        continue

                    mdata = mr.json()
                    if not mdata.get("closed") and not mdata.get("resolved"):
                        continue

                    prices = mdata.get("outcomePrices", [])
                    if not prices:
                        continue

                    yes_final = float(prices[0])
                    actual_outcome = "YES" if yes_final > 0.5 else "NO"
                    won = (actual_outcome == d["direction"])

                    # 计算盈亏
                    actual_price = d.get("actual_price", 0.5)
                    size         = d.get("size_usdc", 0)
                    if won:
                        pnl = round(size * (1.0 / actual_price - 1.0), 2)
                        memory["performance"]["wins"] += 1
                    else:
                        pnl = -size
                        memory["performance"]["losses"] = memory["performance"].get("losses", 0) + 1

                    memory["performance"]["total_pnl"] += pnl

                    # 按类别统计
                    cat = d.get("category", "其他")
                    cat_perf = memory["performance"]["by_category"].setdefault(
                        cat, {"trades": 0, "wins": 0, "pnl": 0.0}
                    )
                    cat_perf["trades"] += 1
                    if won:
                        cat_perf["wins"] += 1
                    cat_perf["pnl"] = round(cat_perf["pnl"] + pnl, 2)

                    d["outcome"] = actual_outcome
                    d["pnl"]     = pnl

                    emoji = "✅" if won else "❌"
                    log.info(
                        f"{emoji} 结算 | {d['question'][:40]} | "
                        f"预测: {d['direction']} | 实际: {actual_outcome} | "
                        f"PnL: ${pnl:.2f}"
                    )
                    tg(
                        f"{emoji} *结算*\n{d['question'][:50]}\n"
                        f"预测: {d['direction']} | 实际: {actual_outcome} | "
                        f"盈亏: ${pnl:+.2f}"
                    )

                except Exception as inner_e:
                    log.warning(f"检查单个结果失败: {inner_e}")

        save_memory(memory)

    except Exception as e:
        log.error(f"检查结果失败: {e}")


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


# ═══════════════════════════════════════════════════════════════════════════════
# 主 Agent 循环
# ═══════════════════════════════════════════════════════════════════════════════

def run():
    if not PRIVATE_KEY:
        log.error("未设置 PRIVATE_KEY")
        return
    if not ANTHROPIC_API_KEY:
        log.error("未设置 ANTHROPIC_API_KEY")
        return

    log.info("=" * 65)
    log.info("🤖 Polymarket AI Agent v1.0 启动")
    log.info(f"   预算: ${BUDGET_USDC} USDC")
    log.info(f"   Funder: {FUNDER}")
    log.info(f"   模型: {MODEL}")
    log.info(f"   扫描间隔: {SCAN_INTERVAL}s | 复盘间隔: {REFLECT_INTERVAL}s")
    log.info("=" * 65)

    # 初始化 Anthropic 客户端
    ai = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # 初始化记忆
    memory = load_memory()

    # 推导地址
    raw_key = "0x" + PRIVATE_KEY if not PRIVATE_KEY.startswith("0x") else PRIVATE_KEY
    my_addr = Account.from_key(raw_key).address.lower()
    log.info(f"   签名地址: {my_addr}")

    # 初始化 Polymarket CLOB 客户端
    creds = ApiCreds(
        api_key=API_KEY,
        api_secret=API_SECRET,
        api_passphrase=API_PASSPHRASE,
    )
    client = ClobClient(
        CLOB_HOST,
        key=raw_key,
        chain_id=137,
        creds=creds,
        signature_type=SignatureTypeV2.POLY_1271,
        funder=FUNDER,
    )

    perf = memory["performance"]
    tg(
        f"🤖 *Polymarket AI Agent 上线*\n"
        f"预算: ${BUDGET_USDC} | 历史战绩: {perf['wins']}/{perf['total_trades']} | "
        f"总盈亏: ${perf['total_pnl']:.2f}"
    )

    last_scan    = 0.0
    last_reflect = 0.0

    while True:
        try:
            now = time.time()

            # ── 1. 检查已有持仓是否结算 ────────────────────────────────────
            check_outcomes(memory, my_addr)

            # ── 2. 市场扫描 & Claude 分析决策 ──────────────────────────────
            if now - last_scan >= SCAN_INTERVAL:
                log.info(f"\n{'━'*50}")
                log.info("🔍 开始新一轮市场扫描")

                # 检查持仓上限
                open_count = sum(
                    1 for d in memory["decisions"] if d.get("outcome") is None
                )
                if open_count >= MAX_OPEN_POSITIONS:
                    log.info(f"持仓已满 ({open_count}/{MAX_OPEN_POSITIONS})，跳过本轮")
                else:
                    markets = scan_markets()

                    # 避免重复分析已持仓的 token
                    active_tokens = {
                        d.get("token_id")
                        for d in memory["decisions"]
                        if d.get("outcome") is None
                    }

                    trades_this_round = 0

                    for market in markets:
                        yes_tok = market.get("_yes_token", "")
                        no_tok  = market.get("_no_token", "")

                        if yes_tok in active_tokens or no_tok in active_tokens:
                            continue

                        if trades_this_round >= 3:  # 每轮最多 3 笔新交易
                            break

                        # Claude 分析
                        decision = claude_analyze(market, memory, ai)
                        if not decision:
                            time.sleep(1)
                            continue

                        direction = decision.get("direction", "SKIP")
                        edge      = decision.get("edge", 0)

                        if direction == "SKIP" or edge < MIN_EDGE:
                            log.info(
                                f"  ⏭  SKIP | {market.get('question','')[:40]} "
                                f"(edge={edge:.2f})"
                            )
                            time.sleep(1)
                            continue

                        # 执行交易
                        if execute_trade(client, decision, memory):
                            trades_this_round += 1

                        time.sleep(2)

                    if trades_this_round == 0:
                        log.info("本轮未发现合适交易机会")

                last_scan = time.time()

            # ── 3. 定期复盘 ─────────────────────────────────────────────────
            if now - last_reflect >= REFLECT_INTERVAL:
                log.info(f"\n{'━'*50}")
                log.info("📊 Claude 开始复盘")

                reflection = claude_reflect(memory, ai)
                if reflection:
                    note = f"[{datetime.date.today()}] {reflection}"
                    memory["strategy_notes"].append(note)
                    memory["last_reflection"] = datetime.datetime.utcnow().isoformat()
                    save_memory(memory)
                    tg(f"📊 *Agent 复盘*\n{reflection[:300]}")

                last_reflect = time.time()

        except KeyboardInterrupt:
            log.info("Agent 手动停止")
            tg("🛑 AI Agent 已停止")
            break
        except Exception as e:
            log.error(f"主循环异常: {e}", exc_info=True)

        time.sleep(15)


if __name__ == "__main__":
    run()
