import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/dataservice'
const columns = [
  "服务编号",
  "服务名称",
  "接口路径",
  "查询参数",
  "返回字段",
  "调用频次限制",
  "缓存策略",
  "服务版本"
]
const actions = [
  "提交服务评审",
  "上线服务",
  "下线服务"
]
const statuses = [
  "草稿",
  "服务评审中",
  "已上线",
  "已下线"
]
const stats = [
  {
    "label": "服务接口总数",
    "value": 0
  },
  {
    "label": "已上线服务",
    "value": 0
  },
  {
    "label": "评审中服务",
    "value": 0
  }
]

type Row = Record<string, unknown>

type ContractVersion = {
  契约版本: number
  字段: string[]
  别名: Record<string, string>
  状态: string
  生效: boolean
  契约哈希: string
  更新时间: string
}

type Detail = Row & {
  契约历史?: ContractVersion[]
  契约版本?: number
  契约哈希?: string
  字段?: string[]
  别名?: Record<string, string>
  只读?: boolean
}

type TemplateSnapshot = {
  模板编号: string
  模板版本: number | null
  快照编号: string | null
  快照版本: number | null
  绑定状态: string | null
  绑定契约版本: number | null
  绑定列: string[]
  快照列顺序: string[]
  只读: boolean
}

function text(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (Array.isArray(value)) return value.join(', ')
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

export default function DataserviceView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [infoMessage, setInfoMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [detail, setDetail] = useState<Detail | null>(null)
  const [templates, setTemplates] = useState<TemplateSnapshot[]>([])
  const [fieldDraft, setFieldDraft] = useState('')
  const [aliasDraft, setAliasDraft] = useState('')
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('服务接口列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据服务列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  const loadDetail = useCallback(async (entryId: number) => {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${entryId}`)
      if (!response.ok) {
        throw new Error('服务接口详情读取失败')
      }
      const payload = (await response.json()) as Detail
      setDetail(payload)
      setFieldDraft((payload['返回字段'] as string[] | undefined)?.join(', ') ?? '')
      setAliasDraft(JSON.stringify(payload['字段别名'] ?? {}, null, 2))
      await loadTemplates(String(payload['服务编号'] ?? ''))
    } catch (error) {
      setDetail(null)
      setTemplates([])
      setErrorMessage(error instanceof Error ? error.message : '数据服务详情读取失败')
    }
  }, [])

  async function loadTemplates(serviceNo: string) {
    if (!serviceNo) {
      setTemplates([])
      return
    }
    const response = await request(`/api/viztemplate?size=200`)
    if (!response.ok) {
      setTemplates([])
      return
    }
    const payload = await response.json()
    const related = ((payload.items ?? []) as Detail[]).filter((item) => {
      const binding = item['数据绑定']
      return binding !== null && typeof binding === 'object'
        ? (binding as Record<string, unknown>)['服务编号'] === serviceNo
        : String(binding ?? '').includes(serviceNo)
    })
    setTemplates(related.map((item) => ({
      模板编号: String(item['模板编号'] ?? ''),
      模板版本: item['模板版本'] as number | null,
      快照编号: (item['快照编号'] as string | null) ?? null,
      快照版本: item['快照版本'] as number | null,
      绑定状态: (item['绑定状态'] as string | null) ?? null,
      绑定契约版本: (item['绑定契约版本'] as number | null) ?? null,
      绑定列: (item['绑定列'] as string[]) ?? [],
      快照列顺序: (item['快照列顺序'] as string[]) ?? [],
      只读: item['status'] === '已废弃',
    })))
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setInfoMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action, expected_version: row['契约版本'] }),
      })
      const body = await response.json().catch(() => ({}))
      if (!response.ok || body.ok === false) {
        throw new Error(body.message ?? '数据服务动作未生效，请稍后重试')
      }
      setInfoMessage(body.message ?? '')
      await reload()
      if (detail && Number(detail.id) === Number(row.id)) {
        await loadDetail(Number(row.id))
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据服务操作失败')
    }
  }

  async function saveContract() {
    if (!detail) return
    setErrorMessage('')
    setInfoMessage('')
    const fields = fieldDraft.split(/[,，;；|\s]+/).map((item) => item.trim()).filter(Boolean)
    let aliases: Record<string, string> = {}
    try {
      const parsed = aliasDraft.trim() ? JSON.parse(aliasDraft) : {}
      aliases = Object.fromEntries(
        Object.entries(parsed as Record<string, unknown>).map(([key, value]) => [key, String(value)]),
      )
    } catch {
      setErrorMessage('字段别名需要是 JSON 对象，例如 {"day":"dt"}')
      return
    }
    try {
      const response = await request(`${ENDPOINT}/${detail.id}/contract`, {
        method: 'PUT',
        body: JSON.stringify({
          values: {
            返回字段: fields,
            字段别名: aliases,
            contract_version: detail['契约版本'],
            expected_contract_version: detail['契约版本'],
          },
        }),
      })
      const body = await response.json().catch(() => ({}))
      if (!response.ok || body.ok === false) {
        throw new Error(body.message ?? '服务契约升级失败')
      }
      setInfoMessage(body.message ?? '服务契约已升级')
      await reload()
      await loadDetail(Number(detail.id))
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '服务契约升级失败')
    }
  }

  return (
    <section className="page" data-module="dataservice">
      <header className="page-head">
        <div>
          <h2>数据服务管理</h2>
          <p className="page-desc">管理对外暴露的数据分析服务接口；返回字段按契约版本管理，图表模板绑定随契约同步。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('服务接口登记入口尚未接入审批流')}>
            登记服务接口
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出数据服务清单
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
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>契约版本</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{text(row[column])}</td>)}
              <td>v{text(row['契约版本'])}</td>
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void loadDetail(Number(row.id))}>查看契约</button>
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
              <td className="empty-state" colSpan={columns.length + 2}>暂无数据服务数据，可先登记服务接口</td>
            </tr>
          )}
        </tbody>
      </table>

      {detail && (
        <section className="panel detail-panel">
          <header className="panel-head">
            <h3>{text(detail['服务编号'])} · {text(detail['服务名称'])}</h3>
            <span className={`badge ${detail['只读'] ? 'badge-muted' : 'badge-ok'}`}>
              {text(detail['status'])}{detail['只读'] ? '（只读）' : ''}
            </span>
          </header>

          <div className="detail-grid">
            <div>
              <h4>当前契约 v{text(detail['契约版本'])}</h4>
              <p className="hint">契约哈希：{text(detail['契约哈希'])}</p>
              <label className="stacked">
                <span>返回字段（逗号分隔，顺序即默认列顺序）</span>
                <textarea rows={2} value={fieldDraft} onChange={(event) => setFieldDraft(event.target.value)} />
              </label>
              <label className="stacked">
                <span>字段别名（旧列名 → 新字段）</span>
                <textarea rows={4} value={aliasDraft} onChange={(event) => setAliasDraft(event.target.value)} />
              </label>
              <button className="btn primary" type="button" disabled={detail['只读'] === true} onClick={() => void saveContract()}>
                升级服务契约
              </button>

              <h4>契约版本历史</h4>
              <table className="data-table">
                <thead>
                  <tr><th>版本</th><th>字段</th><th>生效</th><th>状态</th><th>更新时间</th></tr>
                </thead>
                <tbody>
                  {(detail['契约历史'] ?? []).map((contract) => (
                    <tr key={`${contract.契约版本}-${contract.契约哈希}`}>
                      <td>v{contract.契约版本}</td>
                      <td>{contract.字段.join(', ')}</td>
                      <td>{contract.生效 ? '是' : '否'}</td>
                      <td>{contract.状态}</td>
                      <td>{contract.更新时间}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div>
              <h4>使用该契约的图表模板</h4>
              <p className="hint">数据服务契约升级后，模板绑定版本随模板版本一起更新，页面预览、快照和导出保持同一列顺序。</p>
              <table className="data-table">
                <thead>
                  <tr><th>模板</th><th>绑定状态</th><th>绑定契约</th><th>快照</th><th>列顺序</th></tr>
                </thead>
                <tbody>
                  {templates.map((item) => (
                    <tr key={item.模板编号}>
                      <td>{item.模板编号}（模板 v{text(item.模板版本)}）</td>
                      <td className={item.绑定状态 === '冲突' ? 'error-text' : undefined}>{text(item.绑定状态)}</td>
                      <td>v{text(item.绑定契约版本)}</td>
                      <td>{item.快照编号 ? `${item.快照编号}` : '未发布'}</td>
                      <td>{item.快照列顺序.length > 0 ? item.快照列顺序.join(' > ') : item.绑定列.join(' > ')}</td>
                    </tr>
                  ))}
                  {templates.length === 0 && (
                    <tr><td className="empty-state" colSpan={5}>没有模板绑定该服务</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条数据服务记录</span>
        <span>
          {infoMessage && <span className="ok-text">{infoMessage}</span>}
          {errorMessage && <span className="error-text">{errorMessage}</span>}
        </span>
      </footer>
    </section>
  )
}
