import { useEffect, useState } from 'react'
import TickChart from '../components/TickChart'
import './TickDataPage.css'

interface TickPoint {
  time: number
  bid: number
  ask: number
}

const SYMBOLS = ['avaxusdt', 'linkusdt', 'aaveusdt', 'injusdt']
const API = 'http://127.0.0.1:8080'

function TickDataPage() {
  const [symbol, setSymbol] = useState(SYMBOLS[0])
  const [dates, setDates] = useState<string[]>([])
  const [date, setDate] = useState<string>('')
  const [ticks, setTicks] = useState<TickPoint[]>([])

  // Fetch available dates whenever the symbol changes
  useEffect(() => {
    fetch(`${API}/api/tick-dates?symbol=${symbol}`)
      .then((res) => (res.ok ? res.json() : []))
      .then((d: string[]) => {
        setDates(d)
        setDate(d[0] ?? '')
      })
  }, [symbol])

  // Fetch ticks whenever symbol or date changes
  useEffect(() => {
    if (!date) {
      setTicks([])
      return
    }
    fetch(`${API}/api/ticks?symbol=${symbol}&date=${date}`)
      .then((res) => (res.ok ? res.json() : []))
      .then(setTicks)
  }, [symbol, date])

  return (
    <div>
      <h1>Tick Data</h1>

      <div className="tick-controls">
        <label>
          Symbol
          <select value={symbol} onChange={(e) => setSymbol(e.target.value)}>
            {SYMBOLS.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </label>

        <label>
          Date
          <select value={date} onChange={(e) => setDate(e.target.value)}>
            {dates.map((d) => (
              <option key={d} value={d}>{d}</option>
            ))}
          </select>
        </label>
      </div>

      <div className="tick-chart-card">
        {ticks.length > 0 ? (
          <TickChart data={ticks} />
        ) : (
          <p>No data for {symbol} on {date || '...'}</p>
        )}
      </div>
    </div>
  )
}

export default TickDataPage
