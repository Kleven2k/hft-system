import { useEffect, useState } from 'react'
import EquityChart from '../components/EquityChart'
import './BacktestsPage.css'

interface EquityPoint {
  fill_number: number
  pnl: number
}

interface BacktestResult {
  symbol: string
  n_rows: number
  n_orders: number
  n_fills: number
  n_cancels: number
  total_pnl: number
  fill_rate: number
  avg_edge: number
  sharpe: number
  max_long: number
  max_short: number
  equity_curve: EquityPoint[]
}

const SYMBOLS = ['avaxusdt', 'linkusdt', 'aaveusdt', 'injusdt']

function BacktestsPage() {
  const [results, setResults] = useState<BacktestResult[]>([])

  useEffect(() => {
    Promise.all(
      SYMBOLS.map((symbol) =>
        fetch(`http://127.0.0.1:8080/api/backtest?symbol=${symbol}`)
          .then((res) => (res.ok ? res.json() : null))
      )
    ).then((data) => setResults(data.filter((r): r is BacktestResult => r !== null)))
  }, [])

  return (
    <div>
      <h1>Backtests</h1>
      <table className="results-table">
        <thead>
          <tr>
            <th>Symbol</th>
            <th>Rows</th>
            <th>Orders</th>
            <th>Fills</th>
            <th>Total P&amp;L</th>
            <th>Fill Rate</th>
            <th>Sharpe</th>
          </tr>
        </thead>
        <tbody>
          {results.map((r) => (
            <tr key={r.symbol}>
              <td>{r.symbol}</td>
              <td>{r.n_rows.toLocaleString()}</td>
              <td>{r.n_orders}</td>
              <td>{r.n_fills}</td>
              <td className={r.total_pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}>
                ${r.total_pnl.toFixed(2)}
              </td>
              <td>{(r.fill_rate * 100).toFixed(0)}%</td>
              <td>{r.sharpe.toFixed(2)}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {results.map((r) => (
        <div className="equity-card" key={r.symbol}>
          <h2>{r.symbol} equity curve</h2>
          <EquityChart data={r.equity_curve} />
        </div>
      ))}
    </div>
  )
}

export default BacktestsPage
