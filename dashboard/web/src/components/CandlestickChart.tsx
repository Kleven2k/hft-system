import { useEffect, useRef } from 'react'
import { createChart, CandlestickSeries, type UTCTimestamp } from 'lightweight-charts'

interface Candle {
  time: number
  open: number
  high: number
  low: number
  close: number
}

interface CandlestickChartProps {
  data: Candle[]
}

function CandlestickChart({ data }: CandlestickChartProps) {
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!containerRef.current || data.length === 0) return

    const style = getComputedStyle(document.documentElement)
    const textColor = style.getPropertyValue('--text-h').trim() || '#333'
    const bgColor = style.getPropertyValue('--bg').trim() || '#fff'
    const borderColor = style.getPropertyValue('--border').trim() || '#e5e4e7'

    const chart = createChart(containerRef.current, {
      width: containerRef.current.clientWidth,
      height: 400,
      layout: { textColor, background: { color: bgColor } },
      grid: {
        vertLines: { color: borderColor },
        horzLines: { color: borderColor },
      },
    })

    const series = chart.addSeries(CandlestickSeries, {
      upColor: '#22c55e',
      downColor: '#ef4444',
      borderVisible: false,
      wickUpColor: '#22c55e',
      wickDownColor: '#ef4444',
    })

    series.setData(
      data.map((c) => ({
        time: c.time as UTCTimestamp,
        open: c.open,
        high: c.high,
        low: c.low,
        close: c.close,
      }))
    )
    chart.timeScale().fitContent()

    return () => chart.remove()
  }, [data])

  return <div ref={containerRef} />
}

export default CandlestickChart
