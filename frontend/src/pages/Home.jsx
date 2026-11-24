import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Home() {
  const [o, setO] = useState(null)
  const [err, setErr] = useState('')
  const load = () =>
    api
      .总览()
      .then(setO)
      .catch((e) => setErr(e.message))
  useEffect(() => {
    load()
  }, [])
  if (!o) return <div className="page"><p className="msg">{err || '加载中'}</p></div>
  // 总览只认后端同一快照口径，不接受任何旁路徽标/占用覆盖。
  const items = [
    ['桩位空闲', o.桩位空闲],
    ['桩位占用', o.桩位占用],
    ['桩位故障', o.桩位故障],
    ['桩位维护', o.桩位维护],
    ['充电中', o.充电中],
    ['结算中', o.结算中],
    ['已关账', o.已关账],
    ['已超时', o.已超时],
    ['已中断', o.已中断],
    ['在充电量', o.在充电量],
    ['本日关账金额', o.本日关账金额],
    ['封印世代', o.封印世代],
  ]
  return (
    <div className="page">
      <h2>总览</h2>
      <p className="lead">
        营业日 {o.营业日} · 锁账 <span className={`tag ${o.充日已封印 ? '已中断' : '可用'}`}>{o.锁账徽标}</span>
        {' '}· 封印世代 {o.封印世代} · 账本序号 {o.锁账事件序号}
      </p>
      <div className="grid">
        {items.map(([k, v]) => (
          <div className="stat" key={k}>
            <div className="k">{k}</div>
            <div className="v">{v}</div>
          </div>
        ))}
      </div>
      <button type="button" onClick={load}>刷新</button>
      <p className="msg">{err}</p>
    </div>
  )
}
