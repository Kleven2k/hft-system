import { useEffect, useRef } from 'react'
import { createChart, LineSeries, type UTCTimestamp } from 'lightweight-charts'

interface EquityPoint {
  fill_number: number
  pnl: number
}

interface EquityChartProps {
  data: EquityPoint[]
}

function EquityChart({ data }: EquityChartProps) {
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!containerRef.current || data.length === 0) return

    const style = getComputedStyle(document.documentElement)
    const textColor = style.getPropertyValue('--text-h').trim() || '#333'
    const bgColor = style.getPropertyValue('--bg').trim() || '#fff'
    const borderColor = style.getPropertyValue('--border').trim() || '#e5e4e7'
    const accentColor = style.getPropertyValue('--accent').trim() || '#2563eb'

    const chart = createChart(containerRef.current, {
      width: containerRef.current.clientWidth,
      height: 200,
      layout: { textColor, background: { color: bgColor } },
      grid: {
        vertLines: { color: borderColor },
        horzLines: { color: borderColor },
      },
      // fill_number (1, 2, 3...) is not a real calendar time — print it as a plain
      // number on the axis instead of lightweight-charts' default date formatting.
      timeScale: {
        tickMarkFormatter: (time: number) => String(time),
      },
      localization: {
        timeFormatter: (time: number) => `fill #${time}`,
      },
    })

    const series = chart.addSeries(LineSeries, { color: accentColor })
    series.setData(data.map((p) => ({ time: p.fill_number as UTCTimestamp, value: p.pnl })))
    chart.timeScale().fitContent()

    return () => chart.remove()
  }, [data])

  return <div ref={containerRef} />
}

export default EquityChart
