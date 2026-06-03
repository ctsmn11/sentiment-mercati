import { useState, useEffect } from 'react'

export function useDashboardData() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    Promise.all([
      fetch('/data/news_classified.json').then(r => r.json()),
      fetch('/data/prices.json').then(r => r.json()),
      fetch('/data/correlations.json').then(r => r.json()),
    ])
      .then(([news, prices, correlations]) => {
        setData({ news, prices, correlations })
        setLoading(false)
      })
      .catch(e => {
        setError('File dati non trovati. Esegui il pipeline e poi: npm run sync-data')
        setLoading(false)
      })
  }, [])

  return { data, loading, error }
}
