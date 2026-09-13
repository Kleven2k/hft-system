import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import './OverviewPage.css'

interface BacktestResult {
  symbol: string
  total_pnl: number
  fill_rate: number
  sharpe: number
}

interface StatusResponse {
  data_dir_reachable: boolean
  symbol_files_found: number
  most_recent_file: string | null
  most_recent_age_secs: number | null
}

const SYMBOLS = ['avaxusdt', 'linkusdt', 'aaveusdt', 'injusdt']
const API = 'http://127.0.0.1:8080'

function formatAge(secs: number): string {
  if (secs < 60) return `${secs}s ago`
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`
  return `${Math.floor(secs / 86400)}d ago`
}

function OverviewPage() {
  const [status, setStatus] = useState<StatusResponse | null>(null)
  const [results, setResults] = useState<BacktestResult[]>([])

  useEffect(() => {
    fetch(`${API}/api/status`)
      .then((res) => (res.ok ? res.json() : null))
      .then(setStatus)
      .catch(() => setStatus(null))

    Promise.all(
      SYMBOLS.map((symbol) =>
        fetch(`${API}/api/backtest?symbol=${symbol}`)
          .then((res) => (res.ok ? res.json() : null))
          .catch(() => null)
      )
    ).then((data) => setResults(data.filter((r): r is BacktestResult => r !== null)))
  }, [])

  const statusLevel = !status
    ? 'down'
    : status.most_recent_age_secs !== null && status.most_recent_age_secs > 24 * 3600
      ? 'stale'
      : 'ok'

  const statusText = !status
    ? 'API unreachable'
    : `Data dir: ${status.symbol_files_found} files` +
      (status.most_recent_file
        ? ` — latest: ${status.most_recent_file} (${formatAge(status.most_recent_age_secs ?? 0)})`
        : ' — no data found')

  return (
    <div>
      <h1>HFT Dashboard</h1>

      <div className="status-banner">
        <span className={`status-dot ${statusLevel}`} />
        <span>{statusText}</span>
      </div>

      <h2>Latest backtest summary</h2>
      <div className="summary-grid">
        {results.map((r) => (
          <div className="summary-card" key={r.symbol}>
            <div className="symbol">{r.symbol}</div>
            <div className="pnl" style={{ color: r.total_pnl >= 0 ? '#22c55e' : '#ef4444' }}>
              ${r.total_pnl.toFixed(2)}
            </div>
            <div className="meta">
              {(r.fill_rate * 100).toFixed(0)}% fill rate &middot; Sharpe {r.sharpe.toFixed(2)}
            </div>
          </div>
        ))}
      </div>

      <h2>Explore</h2>
      <div className="nav-grid">
        <Link className="nav-card" to="/backtests">
          Backtests
          <span className="desc">Strategy performance & equity curves</span>
        </Link>
        <Link className="nav-card" to="/live">
          Live Trading
          <span className="desc">FPGA telemetry, P&amp;L, positions</span>
        </Link>
        <Link className="nav-card" to="/market">
          Market Explorer
          <span className="desc">Price charts for any symbol</span>
        </Link>
        <Link className="nav-card" to="/ticks">
          Tick Data
          <span className="desc">Browse raw collected ticks</span>
        </Link>
      </div>
    </div>
  )
}

export default OverviewPage
