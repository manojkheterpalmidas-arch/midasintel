import { useState, useCallback, useRef } from 'react'

const STAGE_LABELS = {
  starting: 'Starting...',
  crawling: 'Crawling website',
  analysing: 'AI analysis',
  enriching: 'Enriching data',
  strategy: 'Sales strategy',
  saving: 'Saving report',
  complete: 'Complete',
  error: 'Error',
}

export function useAnalysis(apiBase) {
  const [analysing, setAnalysing] = useState(false)
  const [progress, setProgress] = useState(0)
  const [progressMessage, setProgressMessage] = useState('')
  const [stage, setStage] = useState('')
  const [error, setError] = useState(null)
  const pollingRef = useRef(false)

  const startAnalysis = useCallback(async (url) => {
    setAnalysing(true)
    setProgress(0)
    setProgressMessage('Starting...')
    setStage('starting')
    setError(null)
    pollingRef.current = true

    try {
      // Start the analysis job via REST
      const res = await fetch(`${apiBase}/api/analyse`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url })
      })
      const data = await res.json()

      if (data.status === 'error') {
        setAnalysing(false)
        setError(data.message || 'Failed to start analysis')
        setStage('error')
        pollingRef.current = false
        return null
      }

      const domain = data.domain

      // Poll for progress until complete or error
      while (pollingRef.current) {
        await new Promise(r => setTimeout(r, 1500))

        if (!pollingRef.current) break

        try {
          const pollRes = await fetch(`${apiBase}/api/jobs/${encodeURIComponent(domain)}`)
          const job = await pollRes.json()

          if (job.status === 'running') {
            setProgress(job.progress || 0)
            setProgressMessage(job.message || '')
            setStage(job.stage || 'working')
          } else if (job.status === 'complete') {
            setProgress(100)
            setProgressMessage('Complete!')
            setStage('complete')
            pollingRef.current = false

            // Fetch the full report from history
            const reportRes = await fetch(`${apiBase}/api/history/${encodeURIComponent(domain)}`)
            if (reportRes.ok) {
              const report = await reportRes.json()
              setAnalysing(false)
              return report
            } else {
              // Job says complete but report not in history yet — use job result
              if (job.result) {
                setAnalysing(false)
                return job.result
              }
              setAnalysing(false)
              setError('Analysis completed but report not found')
              return null
            }
          } else if (job.status === 'error') {
            setAnalysing(false)
            setError(job.error || job.message || 'Analysis failed')
            setStage('error')
            pollingRef.current = false
            return null
          } else if (job.status === 'not_found') {
            // Job disappeared — maybe server restarted, check history
            const reportRes = await fetch(`${apiBase}/api/history/${encodeURIComponent(domain)}`)
            if (reportRes.ok) {
              const report = await reportRes.json()
              setAnalysing(false)
              setProgress(100)
              setStage('complete')
              pollingRef.current = false
              return report
            }
            // Still not found — keep polling a bit more, server might be slow
          }
        } catch (pollErr) {
          // Network error during poll — keep trying (user might have gone offline briefly)
          setProgressMessage('Reconnecting...')
        }
      }

      setAnalysing(false)
      return null

    } catch (err) {
      setAnalysing(false)
      setError('Failed to connect to backend')
      setStage('error')
      pollingRef.current = false
      return null
    }
  }, [apiBase])

  // Allow external cancellation
  const cancelAnalysis = useCallback(() => {
    pollingRef.current = false
    setAnalysing(false)
    setStage('')
    setProgressMessage('')
  }, [])

  return { analysing, progress, progressMessage, stage, startAnalysis, cancelAnalysis, error }
}
