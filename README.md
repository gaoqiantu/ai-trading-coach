# AI Trading Coach（只读复盘官）

这是一个 **只读** 的交易复盘系统：自动拉取 Bitget（USDT 永续）的历史成交，聚合成“持仓生命周期”，做 **可追溯、可解释** 的事件检测与纪律复盘，并在固定时间推送到 Discord webhook。

## 重要约束（不谈情绪价值）

- **绝不包含任何下单/改单/撤单逻辑**
- **不做预测、不喊单、不提供方向建议**
- 所有结论必须基于 **已发生的数据**（成交/资金费/账户快照等）
- 所有结论必须 **可追溯**：事件输出里包含阈值、比较式、触发成交引用（trade_id/order_id/时间/价格/数量）

## 快速开始

在 `ai-trading-coach/` 目录：

```bash
python3 -m venv venv
source venv/bin/activate
pip3 install -r requirements.txt
```

### 1) 配置 `.env`

你已经填好 `.env` 的情况下，只需要确认这些字段存在（不要把密钥发到聊天里）：

#### Bitget（只读）

- `BITGET_API_KEY`
- `BITGET_API_SECRET`
- `BITGET_API_PASSWORD`

#### Discord（要推送才填）

- `DISCORD_WEBHOOK_URL`
- `DISCORD_USERNAME`（可选，默认 `ViperCoach`）

#### LLM（OpenAI-compatible）

- `LLM_BASE_URL`（例如 `https://space.ai-builders.com/backend/v1`）
- `LLM_API_KEY`
- `LLM_MODEL`（默认 `gpt-5`）

#### 调度（US/Eastern）

- `TIMEZONE=America/New_York`
- `DAILY_AT=23:00`
- `WEEKLY_DOW=sat`
- `WEEKLY_AT=23:00`
- `MONTHLY_AT=23:00`（月末检测任务在该时间触发）
- `ENABLE_SCHEDULER=1`（部署在同一个服务里跑定时任务：每日/每周/月末）

#### 存储

- `SQLITE_PATH=./data/ai_trading_coach.sqlite3`

#### 交易对（可选）

- `SYMBOLS=`（留空会自动枚举 Bitget 所有 USDT 永续市场逐个拉取，**慢但省事**）
- `MAX_SYMBOLS=0`（0=不限制；第一次建议设置为 200 之类防止太慢）

#### 同步参数（可选，防漏/防卡）

- **强制显示 long/short（推荐）**：
  - `BITGET_USE_REST_FILLS=1`（默认）
  - 使用 Bitget 私有 REST：`/api/v2/mix/order/fills` + `/api/v2/mix/order/detail`，用 `posSide` 保证方向准确
  - `BITGET_PRODUCT_TYPE=USDT-FUTURES`
  - `BITGET_REST_WINDOW_DAYS=1`（拆窗，避免一次拉太多）
  - `BITGET_REST_PAGE_LIMIT=100`
  - `BITGET_REST_MAX_PAGES_PER_WINDOW=50`（硬上限，防“看起来卡死”）
- **ccxt 兜底模式（不保证 hedge_mode 下的 long/short）**：
  - `BITGET_USE_REST_FILLS=0`
  - `SYNC_LIMIT=500`
  - `SYNC_MAX_PAGES=40`

通用：
- `SYNC_LOOKBACK_DAYS=7`（每次同步向前回溯 N 天，防漏成交）
- `SYNC_RESET=1`（重置：清空本地 lifecycles + 缓存 + sync_state；**会删除本地数据**）
- `SYNC_STOP_AFTER_LIFECYCLES=0`（测试用：找到并落库 N 个生命周期就停止；0=不限制）

> 注意：项目内置了一个极简 `.env` 解析器（不依赖 `python-dotenv`）。请尽量使用最简单的 `KEY=VALUE`，不要写引号/多行/复杂转义。

### 2) 手动同步一次（只读）

```bash
PYTHONPATH=./src python3 -m ai_trading_coach.pipeline.sync_bitget
```

如果你要 **重置并重拉**（例如第一次接入 / 规则大改后）：

```bash
export SYNC_RESET=1
PYTHONPATH=./src python3 -m ai_trading_coach.pipeline.sync_bitget
```

### 3) 运行调度器（每日/每周/月末）

```bash
PYTHONPATH=./src python3 -m ai_trading_coach.scheduler.scheduler_app
```

### 4) 运行 API（POST /at/chat）

```bash
PYTHONPATH=./src uvicorn ai_trading_coach.server:app --host 0.0.0.0 --port ${PORT:-8000}
```

接口：

- `POST /at/chat`
  - body：`{"user_message": "..."}`  
  - 模型：`gpt-5`（可用 `LLM_MODEL` 覆盖）

## Discord 推送策略（不刷屏）

- 推送正文只发：**标题 + 纪律分 + P0/P1/P2 + Top3重点问题（不放证据ID）**
- 详细内容（证据/列表/规则）放在 **Markdown 附件**

## 回溯窗口（同步时什么意思）

同步会从某个 `since` 时间点开始拉成交（例如最近 7 天）。这叫“回溯窗口”，目的只有一个：**防漏**（延迟/错过运行/分页遗漏）。它不涉及任何预测或交易逻辑。

## 已知限制 / 后续会补

- “开仓时可用保证金余额”需要从 Bitget 账户接口取历史/近似快照并写入 lifecycle（用于 5% 大亏/有效杠杆事件）。

## GitHub 推送前必须确认（隐私与安全）

- **不要提交**：`.env`、`venv/`、`data/*.sqlite3`（本项目已提供 `.gitignore`）
- `data/*.sqlite3` 里包含你的成交与复盘结果：默认视为敏感数据

## 部署（Koyeb/类似平台）

- 本项目提供 `Dockerfile`，启动命令会监听平台注入的 `PORT`（例如 8001）
- 如果平台要求你填写端口，确保与 `PORT` 一致（平台注入为准）
- 如果你只部署一个服务：把 `ENABLE_SCHEDULER=1`，这样 API + 定时复盘会在同一个进程里运行（**注意保持 uvicorn 单进程/单 worker**，避免重复执行任务）


