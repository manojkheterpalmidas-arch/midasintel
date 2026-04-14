import { useState, useCallback, useRef } from 'react'

export function useAnalysis(apiBase) {
  const [analysing, setAnalysing] = useState(false)
  const [progress, setProgress] = useState(0)
  const [progressMessage, setProgressMessage] = useState('')
  const [stage, setStage] = useState('')
  const [error, setError] = useState(null)
  const pollingRef = useRef(false)
  const notFoundCount = useRef(0)

  const startAnalysis = useCallback(async (url) => {
    setAnalysing(true)
    setProgress(0)
    setProgressMessage('Starting...')
    setStage('starting')
    setError(null)
    pollingRef.current = true
    notFoundCount.current = 0

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

      // Poll for progress until complete
      while (pollingRef.current) {
        await new Promise(r => setTimeout(r, 1500))
        if (!pollingRef.current) break

        try {
          const pollRes = await fetch(`${apiBase}/api/jobs/${encodeURIComponent(domain)}`)
          const job = await pollRes.json()

          if (job.status === 'running') {
            notFoundCount.current = 0
            setProgress(job.progress || 0)
            setProgressMessage(job.message || '')
            setStage(job.stage || 'working')

          } else if (job.status === 'complete') {
            setProgress(100)
            setProgressMessage('Complete!')
            setStage('complete')
            pollingRef.current = false

            // Use the job result DIRECTLY — it's always the fresh result
            if (job.result) {
              setAnalysing(false)
              return job.result
            }

            // Fallback: job says complete but no result attached (shouldn't happen)
            // Wait a moment then check history
            await new Promise(r => setTimeout(r, 1000))
            const reportRes = await fetch(`${apiBase}/api/history/${encodeURIComponent(domain)}`)
            if (reportRes.ok) {
              const report = await reportRes.json()
              setAnalysing(false)
              return report
            }
            setAnalysing(false)
            setError('Analysis completed but report not found')
            return null

          } else if (job.status === 'error') {
            setAnalysing(false)
            setError(job.error || job.message || 'Analysis failed')
            setStage('error')
            pollingRef.current = false
            return null

          } else if (job.status === 'not_found') {
            // Job not in tracker — could be server restart or hasn't registered yet
            notFoundCount.current += 1

            // Give it a few tries (job might not have registered yet)
            if (notFoundCount.current <= 5) {
              setProgressMessage('Waiting for analysis to start...')
              continue
            }

            // After 5 "not found" polls, check if result landed in history
            const reportRes = await fetch(`${apiBase}/api/history/${encodeURIComponent(domain)}`)
            if (reportRes.ok) {
              const report = await reportRes.json()
              // Only accept it if it was analysed recently (within last 2 minutes)
              const reportDate = report.date || ''
              if (reportDate) {
                setAnalysing(false)
                setProgress(100)
                setStage('complete')
                pollingRef.current = false
                return report
              }
            }
            // Still nothing — give up
            if (notFoundCount.current > 10) {
              setAnalysing(false)
              setError('Analysis job lost. Please try again.')
              pollingRef.current = false
              return null
            }
          }
        } catch (pollErr) {
          // Network error during poll — keep trying
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

  const cancelAnalysis = useCallback(() => {
    pollingRef.current = false
    setAnalysing(false)
    setStage('')
    setProgressMessage('')
  }, [])

  return { analysing, progress, progressMessage, stage, startAnalysis, cancelAnalysis, error }
}
