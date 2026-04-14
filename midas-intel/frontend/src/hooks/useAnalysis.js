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
      const jobId = data.job_id

      while (pollingRef.current) {
        await new Promise(r => setTimeout(r, 1500))
        if (!pollingRef.current) break

        try {
          const pollRes = await fetch(`${apiBase}/api/jobs/${encodeURIComponent(domain)}`)
          const job = await pollRes.json()

          if (jobId && job.status !== 'not_found' && job.job_id !== jobId) {
            setProgressMessage('Waiting for fresh re-crawl...')
            continue
          }

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

            if (job.result && (!jobId || job.result.job_id === jobId)) {
              setAnalysing(false)
              return job.result
            }

            setAnalysing(false)
            setError('Analysis completed but fresh report was not returned')
            return null

          } else if (job.status === 'error') {
            setAnalysing(false)
            setError(job.error || job.message || 'Analysis failed')
            setStage('error')
            pollingRef.current = false
            return null

          } else if (job.status === 'not_found') {
            notFoundCount.current += 1

            if (notFoundCount.current <= 10) {
              setProgressMessage('Waiting for fresh re-crawl...')
              continue
            }

            setAnalysing(false)
            setError('Fresh re-crawl job was lost. Please try again.')
            pollingRef.current = false
            return null
          }
        } catch (pollErr) {
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
