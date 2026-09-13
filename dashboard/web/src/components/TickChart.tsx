import { useEffect, useRef } from 'react'
import { createChart, LineSeries, type UTCTimestamp } from 'lightweight-charts'

interface TickPoint {
  time: number
  bid: number
  ask: number
}

interface TickChartProps {
  data: TickPoint[]
}

function TickChart({ data }: TickChartProps) {
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!containerRef.current || data.length === 0) return

    const style = getComputedStyle(document.documentElement)
    const textColor = style.getPropertyValue('--text-h').trim() || '#333'
    const bgColor = style.getPropertyValue('--bg').trim() || '#fff'
    const borderColor = style.getPropertyValue('--border').trim() || '#e5e4e7'

    const chart = createChart(containerRef.current, {
      width: containerRef.current.clientWidth,
      height: 300,
      layout: { textColor, background: { color: bgColor } },
      grid: {
        vertLines: { color: borderColor },
        horzLines: { color: borderColor },
      },
      timeScale: {
        tickMarkFormatter: (time: number) => String(time),
      },
      localization: {
        timeFormatter: (time: number) => `tick #${time}`,
      },
    })

    const bidSeries = chart.addSeries(LineSeries, { color: '#22c55e', title: 'bid' })
    bidSeries.setData(data.map((p) => ({ time: p.time as UTCTimestamp, value: p.bid })))

    const askSeries = chart.addSeries(LineSeries, { color: '#ef4444', title: 'ask' })
    askSeries.setData(data.map((p) => ({ time: p.time as UTCTimestamp, value: p.ask })))

    chart.timeScale().fitContent()

    return () => chart.remove()
  }, [data])

  return <div ref={containerRef} />
}

export default TickChart
