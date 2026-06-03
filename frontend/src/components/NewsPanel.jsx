const BADGE = {
  positive: 'bg-emerald-950 text-emerald-400',
  negative: 'bg-red-950 text-red-400',
  neutral: 'bg-slate-800 text-slate-400',
}

export default function NewsPanel({ news }) {
  const sorted = [...news.articles].sort((a, b) => b.date.localeCompare(a.date))
  const score = a => a.sentiment * a.relevance

  return (
    <div className="bg-slate-900 rounded-xl p-5">
      <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-4">
        Notizie analizzate ({news.articles.length})
      </h2>
      <div className="space-y-1">
        {sorted.map((item, i) => (
          <div key={i} className="flex items-start gap-3 rounded-lg px-3 py-2 hover:bg-slate-800 transition-colors">
            <span className="text-xs text-slate-500 font-mono mt-0.5 shrink-0 w-[4.5rem]">
              {item.date.slice(5)}
            </span>
            <span className="flex-1 text-sm text-slate-200 leading-snug">
              {item.headline}
            </span>
            <div className="shrink-0 flex items-center gap-2 mt-0.5">
              <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${BADGE[item.label] ?? BADGE.neutral}`}>
                {item.label}
              </span>
              <span className={`text-xs font-mono font-semibold w-14 text-right ${score(item) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                {score(item) >= 0 ? '+' : ''}{score(item).toFixed(3)}
              </span>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
