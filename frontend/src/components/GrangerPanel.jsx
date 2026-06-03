const DIRECTIONS = [
  {
    key: 'sentiment_causes_returns',
    label: 'Notizie → Mercato',
    question: 'Le notizie anticipano i movimenti di prezzo?',
    positiveMsg: 'Sì — il sentiment Granger-causa i rendimenti',
    negativeMsg: 'No — le notizie non anticipano il mercato',
  },
  {
    key: 'returns_cause_sentiment',
    label: 'Mercato → Notizie',
    question: 'I movimenti di prezzo anticipano il tono delle notizie?',
    positiveMsg: 'Sì — i rendimenti Granger-causano il sentiment',
    negativeMsg: 'No — il mercato non anticipa le notizie',
  },
]

function DirectionTable({ rows, label, question, positiveMsg, negativeMsg }) {
  const anySig = rows.some(r => r.significant)
  return (
    <div className="flex-1 min-w-0">
      <div className="flex items-center gap-2 mb-1">
        <span className="text-sm font-semibold text-slate-200">{label}</span>
        <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${
          anySig ? 'bg-emerald-950 text-emerald-400' : 'bg-slate-800 text-slate-500'
        }`}>
          {anySig ? positiveMsg : negativeMsg}
        </span>
      </div>
      <p className="text-xs text-slate-600 mb-3">{question}</p>
      <table className="w-full text-xs">
        <thead>
          <tr className="text-slate-500 border-b border-slate-800">
            <th className="text-left py-1 font-medium">Lag</th>
            <th className="text-right py-1 font-medium">F-stat</th>
            <th className="text-right py-1 font-medium">p-value</th>
            <th className="text-right py-1 font-medium"></th>
          </tr>
        </thead>
        <tbody>
          {rows.map(r => (
            <tr key={r.lag} className={`border-b border-slate-800/50 ${r.significant ? 'text-slate-200' : 'text-slate-500'}`}>
              <td className="py-1.5 font-mono">{r.lag}</td>
              <td className="py-1.5 font-mono text-right">{r.f_stat.toFixed(3)}</td>
              <td className={`py-1.5 font-mono text-right ${r.significant ? 'text-emerald-400' : ''}`}>
                {r.pvalue.toFixed(4)}
              </td>
              <td className="py-1.5 text-right">
                {r.significant && <span className="text-emerald-400">✓</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function GrangerPanel({ correlations }) {
  const { granger, ticker, n_days, date_range } = correlations
  const s2r = granger.sentiment_causes_returns
  const r2s = granger.returns_cause_sentiment
  const sCauses = s2r.some(r => r.significant)
  const rCauses = r2s.some(r => r.significant)

  let conclusion, conclusionColor
  if (sCauses && rCauses) {
    conclusion = 'Causalità bidirezionale: notizie e mercato si influenzano a vicenda.'
    conclusionColor = 'text-yellow-400'
  } else if (sCauses) {
    conclusion = 'Le notizie Granger-causano il mercato — il sentiment anticipa i prezzi.'
    conclusionColor = 'text-emerald-400'
  } else if (rCauses) {
    conclusion = 'Il mercato Granger-causa le notizie — i prezzi anticipano il sentiment.'
    conclusionColor = 'text-blue-400'
  } else {
    conclusion = 'Nessuna causalità di Granger significativa rilevata.'
    conclusionColor = 'text-slate-500'
  }

  return (
    <div className="bg-slate-900 rounded-xl p-5">
      <div className="flex items-baseline gap-3 mb-1">
        <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider">
          Test di Granger
        </h2>
        <span className="text-xs text-slate-600">
          {ticker} · {n_days} giorni · {date_range.start} → {date_range.end}
        </span>
      </div>

      <p className={`text-sm font-medium mb-5 ${conclusionColor}`}>{conclusion}</p>

      <div className="flex gap-8">
        {DIRECTIONS.map(d => (
          <DirectionTable
            key={d.key}
            rows={granger[d.key]}
            {...d}
          />
        ))}
      </div>
    </div>
  )
}
