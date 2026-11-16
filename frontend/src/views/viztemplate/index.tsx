import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/viztemplate'
const columns = [
  "模板编号",
  "图表类型",
  "数据绑定",
  "颜色方案",
  "坐标轴配置",
  "图例位置",
  "交互行为",
  "导出格式"
]
const actions = [
  "提交模板评审",
  "发布模板",
  "废弃模板"
]
const statuses = [
  "草稿",
  "模板评审中",
  "已发布",
  "已废弃"
]
const exportFormats = ['csv', 'json', 'tsv', 'ndjson']
const stats = [
  {
    "label": "图表模板总数",
    "value": 0
  },
  {
    "label": "已发布模板",
    "value": 0
  },
  {
    "label": "评审中模板",
    "value": 0
  }
]

type Row = Record<string, unknown>

type BindingCheck = {
  ok: boolean
  error: string | null
  service: { 服务编号?: string; 服务名称?: string; status?: string } | null
  contract_version: number | null
  contract_hash: string | null
  columns: string[]
  conflicts: { 旧字段: string; 新字段: string | null; 说明: string }[]
  warnings: string[]
}

type SnapshotSummary = {
  快照编号: string
  快照版本: number
  模板版本: number
  有效: boolean
  契约版本: number | null
  列顺序: string[]
  生成时间: string
}

type RenderPayload = {
  模式: string
  快照编号: string
  快照版本: number | null
  列顺序: string[]
  契约版本: number | null
  契约哈希: string | null
  样式: Record<string, unknown>
  配色: { 调色板: string[]; 字段配色: Record<string, string>; 配色指纹: string }
  图表: {
    图表类型: string | null
    维度: string | null
    度量: string[]
    颜色: Record<string, string>
    坐标轴: Record<string, unknown>
    图例位置: string
    交互行为: Record<string, unknown>
    导出格式: string
    数据列: string[]
  }
  数据: { columns: string[]; rows: Record<string, unknown>[]; row_count: number }
  指纹: Record<string, string | null>
  只读: boolean
}

type Detail = Row & {
  绑定解析?: BindingCheck
  样式视图?: Record<string, unknown>
  绑定状态?: string
  快照清单?: SnapshotSummary[]
  核对?: { 一致: boolean; 说明: string; 检查项: { 检查项: string; 一致: boolean; 快照值: unknown; 当前值: unknown }[] }
}

function text(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'object') return JSON.stringify(value, null, 0)
  return String(value)
}

function bindingText(value: unknown): string {
  if (value && typeof value === 'object') {
    const binding = value as Record<string, unknown>
    const service = text(binding['服务编号'] ?? binding.service_no)
    const fields = Array.isArray(binding['字段']) ? (binding['字段'] as unknown[]).join(' > ') : ''
    const version = binding['契约版本'] ?? binding.contract_version
    return [service, version ? `v${version}` : '', fields].filter(Boolean).join(' · ')
  }
  return text(value)
}

