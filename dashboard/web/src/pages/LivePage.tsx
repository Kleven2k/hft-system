import { useEffect, useState } from 'react'
import './LivePage.css'

interface SlotTelemetry {
  position: number
  pnl_usd: number
  rejects: number
  token: number
  max_burst: number
  bid_valid: boolean
  ask_valid: boolean
  stop_loss: boolean
}

interface LatencyStats {
  min_ns: number
  max_ns: number
  last_ns: number
  count: number
}

interface TelemetrySnapshot {
  connected: boolean
  seq: number
  total_orders: number
  orders_per_sec: number
  slots: SlotTelemetry[]
  latency: LatencyStats
  age_secs: number
}

const API = 'http://127.0.0.1:8080'
const SYMBOLS = ['AVAX', 'LINK', 'AAVE', 'INJ']
const POLL_MS = 1000
const STALE_AFTER_SECS = 5

function fmtNs(ns: number): string {
  if (ns >= 1_000_000) return `${(ns / 1_000_000).toFixed(2)} ms`
  if (ns >= 1_000) return `${(ns / 1_000).toFixed(2)} µs`
  return `${ns} ns`
}

function LivePage() {
  const [telem, setTelem] = useState<TelemetrySnapshot | null>(null)
  const [unreachable, setUnreachable] = useState(false)

  useEffect(() => {
    let cancelled = false

    const poll = () => {
      fetch(`${API}/api/telemetry`)
        .then((res) => (res.ok ? res.json() : Promise.reject()))
        .then((data: TelemetrySnapshot) => {
          if (!cancelled) {
            setTelem(data)
            setUnreachable(false)
          }
        })
        .catch(() => {
          if (!cancelled) setUnreachable(true)
        })
    }

    poll()
    const id = setInterval(poll, POLL_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  const isStale = telem ? telem.age_secs > STALE_AFTER_SECS : false
  const statusLevel = unreachable ? 'down' : !telem?.connected ? 'down' : isStale ? 'stale' : 'ok'
  const statusText = unreachable
    ? 'API unreachable'
    : !telem?.connected
      ? 'No FPGA packets received yet — waiting on UDP 42002'
      : isStale
        ? `No packet in ${telem.age_secs.toFixed(0)}s — FPGA running?`
        : `Live — seq ${telem.seq}, ${telem.orders_per_sec.toFixed(1)} ord/s, ${telem.total_orders.toLocaleString()} total orders`

  return (
    <div>
      <h1>Live Trading</h1>

      <div className="status-banner">
        <span className={`status-dot ${statusLevel}`} />
        <span>{statusText}</span>
      </div>

      <div className="slot-grid">
        {SYMBOLS.map((symbol, i) => {
          const slot = telem?.slots[i]
          return (
            <div className="slot-card" key={symbol}>
              <div className="slot-header">
                <span className="slot-symbol">{symbol}</span>
                {slot?.stop_loss && <span className="slot-badge stop-loss">STOP-LOSS</span>}
              </div>

              {!slot ? (
                <div className="slot-empty">No data</div>
              ) : (
                <>
                  <div className="slot-pnl" style={{ color: slot.pnl_usd >= 0 ? '#22c55e' : '#ef4444' }}>
                    ${slot.pnl_usd.toFixed(4)}
                  </div>
                  <div className="slot-row">
                    <span>Position</span>
                    <span>{slot.position > 0 ? `+${slot.position}` : slot.position}</span>
                  </div>
                  <div className="slot-row">
                    <span>Rejects</span>
                    <span>{slot.rejects}</span>
                  </div>
                  <div className="slot-row">
                    <span>Quote</span>
                    <span>
                      <span className={slot.bid_valid ? 'flag-ok' : 'flag-off'}>B</span>{' '}
                      <span className={slot.ask_valid ? 'flag-ok' : 'flag-off'}>A</span>
                    </span>
                  </div>
                  <div className="token-bucket">
                    {Array.from({ length: slot.max_burst }).map((_, j) => (
                      <span key={j} className={`token ${j < slot.token ? 'filled' : ''}`} />
                    ))}
                  </div>
                </>
              )}
            </div>
          )
        })}
      </div>

      <h2>Pipeline Latency (quote → order)</h2>
      <div className="latency-card">
        {telem && telem.latency.count > 0 ? (
          <div className="latency-row">
            <div>
              <span className="latency-label">Min</span>
              <span className="latency-value">{fmtNs(telem.latency.min_ns)}</span>
            </div>
            <div>
              <span className="latency-label">Last</span>
              <span className="latency-value">{fmtNs(telem.latency.last_ns)}</span>
            </div>
            <div>
              <span className="latency-label">Max</span>
              <span className="latency-value">{fmtNs(telem.latency.max_ns)}</span>
            </div>
            <div>
              <span className="latency-label">Samples</span>
              <span className="latency-value">{telem.latency.count.toLocaleString()}</span>
            </div>
          </div>
        ) : (
          <p>No latency measurements yet.</p>
        )}
      </div>
    </div>
  )
}

export default LivePage
