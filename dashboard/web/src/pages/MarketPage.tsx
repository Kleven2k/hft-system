import { useEffect, useState } from 'react'
import CandlestickChart from '../components/CandlestickChart'
import './MarketPage.css'

interface Candle {
  time: number
  open: number
  high: number
  low: number
  close: number
}

const INTERVALS = ['15m', '1h', '4h', '1d']
const API = 'http://127.0.0.1:8080'

function MarketPage() {
  const [symbolInput, setSymbolInput] = useState('btcusdt')
  const [symbol, setSymbol] = useState('btcusdt')
  const [interval, setInterval] = useState('1h')
  const [candles, setCandles] = useState<Candle[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setError(null)
    fetch(`${API}/api/market?symbol=${symbol}&interval=${interval}`)
      .then(async (res) => {
        if (!res.ok) throw new Error(await res.text())
        return res.json()
      })
      .then(setCandles)
      .catch((e) => {
        setCandles([])
        setError(String(e.message ?? e))
      })
  }, [symbol, interval])

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    setSymbol(symbolInput.trim().toLowerCase())
  }

  return (
    <div>
      <h1>Market Explorer</h1>

      <form className="market-controls" onSubmit={handleSubmit}>
        <label>
          Symbol
          <input
            value={symbolInput}
            onChange={(e) => setSymbolInput(e.target.value)}
            placeholder="e.g. btcusdt"
          />
        </label>

        <label>
          Interval
          <select value={interval} onChange={(e) => setInterval(e.target.value)}>
            {INTERVALS.map((i) => (
              <option key={i} value={i}>{i}</option>
            ))}
          </select>
        </label>
      </form>

      <div className="market-chart-card">
        {error && <p className="market-error">Failed to load: {error}</p>}
        {!error && candles.length > 0 && <CandlestickChart data={candles} />}
        {!error && candles.length === 0 && <p>Loading {symbol}...</p>}
      </div>
    </div>
  )
}

export default MarketPage