export default function ViztemplateView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [infoMessage, setInfoMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [detail, setDetail] = useState<Detail | null>(null)
  const [render, setRender] = useState<RenderPayload | null>(null)
  const [activeSnapshot, setActiveSnapshot] = useState<SnapshotSummary | null>(null)
  const [previewError, setPreviewError] = useState('')
  const [showRegister, setShowRegister] = useState(false)
  const [registerValues, setRegisterValues] = useState<Record<string, string>>({
    模板编号: '',
    图表类型: '折线图',
    数据绑定: '',
    颜色方案: '',
    坐标轴配置: '',
    图例位置: '顶部',
    交互行为: '',
    导出格式: 'csv',
  })
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('图表模板列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '可视化模板列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  const loadDetail = useCallback(async (entryId: number) => {
    setSelectedId(entryId)
    setPreviewError('')
    try {
      const response = await request(`${ENDPOINT}/${entryId}`)
      if (!response.ok) {
        throw new Error('图表模板详情读取失败')
      }
      const payload = (await response.json()) as Detail
      setDetail(payload)
      setActiveSnapshot(null)
      await renderTemplate(entryId)
    } catch (error) {
      setDetail(null)
      setRender(null)
      setPreviewError(error instanceof Error ? error.message : '图表模板详情读取失败')
    }
  }, [])

  async function renderTemplate(entryId: number) {
    setPreviewError('')
    const response = await request(`${entryPath(entryId)}/preview`)
    if (!response.ok) {
      const body = await response.json().catch(() => ({}))
      setRender(null)
      setPreviewError(body.detail ?? '图表渲染失败')
      return
    }
    setRender((await response.json()) as RenderPayload)
  }

  async function viewSnapshot(entryId: number, snapshot: SnapshotSummary) {
    setActiveSnapshot(snapshot)
    setPreviewError('')
    const response = await request(`${entryPath(entryId)}/snapshots/${snapshot.快照版本}`)
    if (!response.ok) {
      setPreviewError('历史快照读取失败')
      return
    }
    const body = await response.json()
    setRender({
      模式: `历史快照 v${snapshot.快照版本}`,
      快照编号: body.快照编号,
      快照版本: body.快照版本,
      列顺序: body.列顺序,
      契约版本: body.契约版本,
      契约哈希: body.契约哈希,
      样式: body.样式,
      配色: body.配色,
      图表: body.图表,
      数据: body.数据,
      指纹: {
        样式: body.样式指纹,
        绑定: body.绑定指纹,
        数据: body.数据指纹,
        配置: body.配置指纹,
        规则: body.规则版本,
      },
      只读: true,
    })
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setInfoMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      const body = await response.json().catch(() => ({}))
      if (!response.ok || body.ok === false) {
        throw new Error(body.message ?? '可视化模板动作未生效，请稍后重试')
      }
      setInfoMessage(body.message ?? '')
      await reload()
      if (selectedId === Number(row.id)) {
        await loadDetail(Number(row.id))
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '可视化模板操作失败')
    }
  }

  async function saveStyles() {
    if (!detail) return
    setErrorMessage('')
    setInfoMessage('')
    try {
      const response = await request(`${ENDPOINT}/${detail.id}`, {
        method: 'PUT',
        body: JSON.stringify({
          values: {
            expected_version: detail['模板版本'],
            颜色方案: parseJsonField(styleDraft['颜色方案']),
            坐标轴配置: parseJsonField(styleDraft['坐标轴配置']),
            图例位置: styleDraft['图例位置'],
            交互行为: parseJsonField(styleDraft['交互行为']),
            导出格式: styleDraft['导出格式'],
          },
        }),
      })
      const body = await response.json().catch(() => ({}))
      if (!response.ok || body.ok === false) {
        throw new Error(body.message ?? '模板配置保存失败')
      }
      setInfoMessage(body.message ?? '模板配置已保存')
      await reload()
      await loadDetail(Number(detail.id))
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '模板配置保存失败')
    }
  }

  async function registerTemplate() {
    setErrorMessage('')
    setInfoMessage('')
    try {
      const response = await request(ENDPOINT, {
        method: 'POST',
        body: JSON.stringify({
          values: {
            模板编号: registerValues['模板编号'],
            图表类型: registerValues['图表类型'],
            数据绑定: parseJsonField(registerValues['数据绑定']),
            颜色方案: parseJsonField(registerValues['颜色方案']),
            坐标轴配置: parseJsonField(registerValues['坐标轴配置']),
            图例位置: registerValues['图例位置'],
            交互行为: parseJsonField(registerValues['交互行为']),
            导出格式: registerValues['导出格式'],
          },
        }),
      })
      const body = await response.json().catch(() => ({}))
      if (!response.ok || body.ok === false) {
        throw new Error(body.message ?? '图表模板登记失败')
      }
      setInfoMessage(body.message ?? '图表模板已登记')
      setShowRegister(false)
      await reload()
      if (body.entry?.id) {
        await loadDetail(Number(body.entry.id))
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '图表模板登记失败')
    }
  }

  async function syncBinding() {
    if (!detail) return
    setErrorMessage('')
    setInfoMessage('')
    const response = await request(`${ENDPOINT}/${detail.id}/sync-binding`, { method: 'POST' })
    const body = await response.json().catch(() => ({}))
    if (!response.ok || body.ok === false) {
      setErrorMessage(body.message ?? '绑定同步失败')
      return
    }
    setInfoMessage(body.message ?? '数据绑定已同步')
    await reload()
    await loadDetail(Number(detail.id))
  }

  async function verifySnapshot() {
    if (!detail) return
    setErrorMessage('')
    setInfoMessage('')
    const response = await request(`${entryPath(Number(detail.id))}/verify`, {
      method: 'POST',
      body: JSON.stringify({ values: activeSnapshot ? { version: activeSnapshot.快照版本 } : {} }),
    })
    const body = await response.json().catch(() => ({}))
    if (!response.ok) {
      setErrorMessage(body.detail ?? '快照核对失败')
      return
    }
    setDetail((previous) => (previous ? { ...previous, 核对: body } : previous))
    setInfoMessage(body.一致 ? '快照核对一致' : body.说明)
  }

  const [styleDraft, setStyleDraft] = useState<Record<string, string>>({})

  useEffect(() => {
    if (!detail) {
      setStyleDraft({})
      return
    }
    setStyleDraft({
      颜色方案: JSON.stringify(detail['颜色方案'] ?? {}, null, 2),
      坐标轴配置: JSON.stringify(detail['坐标轴配置'] ?? {}, null, 2),
      图例位置: String(detail['图例位置'] ?? ''),
      交互行为: JSON.stringify(detail['交互行为'] ?? {}, null, 2),
      导出格式: String(detail['导出格式'] ?? 'csv'),
    })
  }, [detail])

  return (
    <section className="page" data-module="viztemplate">
      <header className="page-head">
        <div>
          <h2>可视化模板管理</h2>
          <p className="page-desc">图表模板保存样式与交互配置，页面预览、图表快照与导出文件共用同一份数据绑定快照。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setShowRegister((value) => !value)}>
            登记图表模板
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出可视化模板清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      {showRegister && (
        <form
          className="panel"
          onSubmit={(event) => {
            event.preventDefault()
            void registerTemplate()
          }}
        >
          <h3>登记图表模板</h3>
          <div className="form-grid">
            <label>
              <span>模板编号</span>
              <input
                value={registerValues['模板编号']}
                placeholder="例如 VIZT-0009"
                onChange={(event) => setRegisterValues((prev) => ({ ...prev, 模板编号: event.target.value }))}
              />
            </label>
            <label>
              <span>图表类型</span>
              <input
                value={registerValues['图表类型']}
                onChange={(event) => setRegisterValues((prev) => ({ ...prev, 图表类型: event.target.value }))}
              />
            </label>
            <label className="wide">
              <span>数据绑定（JSON：服务编号 / 契约版本 / 字段 / x / y / series）</span>
              <textarea
                rows={3}
                value={registerValues['数据绑定']}
                placeholder='{"服务编号":"DATA-0001","字段":["dt","region","active_users"],"x":"dt","y":["active_users"]}'
                onChange={(event) => setRegisterValues((prev) => ({ ...prev, 数据绑定: event.target.value }))}
              />
            </label>
            <label>
              <span>颜色方案（JSON 或色值列表）</span>
              <textarea
                rows={2}
                value={registerValues['颜色方案']}
                placeholder='{"调色板":["#1f6feb","#16a34a"]}'
                onChange={(event) => setRegisterValues((prev) => ({ ...prev, 颜色方案: event.target.value }))}
              />
            </label>
            <label>
              <span>坐标轴配置（JSON）</span>
              <textarea
                rows={2}
                value={registerValues['坐标轴配置']}
                placeholder='{"x轴":{"字段":"dt"},"显示网格":true}'
                onChange={(event) => setRegisterValues((prev) => ({ ...prev, 坐标轴配置: event.target.value }))}
              />
            </label>
            <label>
              <span>图例位置</span>
              <input
                value={registerValues['图例位置']}
                onChange={(event) => setRegisterValues((prev) => ({ ...prev, 图例位置: event.target.value }))}
              />
            </label>
            <label>
              <span>交互行为（JSON 或开关列表）</span>
              <textarea
                rows={2}
                value={registerValues['交互行为']}
                placeholder='{"提示框":true,"缩放":true}'
                onChange={(event) => setRegisterValues((prev) => ({ ...prev, 交互行为: event.target.value }))}
              />
            </label>
            <label>
              <span>导出格式</span>
              <select
                value={registerValues['导出格式']}
                onChange={(event) => setRegisterValues((prev) => ({ ...prev, 导出格式: event.target.value }))}
              >
                {exportFormats.map((format) => <option key={format} value={format}>{format}</option>)}
              </select>
            </label>
          </div>
          <div className="page-actions">
            <button className="btn primary" type="submit">提交登记</button>
            <button className="btn ghost" type="button" onClick={() => setShowRegister(false)}>取消</button>
          </div>
        </form>
      )}

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <label className="filter-item">
          <span>绑定状态</span>
          <select
            value={filters.binding_status ?? ''}
            onChange={(event) => setFilters((prev) => ({ ...prev, binding_status: event.target.value }))}
          >
            <option value="">全部</option>
            <option value="可用">可用</option>
            <option value="待核对">待核对</option>
            <option value="冲突">冲突</option>
            <option value="不可用">不可用</option>
          </select>
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>绑定状态</th>
            <th>快照</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)} className={selectedId === Number(row.id) ? 'row-selected' : undefined}>
              {columns.map((column) => (
                <td key={column} title={text(row[column])}>
                  {column === '数据绑定' ? bindingText(row[column]) : text(row[column])}
                </td>
              ))}
              <td>{text(row['绑定状态'])}{row['绑定契约版本'] ? ` · v${text(row['绑定契约版本'])}` : ''}</td>
              <td>{row['快照编号'] ? `${text(row['快照编号'])}` : '—'}</td>
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void loadDetail(Number(row.id))}>查看</button>
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 3}>暂无可视化模板数据，可先登记图表模板</td>
            </tr>
          )}
        </tbody>
      </table>

      {detail && (
        <section className="panel detail-panel">
          <header className="panel-head">
            <h3>{text(detail['模板编号'])} · {text(detail['图表类型'])}</h3>
            <span className={`badge ${detail['只读'] ? 'badge-muted' : 'badge-ok'}`}>
              {text(detail['status'])}{detail['只读'] ? '（只读）' : ''}
            </span>
          </header>

          <div className="detail-grid">
            <div>
              <h4>数据绑定核对</h4>
              <p className="hint">
                服务 {text(detail['绑定服务'])} · 契约 v{text(detail['绑定契约版本'])} ·
                状态 {text(detail['绑定状态'])}
              </p>
              <p className="hint">页面列顺序：{(detail['绑定列'] as string[] | undefined)?.join(' > ') || '—'}</p>
              {detail['绑定解析']?.warnings?.map((warning) => (
                <p key={warning} className="warning-text">{warning}</p>
              ))}
              {detail['绑定解析']?.conflicts?.map((conflict) => (
                <p key={`${conflict.旧字段}-${conflict.新字段}`} className="error-text">{conflict.说明}</p>
              ))}
              <div className="page-actions">
                <button className="btn" type="button" onClick={() => void syncBinding()}>按当前契约同步绑定</button>
                <button className="btn" type="button" onClick={() => window.open(`${entryPath(Number(detail.id))}/export`, '_blank')}>
                  导出图表数据
                </button>
                <button className="btn" type="button" onClick={() => window.open(`${entryPath(Number(detail.id))}/bundle`, '_blank')}>
                  下载图表包
                </button>
                <button className="btn" type="button" onClick={() => window.open(`${entryPath(Number(detail.id))}/bundle?archive=true`, '_blank')}>
                  下载图表包（zip）
                </button>
                <button className="btn" type="button" onClick={() => void verifySnapshot()}>重新载入核对</button>
              </div>
            </div>

            <div>
              <h4>模板样式</h4>
              <label className="stacked">
                <span>颜色方案</span>
                <textarea rows={3} value={styleDraft['颜色方案'] ?? ''} onChange={(event) => setStyleDraft((prev) => ({ ...prev, 颜色方案: event.target.value }))} />
              </label>
              <label className="stacked">
                <span>坐标轴配置</span>
                <textarea rows={3} value={styleDraft['坐标轴配置'] ?? ''} onChange={(event) => setStyleDraft((prev) => ({ ...prev, 坐标轴配置: event.target.value }))} />
              </label>
              <label className="stacked">
                <span>图例位置</span>
                <input value={styleDraft['图例位置'] ?? ''} onChange={(event) => setStyleDraft((prev) => ({ ...prev, 图例位置: event.target.value }))} />
              </label>
              <label className="stacked">
                <span>交互行为</span>
                <textarea rows={3} value={styleDraft['交互行为'] ?? ''} onChange={(event) => setStyleDraft((prev) => ({ ...prev, 交互行为: event.target.value }))} />
              </label>
              <label className="stacked">
                <span>导出格式</span>
                <select value={styleDraft['导出格式'] ?? 'csv'} onChange={(event) => setStyleDraft((prev) => ({ ...prev, 导出格式: event.target.value }))}>
                  {exportFormats.map((format) => <option key={format} value={format}>{format}</option>)}
                </select>
              </label>
              <button className="btn primary" type="button" disabled={detail['只读'] === true} onClick={() => void saveStyles()}>
                保存样式配置
              </button>
            </div>
          </div>

          <h4>图表渲染（读取模板配置与数据绑定快照）</h4>
          {previewError && <p className="error-text">{previewError}</p>}
          {render && <ChartPreview payload={render} />}

          <h4>快照列表</h4>
          <table className="data-table">
            <thead>
              <tr><th>快照编号</th><th>版本</th><th>模板版本</th><th>契约版本</th><th>列顺序</th><th>有效</th><th>生成时间</th><th>操作</th></tr>
            </thead>
            <tbody>
              {(detail['快照清单'] ?? []).map((snapshot) => (
                <tr key={snapshot.快照编号}>
                  <td>{snapshot.快照编号}</td>
                  <td>v{snapshot.快照版本}</td>
                  <td>v{snapshot.模板版本}</td>
                  <td>v{snapshot.契约版本 ?? '—'}</td>
                  <td>{snapshot.列顺序.join(' > ')}</td>
                  <td>{snapshot.有效 ? '是' : '否'}</td>
                  <td>{snapshot.生成时间}</td>
                  <td>
                    <button className="link" type="button" onClick={() => void viewSnapshot(Number(detail.id), snapshot)}>查看</button>
                  </td>
                </tr>
              ))}
              {(detail['快照清单'] ?? []).length === 0 && (
                <tr><td className="empty-state" colSpan={8}>暂无快照，发布模板后会生成</td></tr>
              )}
            </tbody>
          </table>

          {detail['核对'] && (
            <div className="verify-box">
              <p className={detail['核对'].一致 ? 'ok-text' : 'error-text'}>
                重新载入核对：{detail['核对'].一致 ? '一致' : '不一致'} · {detail['核对'].说明}
              </p>
              <ul>
                {detail['核对'].检查项.map((item) => (
                  <li key={item.检查项} className={item.一致 ? 'ok-text' : 'error-text'}>
                    {item.检查项}：{item.一致 ? '一致' : `快照 ${text(item.快照值)} / 当前 ${text(item.当前值)}`}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条可视化模板记录</span>
        <span>
          {infoMessage && <span className="ok-text">{infoMessage}</span>}
          {errorMessage && <span className="error-text">{errorMessage}</span>}
        </span>
      </footer>
    </section>
  )
}

function entryPath(entryId: number): string {
  return `${ENDPOINT}/${entryId}`
}

function parseJsonField(raw: string | undefined): unknown {
  const value = (raw ?? '').trim()
  if (!value) return undefined
  try {
    return JSON.parse(value)
  } catch {
    return value
  }
}

function ChartPreview({ payload }: { payload: RenderPayload }) {
  const columns = payload.数据?.columns ?? payload.列顺序 ?? []
  const rows = payload.数据?.rows ?? []
  const palette = payload.配色?.调色板 ?? []
  return (
    <div className="chart-preview">
      <p className="hint">
        快照 {text(payload.快照编号)} · 契约 v{text(payload.契约版本)} · 列顺序 {columns.join(' > ')}
      </p>
      <div className="chart-bars">
        {rows.slice(0, 12).map((row, index) => {
          const dimension = payload.图表?.维度 ? text(row[payload.图表.维度]) : String(index + 1)
          const measures = payload.图表?.度量 ?? []
          return (
            <div key={`${dimension}-${index}`} className="chart-bar-row">
              <span className="chart-label">{dimension}</span>
              {measures.map((measure, measureIndex) => {
                const raw = Number(row[measure] ?? 0)
                const max = Math.max(
                  1,
                  ...rows.map((candidate) => Number(candidate[measures[0]] ?? 0)),
                )
                const width = Math.min(100, Math.max(2, (Math.abs(raw) / max) * 100))
                const color = payload.图表?.颜色?.[measure] ?? palette[measureIndex % (palette.length || 1)] ?? '#1f6feb'
                return (
                  <span key={measure} className="chart-bar" title={`${measure}: ${text(row[measure])}`}>
                    <span className="chart-bar-fill" style={{ width: `${width}%`, background: color }} />
                    <span className="chart-bar-value">{text(row[measure])}</span>
                  </span>
                )
              })}
            </div>
          )
        })}
      </div>
      <p className="hint">
        图例：{text(payload.样式['图例位置'])} ·
        交互：{Object.entries((payload.样式['交互行为'] as Record<string, unknown>) ?? {})
          .map(([key, value]) => `${key}${value ? '开' : '关'}`)
          .join('、')} ·
        导出格式：{text(payload.样式['导出格式'])}
      </p>
    </div>
  )
}
