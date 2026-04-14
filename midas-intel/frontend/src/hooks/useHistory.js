import { useState, useEffect, useCallback } from 'react'

export function useHistory(apiBase) {
  const [history, setHistory] = useState([])

  const fetchHistory = useCallback(async (search = '') => {
    try {
      const params = search ? `?search=${encodeURIComponent(search)}` : ''
      const res = await fetch(`${apiBase}/api/history${params}`, { cache: 'no-store' })
      const data = await res.json()
      setHistory(data.history || [])
    } catch (e) {
      console.error('Failed to load history:', e)
    }
  }, [apiBase])

  useEffect(() => {
    fetchHistory()
  }, [fetchHistory])

  const refreshHistory = useCallback(() => fetchHistory(), [fetchHistory])

  const searchHistory = useCallback((q) => fetchHistory(q), [fetchHistory])

  const deleteFromHistory = useCallback(async (domain) => {
    try {
      await fetch(`${apiBase}/api/history/${encodeURIComponent(domain)}`, { method: 'DELETE', cache: 'no-store' })
      setHistory(prev => prev.filter(h => h.domain !== domain))
    } catch (e) {
      console.error('Failed to delete:', e)
    }
  }, [apiBase])

  return { history, refreshHistory, searchHistory, deleteFromHistory }
}
