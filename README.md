# Polymarket Agent

两个独立策略，共用一套凭证：

| 文件 | 策略 |
|------|------|
| `agent.py` | AI Agent：Claude 自主扫描市场、判断概率偏差、下单、复盘进化 |
| `copy_trader.py` | 跟单策略：监控聪明钱地址，自动跟随买卖 |
| `monitor.py` | AI Agent 的监控面板 |

## 跟单策略（copy_trader.py）

### 快速开始

```bash
pip install -r requirements.txt

# 1. 纸面模式先跑几天，验证跟单效果（默认 DRY_RUN=1，不下真单）
export COPY_LEADERS="0x1234...abcd:whale1,0x5678...efgh"
python3 copy_trader.py

# 2. 确认效果后再实盘
export DRY_RUN=0
export PRIVATE_KEY="..."          # 签名私钥
export POLY_API_KEY="..."         # CLOB API 凭证
export POLY_API_SECRET="..."
export POLY_API_PASSPHRASE="..."
python3 copy_trader.py
```

跟单对象可以在 [Polymarket 排行榜](https://polymarket.com/leaderboard) 找，
把用户主页 URL 里的钱包地址填进 `COPY_LEADERS`（逗号分隔，冒号后可加昵称）。

### 跟单逻辑

- **买入**：leader 买入且单笔 ≥ `MIN_LEADER_USDC` 时，按 `COPY_RATIO` 比例
  （或 `COPY_FIXED_USDC` 固定金额）跟买，金额限制在
  `MIN_COPY_USDC`~`MAX_COPY_USDC` 之间
- **卖出**：leader 卖出时，按其本次卖出占原持仓的比例，同步减掉我们的仓位
  （leader 清仓 → 我们也清仓）
- **只跟新单**：启动后才发生的交易才会被跟，不会补跟历史仓位
- **结算**：市场结算后自动计入胜负和盈亏，Telegram 通知

### 风控

| 环境变量 | 默认 | 说明 |
|----------|------|------|
| `DRY_RUN` | `1` | 纸面模式，`0` 才实盘 |
| `COPY_RATIO` | `0.1` | 跟 leader 单笔金额的 10% |
| `COPY_FIXED_USDC` | `0` | 设 >0 则每笔固定跟这个金额 |
| `MIN_COPY_USDC` / `MAX_COPY_USDC` | `1` / `10` | 单笔跟单金额下下限/上限 |
| `MIN_LEADER_USDC` | `20` | leader 单笔低于此金额不跟（过滤尘埃单） |
| `COPY_DAILY_LIMIT` | `50` | 每日跟单总额上限 |
| `COPY_MAX_OPEN` | `10` | 最多同时持有的跟单仓位数 |
| `COPY_SLIPPAGE` | `0.03` | 当前价比 leader 成交价高出此值则放弃 |
| `COPY_POLL_INTERVAL` | `60` | 轮询间隔（秒） |
| `TG_TOKEN` / `TG_CHAT_ID` | - | Telegram 通知（可选） |

### 注意

- 跟单用 GTC 限价单挂在当前中间价，行情快时可能不成交（宁可错过不追高）
- 状态存在 `copy_state.json`，删掉即重置（水位线、持仓记录、战绩）
- 跟单有延迟（轮询间隔 + 挂单成交时间），leader 的短线单不适合跟
