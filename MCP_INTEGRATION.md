# MCP 集成说明

本项目基于**麦当劳中国官方 MCP Server** 开发。本文说明实际使用的 Server、Tool、
调用流程与业务价值。

## Server 信息

| 项 | 值 |
|---|---|
| Server 名称 | `mcd-mcp`（官方仓库示例命名） |
| 接入地址 | `https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP |
| 鉴权方式 | `Authorization: Bearer <MCP_TOKEN>` |
| Token 申请 | https://open.mcd.cn/mcp |
| 服务规则 | https://cdn.mcd.cn/cms/pages/MCPServerRules.html |
| 官方仓库 | https://github.com/M-China/mcd-mcp-server |

配置文件见 [`mcp-config.example.json`](mcp-config.example.json)，
其中 Token 使用环境变量占位符 `${MCD_MCP_TOKEN}`，仓库内不含任何真实凭证。

## 实际使用的 Tool

### 核心依赖

#### `list-nutrition-foods` —— 餐品营养信息列表

**这是本 Skill 唯一的硬依赖。** 返回 160 款麦当劳常见餐品的官方营养成分：

```
productName, nutritionDescription, energyKj, energyKcal,
protein, fat, carbohydrate, sodium, calcium
```

实测返回（截取）：

```
[160]{productName,...,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
  巨无霸,null,2146,513,27,26,42,961,171
  板烧鸡腿堡,null,1638,391,23,17,35,1041,93
  麦香鱼,null,1359,325,16,13,35,556,99
```

> **接入注意**：返回体是 MCP 封装的文本，其中的 `data` 字段又是**扁平化文本**
> （非 JSON 数组）。需先提取 `## Original Response` 段，再按逗号切分、跳过首行表头。
> 直接 `json.loads` 会失败。

实测数据特征：

| 指标 | 数值 |
|---|---|
| 餐品总数 | 160 |
| 蛋白 ≥20g 的高蛋白单品 | 26 款 |
| 钠 ≤500mg 的低钠单品 | 111 款 |
| 蛋白密度最高 | 蒜了吧黄油风味薄皮脆汁鸡-带骨里脊（19g / 126kcal） |

### 延伸调用（用户确认方案后）

| Tool | 用途 | 触发时机 |
|---|---|---|
| `calculate-price` | 估算单餐花费 | 用户关心预算时 |
| `query-meals` | 确认门店实际在售 | 方案需与真实菜单核对时 |
| `query-meal-detail` | 查看套餐组成与可替换项 | 用户想换配菜时 |
| `query-store-coupons` | 查询可用优惠券 | 用户想叠加优惠时 |
| `query-nearby-stores` | 查询附近门店 | 需要确定下单门店时 |
| `create-order` | 创建订单 | **用户明确确认后** |

### 未使用的 Tool

`query-my-account`、`mall-*`、`campaign-calendar`、`order-list`、
`party-*`、`draw-lottery` 等与健身营养场景无关的工具均未调用。

## 调用流程

```
用户输入体重与训练目标
        │
        ├─→ list-nutrition-foods ──→ 解析 160 款营养数据
        │                │
        │                ↓
        │      solve_meal.py 求解
        │      （热量 + 蛋白 + 钠 三目标代价函数）
        │                │
        │                ↓
        │      输出方案：热量偏差 / 蛋白缺口 / 钠预警
        │                │
        └────────────────┘
                         │
              用户确认方案
                         │
        ┌────────────────┼────────────────┐
        ↓                ↓                ↓
  calculate-price   query-meals    query-store-coupons
 （预算约束）      （在售核对）      （优惠叠加）
        │                │                │
        └────────────────┴────────────────┘
                         │
                    create-order
                  （需用户明确确认）
```

## 业务价值

### 解决的真实痛点

健身人群与麦当劳的冲突是**结构性的**：门店密度高、性价比高、便利性强，
但传统印象是高油高钠。现有解法有两个极端——

1. **完全戒断**：放弃Convenience，代价是依从性差，难以长期维持
2. **无视营养**：照常吃，代价是减脂/增肌目标被悄悄破坏

中间地带——**「在真实菜单里找营养最优解」**——几乎没有被工具化。

### 本项目的价值

| 价值点 | 说明 |
|---|---|
| **可执行** | 输出麦当劳真实品名，不是「鸡胸肉沙拉」这类无法落地的建议 |
| **可验证** | 每个数字对应官方营养字段，求解器可复算 |
| **可落地** | 方案确认后可直接下单，闭环而非纸上谈兵 |
| **不对抗** | 不劝退用户，而是让用户在该吃的场景下吃得更聪明 |

### 与通用大模型回答的差异

通用模型能给出合理的营养建议，但无法回答「麦当劳今天有什么、各多少大卡、
哪几样能凑到 735 大卡」。这个缺口只有**真实菜单数据 + 求解算法**能补上。

### 与同赛道参赛作品的差异

排行榜中已有多款营养/热量类作品（如 `mcd-calorie-budget-skill`、
`mcd-macro-strategist`、`mcd-nutrition-optimizer`）。本项目的差异化在于：

1. **求解而非查询** —— 不是「列出高蛋白单品」，而是在全量 SKU 上做组合优化
2. **多目标代价函数** —— 首次将钠负荷与蛋白缺口同时纳入优化，而非只看热量
3. **反退化约束** —— 饮品不可当主食、配餐设热量下限，杜绝「数字达标但无意义」的组合

## 速率与合规

- 官方限流：**每Token 每分钟最多 600 次请求**，超限返回 429
- 本 Skill 单次对话典型调用 1~3 次，远低于限额
- 遵守《麦当劳 MCP 服务规则》，仅用于个人非商业用途的连接与使用