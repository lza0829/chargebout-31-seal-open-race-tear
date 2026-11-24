import { useEffect, useState } from 'react'
import { api } from '../api'

export default function DaySeal() {
  const [d, setD] = useState(null)
  const [hist, setHist] = useState([])
  const [ledger, setLedger] = useState([])
  const [receipts, setReceipts] = useState([])
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState(false)

  const load = async () => {
    const [day, h, l, r] = await Promise.all([
      api.充日(),
      api.充日历史(),
      api.充日账本(),
      api.开充回执(),
    ])
    setD(day)
    setHist(h)
    setLedger(l)
    setReceipts(r)
  }

  useEffect(() => {
    load()
  }, [])

  const seal = async () => {
    setMsg('')
    setErr(false)
    try {
      // 带上当前世代：世代已被并发推进则 409 让路，而不是盲目覆盖。
      const r = await api.封印('日终封印', d?.封印世代 ?? null)
      setMsg(`已封印 · 世代 ${r.锁账世代} · 当时在途 ${r.在途会话数} 笔`)
      await load()
    } catch (e) {
      setErr(true)
      setMsg(e.message)
      await load()
    }
  }

  const unseal = async () => {
    setMsg('')
    setErr(false)
    try {
      await api.解封(d?.封印世代 ?? null)
      setMsg('已解封')
      await load()
    } catch (e) {
      setErr(true)
      setMsg(e.message)
      await load()
    }
  }

  if (!d) return null
  return (
    <div className="page">
      <h2>充日封印</h2>
      <p className="lead">
        锁账世代优先：封印提交后，任何并发开充整笔让路——不留会话、不动占用、不推进序号、不落回执。
      </p>

      <div className="grid">
        <div className="stat">
          <div className="k">营业日</div>
          <div className="v" style={{ fontSize: '1.2rem' }}>{d.营业日}</div>
        </div>
        <div className="stat">
          <div className="k">锁账徽标</div>
          <div className="v" style={{ fontSize: '1.3rem' }}>{d.锁账徽标}</div>
        </div>
        <div className="stat">
          <div className="k">封印世代</div>
          <div className="v">{d.封印世代}</div>
        </div>
        <div className="stat">
          <div className="k">未结会话</div>
          <div className="v">{d.未结会话数}</div>
        </div>
        <div className="stat">
          <div className="k">账本序号</div>
          <div className="v">{d.账本序号}</div>
        </div>
      </div>

      <div className="panel">
        <div className="row">
          <button type="button" className="primary" onClick={seal} disabled={d.已封印}>
            封印本日
          </button>
          <button type="button" onClick={unseal} disabled={!d.已封印}>
            解封
          </button>
          <button type="button" onClick={load}>刷新</button>
        </div>
        <p className={`msg ${msg && !err ? 'ok' : ''}`} style={err ? { color: 'var(--danger)' } : undefined}>
          {msg}
        </p>
        <p className="lead" style={{ marginBottom: 0 }}>
          世代事件：{d.世代事件.动作 || '尚无'} · 世代 {d.世代事件.世代} · 序号 {d.世代事件.序号} · 未结快照{' '}
          {d.世代事件.未结快照}
        </p>
      </div>

      <div className="panel">
        <h3>未结会话（封印会带上这些快照）</h3>
        <table>
          <thead>
            <tr>
              <th>会话号</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            {d.未结会话.length === 0 && (
              <tr>
                <td colSpan={2}>无</td>
              </tr>
            )}
            {d.未结会话.map((s) => (
              <tr key={s.会话编号}>
                <td>{s.会话号}</td>
                <td><span className={`tag ${s.状态}`}>{s.状态}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h3>争用结局账本（可重放）</h3>
        <p className="lead">封印世代事件与开充回执按同一账本序号合流；序号越大越晚。</p>
        <table>
          <thead>
            <tr>
              <th>序号</th>
              <th>类型</th>
              <th>摘要</th>
              <th>世代</th>
              <th>时间</th>
            </tr>
          </thead>
          <tbody>
            {ledger.length === 0 && (
              <tr>
                <td colSpan={5}>尚无账本记录</td>
              </tr>
            )}
            {ledger.map((x, i) => (
              <tr key={`${x.类型}-${x.序号}-${i}`}>
                <td>{x.序号}</td>
                <td>{x.类型}</td>
                <td>
                  {x.类型 === '封印世代'
                    ? x.动作
                    : `${x.回执号} → ${x.会话号}（会话 ${x.会话编号}）`}
                </td>
                <td>{x.世代}</td>
                <td>{x.时间}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h3>封印世代事件</h3>
        <table>
          <thead>
            <tr>
              <th>序号</th>
              <th>世代</th>
              <th>动作</th>
              <th>未结快照</th>
              <th>时间</th>
              <th>备注</th>
            </tr>
          </thead>
          <tbody>
            {hist.map((e) => (
              <tr key={e.序号}>
                <td>{e.序号}</td>
                <td>{e.世代}</td>
                <td>{e.动作}</td>
                <td>{e.未结快照}</td>
                <td>{e.时间}</td>
                <td>{e.备注}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h3>开充回执</h3>
        <table>
          <thead>
            <tr>
              <th>序号</th>
              <th>回执号</th>
              <th>会话号</th>
              <th>会话编号</th>
              <th>封印世代</th>
              <th>时间</th>
            </tr>
          </thead>
          <tbody>
            {receipts.length === 0 && (
              <tr>
                <td colSpan={6}>尚无开充回执</td>
              </tr>
            )}
            {receipts.map((r) => (
              <tr key={r.序号}>
                <td>{r.序号}</td>
                <td>{r.回执号}</td>
                <td>{r.会话号}</td>
                <td>{r.会话编号}</td>
                <td>{r.封印世代}</td>
                <td>{r.时间}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
