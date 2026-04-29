# ConvertibleBond — 可转债理论定价引擎

A 股可转债的理论定价工具：Crank-Nicolson PDE 求解 + 强赎/回售/下修博弈 + Wind 数据集成 + Apple 风格 GUI。

## 特性

- **完整研究链路**：Wind 基础信息 → 主池准入筛选 → 批量理论定价 → 低估率/风险/流动性打分 → 复核视图
- **PDE 定价引擎**：Crank-Nicolson 隐式格式，三对角系统 `solve_banded` 加速
- **完整条款建模**：阶梯票息、强赎/回售触发、下修博弈、强赎宽限期 (notice window) 的 stock optionality
- **风险中性漂移 + 信用利差折现**：基础利差 + 股价驱动的 distress 扩张
- **完整希腊值**：Δ / Γ / ν / Θ + 价值分解 (纯债/转股/期权溢价)
- **隐含波动率反解**：Brent 求解, 自动避开越界目标
- **历史回测**：周/月频对比"理论价 vs 收盘价 vs 纯债底 vs 转股价值"，支持 IV vs HV 时序
- **现金流可视化**：完整付息计划 + 末期兑付一图展示
- **σ-S 敏感性热力图**：固定其他参数，遍历 (波动率, 正股价) 网格
- **分层自动取数**：转债基础信息固定由 Wind 写入 `cb_data`，正股行情 / Shibor 可选 Wind 或 akshare
- **主池准入过滤**：剔除不可交易、停牌、已公告强赎、临近摘牌、正股 ST/退市风险、低成交额、小余额、低评级等标的
- **GUI**：CustomTkinter + Catppuccin 配色 + 深浅色无缝切换

## 安装

```bash
git clone https://github.com/your/ConvertibleBond.git
cd ConvertibleBond
pip install -e ".[dev]"
```

> **WindPy 不通过 pip 发布**，需在 Wind 终端"插件管理"中将 Python 接口安装到当前 venv。
> 仅离线使用 `convertible_bond.pricer.UniversalCBPricer` 时无需 Wind。

## 快速使用

### GUI

```bash
cb-gui          # 通过 console_script 入口
# 或
python -m convertible_bond.gui.app
```

界面三个 tab：

- **⚡ 定价**：手填条款 / Wind 一键同步 → 单点定价 + 希腊值 + 隐含波动率
- **📈 回测**：历史区间逐点定价 → 理论 vs 市价对比 + IV/HV spread
- **🔥 敏感性**：σ-S 网格热力图

### CLI

```bash
# 兼容入口: 输入转债代码自动定价 (需 Wind)
python CB.py 128009.SZ
python CB.py 128009.SZ 2025-06-30   # 指定估值日

# 同步 Wind 基础信息到 data/cb_data.json
python -m convertible_bond.cli.sync_tradable

# 刷新停牌/强赎/摘牌/ST/成交额等主池准入状态字段
python -m convertible_bond.cli.sync_admission_status
cb-sync-admission-status --limit 50

# 同步公告标题并解析为结构化事件表
python -m convertible_bond.cli.sync_events --limit 50
python -m convertible_bond.cli.sync_events --codes 118006.SH --apply

# 查看批量定价前的主池准入筛选报告
python -m convertible_bond.cli.screen_pool
cb-screen-pool --min-rating AA- --min-balance 1
```

### Python API

```python
from datetime import date
from convertible_bond.pricer import UniversalCBPricer

pricer = UniversalCBPricer(
    S0=55.0, K=52.77,
    current_date=date(2026, 4, 20),
    maturity_date=date(2026, 7, 30),
    issue_date=date(2020, 7, 30),
    conversion_start_date=date(2021, 2, 6),
    coupon_rates=(0.003, 0.004, 0.008, 0.015, 0.018, 0.02),
    redemption_price=107.0,
    call_notice_days=30,        # 强赎宽限期 (新)
)

theo = pricer.price(sigma=0.28, r=0.022, base_spread=0.03,
                    distress_k=0.05, p_down=0.0)

# 含希腊值 + 价值分解
detailed = pricer.price(sigma=0.28, r=0.022, base_spread=0.03,
                        return_greeks=True)

# 反解隐含波动率
iv = pricer.solve_implied_vol(target_price=110.5, r=0.022, base_spread=0.03)
```

### 历史回测

