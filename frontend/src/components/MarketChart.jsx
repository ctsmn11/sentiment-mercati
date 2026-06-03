import { useMemo } from 'react'
import {
  ComposedChart, Line, Bar, Cell,
  XAxis, YAxis, CartesianGrid, Tooltip, Legend,
  ResponsiveContainer, ReferenceLine,
} from 'recharts'

function NewsTooltip({ active, payload, label, newsByDate }) {
  if (!active || !payload?.length) return null
  const close = payload.find(p => p.dataKey === 'close')?.value
  const sentiment = payload.find(p => p.dataKey === 'sentiment')?.value
  const headlines = newsByDate[label] ?? []

  return (
    <div className="bg-slate-800 border border-slate-700 rounded-xl px-4 py-3 text-xs max-w-sm shadow-xl">
      <p className="font-semibold text-slate-200 mb-2">{label}</p>
      {close != null && (
        <p className="text-blue-400 mb-1">Prezzo: <span className="font-mono">{close.toFixed(2)}</span></p>
      )}
      {sentiment != null && (
        <p className={`mb-2 ${sentiment >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
          Sentiment: <span className="font-mono">{sentiment >= 0 ? '+' : ''}{sentiment.toFixed(3)}</span>
        </p>
      )}
      {headlines.length > 0 && (
        <div className="border-t border-slate-700 pt-2 mt-1 space-y-1.5">
          {headlines.map((h, i) => (
            <p key={i} className={`leading-snug ${h.sentiment >= 0 ? 'text-emerald-300' : 'text-red-300'}`}>
              · {h.headline}
            </p>
          ))}
        </div>
      )}
    </div>
  )
}

export default function MarketChart({ news, prices }) {
  const { chartData, newsByDate } = useMemo(() => {
    const byDate = {}
    news.articles.forEach(a => {
      const key = a.date.slice(5).replace('-', '/')
      if (!byDate[key]) byDate[key] = []
      byDate[key].push({ headline: a.headline, sentiment: a.sentiment * a.relevance })
    })

    const scoreByDate = {}
    news.articles.forEach(a => {
      const key = a.date.slice(5).replace('-', '/')
      if (!scoreByDate[key]) scoreByDate[key] = []
      scoreByDate[key].push(a.sentiment * a.relevance)
    })

    const data = prices.prices.map(({ date, close }) => {
      const key = date.slice(5).replace('-', '/')
      const scores = scoreByDate[key]
      const sentiment = scores ? scores.reduce((a, b) => a + b, 0) / scores.length : null
      return { date: key, close, sentiment }
    })

    return { chartData: data, newsByDate: byDate }
  }, [news, prices])

  return (
    <div className="bg-slate-900 rounded-xl p-5">
      <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-4">
        Prezzo vs Sentiment — {news.ticker} &nbsp;
        <span className="text-slate-600 font-normal normal-case">
          {news.start} → {news.end}
        </span>
      </h2>
      <ResponsiveContainer width="100%" height={340}>
        <ComposedChart data={chartData} margin={{ top: 4, right: 16, left: 0, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
          <XAxis
            dataKey="date"
            tick={{ fill: '#64748b', fontSize: 11 }}
            tickLine={false}
            axisLine={{ stroke: '#1e293b' }}
            interval={Math.floor(chartData.length / 8)}
          />
          <YAxis
            yAxisId="price"
            domain={['auto', 'auto']}
            tick={{ fill: '#64748b', fontSize: 11 }}
            tickLine={false}
            axisLine={false}
            width={60}
            tickFormatter={v => v.toFixed(0)}
          />
          <YAxis
            yAxisId="sentiment"
            orientation="right"
            domain={[-1, 1]}
            tick={{ fill: '#64748b', fontSize: 11 }}
            tickLine={false}
            axisLine={false}
            width={40}
            tickFormatter={v => v.toFixed(1)}
          />
          <Tooltip content={<NewsTooltip newsByDate={newsByDate} />} />
          <Legend
            formatter={name => name === 'close' ? 'Prezzo' : 'Sentiment'}
            wrapperStyle={{ fontSize: 12, color: '#94a3b8' }}
          />
          <ReferenceLine yAxisId="sentiment" y={0} stroke="#334155" strokeDasharray="4 2" />
          <Bar yAxisId="sentiment" dataKey="sentiment" maxBarSize={8} name="sentiment">
            {chartData.map((entry, i) => (
              <Cell
                key={i}
                fill={(entry.sentiment ?? 0) >= 0 ? '#34d399' : '#f87171'}
                fillOpacity={0.75}
              />
            ))}
          </Bar>
          <Line
            yAxisId="price"
            type="monotone"
            dataKey="close"
            stroke="#60a5fa"
            dot={false}
            strokeWidth={2}
            name="close"
          />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  )
}
