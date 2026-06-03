import { useDashboardData } from './hooks/useDashboardData'
import MarketChart from './components/MarketChart'
import CorrelationChart from './components/CorrelationChart'
import GrangerPanel from './components/GrangerPanel'
import NewsPanel from './components/NewsPanel'

export default function App() {
  const { data, loading, error } = useDashboardData()

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <header className="border-b border-slate-800 px-8 py-5">
        <h1 className="text-lg font-semibold tracking-tight">Sentiment Mercati</h1>
        <p className="text-sm text-slate-500 mt-0.5">
          Le notizie anticipano i crolli, o i crolli generano notizie negative?
        </p>
      </header>

      <main className="px-8 py-6 space-y-5 max-w-6xl mx-auto">
        {loading && (
          <div className="flex items-center gap-3 text-slate-400 py-12 justify-center">
            <div className="w-4 h-4 border-2 border-slate-500 border-t-slate-200 rounded-full animate-spin" />
            <span>Caricamento dati…</span>
          </div>
        )}

        {error && (
          <div className="text-red-300 bg-red-950 border border-red-800 rounded-xl px-5 py-4 text-sm">
            <strong>Errore:</strong> {error}
          </div>
        )}

        {data && (
          <>
            <MarketChart news={data.news} prices={data.prices} />
            <div className="grid grid-cols-2 gap-5">
              <CorrelationChart correlations={data.correlations} />
              <GrangerPanel correlations={data.correlations} />
            </div>
            <NewsPanel news={data.news} />
          </>
        )}
      </main>
    </div>
  )
}