```python
from datetime import date
from convertible_bond.backtest import backtest_theoretical_price

result = backtest_theoretical_price(
    bond_code="128009.SZ",
    start_date=date(2025, 1, 1),
    end_date=date(2025, 8, 31),
    freq="W",                # 日 / 周 / 月
    solve_iv=True,           # 反解每日 IV (~5x 计算量)
)
# result 包含: dates, theo_prices, market_prices, stock_prices,
#             sigmas, bond_floors, parities, ivs
```

## 项目主线

项目按五层组织，避免把“能定价”和“能筛选”混在一起：

1. **基础信息层**：WindPy 获取转债发行条款、票息、转股价、强赎/回售规则、评级、余额等半静态信息，写入 `data/cb_data.json`。
2. **事件状态层**：`data/cb_events.json` 记录下修、不下修、强赎、回售、摘牌等公告事件；`sync_admission_status` 刷新停牌、ST、成交额等状态字段；字段明确命中风险时先从主池剔除。
3. **动态行情层**：Wind 或 akshare 获取正股/转债收盘价、正股历史波动率、无风险利率等定价输入。
4. **模型定价层**：`UniversalCBPricer` 输出理论价、希腊值、纯债/转股/期权价值分解。
5. **筛选打分层**：批量结果计算 `undervaluation_rate = (理论价 - 市价) / 理论价`，再结合转股溢价、HV、余额、评级、剩余期限等生成 `opportunity_score`、`risk_tags`、`confidence` 和复核视图。

每日推荐流程：

```text
Wind 同步基础信息
→ 刷新停牌/强赎/摘牌/ST/成交额等准入状态
→ 同步公告事件并应用到 cb_data
→ 主池准入筛选
→ 更新行情/利率/HV
→ 批量理论定价
→ 计算低估率和机会分
→ 输出低估候选、转股折价、需复核、关注池
```

## 模型说明

### PDE

求解 Black-Scholes-Merton 类型的可转债 PDE：

```text
∂V/∂t + ½σ²S² ∂²V/∂S² + rS ∂V/∂S − (r + s(S))V + 票息 = 0
```

设计取舍：

- **风险中性漂移用 r**, **折现用 r + spread(S)**：信用利差仅参与折现，不污染漂移
- **信用利差 distress 扩张**：`s(S) = base_spread + distress_k · max(0, 1 − S/K)`
- **下修概率 S-依赖**：`p_down` 按年化强度解释并折算到 PDE 时间步；S ≥ K 时为 0，S = 0 时取当期步进概率，线性插值
- **强赎宽限期**：触发后持有人留有 `call_notice_days` 行权窗口，cap 上抬 `parity·(1 + σ√t_grace)`，对应实务里 5–10% 的转股溢价
- **回售边界**：仅在到期前 `put_active_years` 年内生效

### 票息

使用半开区间 `(start, end]` 累计票息现金流，避免边界双计。末期 `is_final` 通过 `redemption_price` 一次性返还（含末期利息+面值+赎回溢价）。

## 项目结构

```text
ConvertibleBond/
├── convertible_bond/    # 主包
│   ├── pricer.py        # PDE 定价引擎
│   ├── pricing_api.py   # 自动取参与批量定价 helper
│   ├── backtest.py      # 历史回测与 Wind 辅助
│   ├── data_providers.py # Wind / akshare / CSV 数据源抽象
│   ├── cache.py          # cb_data 静态信息缓存 + 动态行情组合 provider
│   └── gui/             # CustomTkinter GUI 包
├── CB.py                # 顶层兼容入口
├── gui.py               # 顶层 GUI 启动入口
├── tests/
│   └── test_pricer.py   # pytest 套件
├── pyproject.toml
├── requirements.txt
└── README.md
```

## 测试

```bash
pytest                  # 覆盖回归 / 边界 / 票息 / IV / 希腊 / 强赎宽限期 / 回测
pytest -v              # 详细输出
pytest -k "Greeks"     # 只跑某一类
```

## 已知限制

1. **强赎触发是单点判断**，未建"30 个交易日中 15 日"的累积条款 — 倾向于高估强赎概率
2. **利率为标量** — 长期限债对利率曲线形状的敏感度未捕获
3. **无股息率参数** — A 股有分红时会系统性低估期权价值
4. **下修后立即重定 K** — 未建董事会决议+股东大会通过率
5. **历史回测忽略历史下修** — 用当前 K 反算所有日期，下修发生过的债会出现 K 跳点

## 许可

MIT
