import {
  BarChart, Bar, Cell, XAxis, YAxis, CartesianGrid,
  Tooltip, ReferenceLine, ResponsiveContainer,
} from 'recharts'

function LagTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null
  const { correlation, pvalue, significant, n } = payload[0].payload
  return (
    <div className="bg-slate-800 border border-slate-700 rounded-xl px-4 py-3 text-xs shadow-xl">
      <p className="font-semibold text-slate-200 mb-1">Lag {label > 0 ? `+${label}` : label}</p>
      <p className="text-slate-300">Pearson r: <span className="font-mono">{correlation >= 0 ? '+' : ''}{correlation.toFixed(4)}</span></p>
      <p className="text-slate-300">p-value: <span className="font-mono">{pvalue.toFixed(4)}</span></p>
      <p className="text-slate-300">n: {n}</p>
      {significant && <p className="text-emerald-400 mt-1 font-medium">✓ Significativo (p &lt; 0.05)</p>}
    </div>
  )
}

export default function CorrelationChart({ correlations }) {
  const data = correlations.correlations.map(r => ({
    ...r,
    label: r.lag > 0 ? `+${r.lag}` : String(r.lag),
  }))

  const best = [...data].sort((a, b) => Math.abs(b.correlation) - Math.abs(a.correlation))[0]

  return (
    <div className="bg-slate-900 rounded-xl p-5">
      <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-1">
        Correlazione Pearson per lag
      </h2>
      <p className="text-xs text-slate-600 mb-4">
        lag &gt; 0 = sentiment anticipa il mercato &nbsp;·&nbsp; lag &lt; 0 = mercato anticipa il sentiment
      </p>

      <ResponsiveContainer width="100%" height={220}>
        <BarChart data={data} margin={{ top: 4, right: 16, left: 0, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
          <XAxis
            dataKey="label"
            tick={{ fill: '#64748b', fontSize: 12 }}
            tickLine={false}
            axisLine={{ stroke: '#1e293b' }}
          />
          <YAxis
            domain={[-0.5, 0.5]}
            tick={{ fill: '#64748b', fontSize: 11 }}
            tickLine={false}
            axisLine={false}
            tickFormatter={v => v.toFixed(1)}
          />
          <Tooltip content={<LagTooltip />} />
          <ReferenceLine y={0} stroke="#334155" />
          <Bar dataKey="correlation" maxBarSize={32} radius={[3, 3, 0, 0]}>
            {data.map((entry, i) => (
              <Cell
                key={i}
                fill={entry.significant
                  ? (entry.correlation >= 0 ? '#34d399' : '#f87171')
                  : '#334155'}
                fillOpacity={entry.significant ? 1 : 0.6}
              />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>

      <p className="text-xs text-slate-500 mt-3">
        Lag dominante: <span className="text-slate-300 font-mono">
          {best.lag > 0 ? `+${best.lag}` : best.lag}
        </span> &nbsp;(r = {best.correlation >= 0 ? '+' : ''}{best.correlation.toFixed(4)},
        p = {best.pvalue.toFixed(4)})
        &nbsp;·&nbsp;
        {best.lag === 0 && 'Correlazione contemporanea'}
        {best.lag > 0 && 'Il sentiment tende ad anticipare il mercato'}
        {best.lag < 0 && 'Il mercato tende ad anticipare il sentiment'}
      </p>
    </div>
  )
}
