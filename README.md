# 实时指标计算与监控大盘平台

面向实时指标计算引擎、多维度下钻分析、告警阈值管理、指标异常检测、数据服务开放与可视化看板配置的数据分析与应用工程平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 实时指标 | `realtimemetric` | 指标定义 | 指标编号、指标名称、计算窗口秒 |
| 监控大盘 | `dashboard` | 大盘配置 | 大盘编号、大盘名称、包含指标组 |
| 多维下钻 | `drilldown` | 下钻路径 | 路径编号、起始维度、下钻层级序列 |
| 阈值管理 | `threshold` | 阈值规则 | 规则编号、关联指标、阈值下限 |
| 异常检测 | `anomalydetect` | 检测模型 | 模型编号、监控指标、检测算法 |
| 数据服务 | `dataservice` | 服务接口 | 服务编号、服务名称、接口路径、返回字段（版本化契约，含字段别名） |
| 告警历史 | `alarmhistory` | 告警记录 | 记录编号、告警规则、触发时间 |
| 同比环比 | `comparison` | 对比配置 | 配置编号、目标指标、对比类型 |
| 聚合配置 | `aggregate` | 聚合规则 | 规则编号、源数据粒度、目标粒度 |
| 筛选预设 | `filterpreset` | 预设模板 | 预设编号、预设名称、维度条件组 |
| 用户洞察 | `userinsight` | 洞察报告 | 报告编号、分析主题、目标人群 |
| 事件分析 | `eventanalysis` | 分析模型 | 模型编号、事件名称、分组维度 |
| 分群画像 | `segmentprofile` | 画像定义 | 画像编号、分群名称、筛选规则 |
| 趋势预测 | `trendforecast` | 预测方案 | 方案编号、预测指标、算法模型 |
| 可视化模板 | `viztemplate` | 图表模板 | 模板编号、图表类型、数据绑定、颜色方案、坐标轴配置、图例位置、交互行为、导出格式 |
| 订阅推送 | `subscription` | 订阅配置 | 订阅编号、订阅内容、接收人 |
| 维度映射 | `dimensionmap` | 映射规则 | 规则编号、原始维度名、标准维度名 |
| 数据质量分 | `qualityscore` | 评分规则 | 规则编号、数据集名称、完整性权重 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据分析与数据应用

## 可视化模板 / 数据服务 / 导出链路

图表模板的取数、渲染与导出只有一条调用链：

```text
模板配置（图表类型 / 颜色方案 / 坐标轴 / 图例 / 交互 / 导出格式）
  + 数据绑定（服务编号 + 字段 + x/y/series）
  -> 数据服务契约（版本化：字段、别名、契约哈希）
  -> 图表模板快照（冻结绑定、契约、配色、列顺序、数据）
  -> 页面预览 / 导出文件 / 图表包 / 重新载入核对
```

关键规则：

- 登记模板时五项样式配置与数据绑定一起持久化；未知字段也保留，不再只存模板编号、图表类型和数据绑定。
- 发布前核对绑定字段：字段不在数据服务契约中直接拦下；旧列名能通过契约别名唯一映射时记入冲突清单并按当前契约解析，
  页面列顺序不会被静默改写。
- 已废弃模板只读：不能修改配置、重新发布、导出或下载图表包，历史快照仍可读取核对。
- 同一模板的并发发布只保留一个有效快照；同一服务重复上线只保留一个生效契约版本。
- 数据服务契约升级后，模板绑定版本随模板版本自动同步（`/api/viztemplate/{id}/sync-binding` 可手动触发），
  页面预览、快照和导出文件使用同一份绑定解析和同一列顺序。
- 绑定在契约升级后无法解析时，已发布模板继续提供上一份完整快照，不会导出空图。
- 历史快照按生成时的规则与契约冻结，保持可读；新快照按当前规则（`2026.11-snapshot-v1`）写入。
- 回填批次按 `batch_id` 幂等：重复提交不重复写入，中断（`stop_after`）后从上一完整条目继续。

主要接口：

| 接口 | 说明 |
| --- | --- |
| `POST /api/viztemplate`、`PUT /api/viztemplate/{id}` | 登记 / 修改模板（含样式、交互、导出格式） |
| `POST /api/viztemplate/{id}/actions` | 提交评审、发布（发布前核对绑定）、废弃（转只读） |
| `GET /api/viztemplate/{id}/preview[?version=快照版本]` | 读取模板配置并渲染数据图表 |
| `GET /api/viztemplate/{id}/snapshots`、`GET /api/viztemplate/{id}/snapshots/{version}` | 快照列表与历史快照读取 |
| `GET /api/viztemplate/{id}/export?format=csv\|json\|tsv\|ndjson` | 导出图表数据（列顺序取快照，响应头带快照/契约/指纹） |
| `GET /api/viztemplate/{id}/bundle[?archive=true]` | 下载图表包（manifest + snapshot + data.csv） |
| `POST /api/viztemplate/{id}/verify` | 重新载入核对模板与快照、导出是否一致 |
| `POST /api/viztemplate/backfill` | 快照回填（幂等、可续跑） |
| `PUT /api/dataservice/{id}/contract` | 升级服务契约（字段 / 别名），自动同步下游模板 |
| `GET /api/dataservice/{id}/contracts` | 契约版本历史 |

测试：`cd backend && .venv/bin/python tests/test_viztemplate_flow.py`
