import { useState, useCallback, useRef } from 'react'

function websocketUrl(apiBase) {
  const base = apiBase.replace(/\/$/, '')
  if (base.startsWith('https://')) return base.replace('https://', 'wss://')
  if (base.startsWith('http://')) return base.replace('http://', 'ws://')
  return base
}

function extractDomain(url) {
  try {
    const parsed = new URL(url.startsWith('http') ? url : `https://${url}`)
    return parsed.hostname.replace('www.', '')
  } catch {
    return ''
  }
}

export function useAnalysis(apiBase) {
  const [analysing, setAnalysing] = useState(false)
  const [progress, setProgress] = useState(0)
  const [progressMessage, setProgressMessage] = useState('')
  const [stage, setStage] = useState('')
  const [error, setError] = useState(null)
  const wsRef = useRef(null)

  const startAnalysis = useCallback(async (url) => {
    if (wsRef.current) {
      wsRef.current.close()
      wsRef.current = null
    }

    setAnalysing(true)
    setProgress(0)
    setProgressMessage('Starting...')
    setStage('starting')
    setError(null)

    return new Promise((resolve) => {
      const ws = new WebSocket(`${websocketUrl(apiBase)}/ws/analyse`)
      wsRef.current = ws
      let finished = false
      let fallbackStarted = false
      const domain = extractDomain(url)

      const waitForSavedReport = async () => {
        if (!domain) return null
        setProgressMessage('Finishing fresh report...')
        for (let i = 0; i < 80; i += 1) {
          try {
            const res = await fetch(`${apiBase}/api/history/${encodeURIComponent(domain)}`, { cache: 'no-store' })
            if (res.ok) {
              return await res.json()
            }
          } catch {}
          await new Promise(r => setTimeout(r, 1500))
        }
        return null
      }

      const finish = (result) => {
        if (finished) return
        finished = true
        wsRef.current = null
        setAnalysing(false)
        resolve(result)
      }

      ws.onopen = () => {
        ws.send(JSON.stringify({ url }))
      }

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data)

          if (msg.type === 'progress') {
            setProgress(msg.progress || 0)
            setProgressMessage(msg.message || '')
            setStage(msg.stage || 'working')
            return
          }

          if (msg.type === 'complete') {
            setProgress(100)
            setProgressMessage('Complete!')
            setStage('complete')
            finish(msg.data || null)
            return
          }

          if (msg.type === 'error') {
            setStage('error')
            setError(msg.message || 'Analysis failed')
            finish(null)
          }
        } catch {
          setProgressMessage('Receiving analysis update...')
        }
      }

      const handleEarlyClose = async () => {
        if (finished || fallbackStarted) return
        fallbackStarted = true
        const report = await waitForSavedReport()
        if (report) {
          setProgress(100)
          setStage('complete')
          setProgressMessage('Complete!')
          finish(report)
          return
        }
        setStage('error')
        setError('Analysis connection closed before completion. Please try again.')
        finish(null)
      }

      ws.onerror = () => {
        handleEarlyClose()
      }

      ws.onclose = () => {
        if (!finished) {
          handleEarlyClose()
        }
      }
    })
  }, [apiBase])

  const cancelAnalysis = useCallback(() => {
    if (wsRef.current) {
      wsRef.current.close()
      wsRef.current = null
    }
    setAnalysing(false)
    setStage('')
    setProgressMessage('')
  }, [])

  return { analysing, progress, progressMessage, stage, startAnalysis, cancelAnalysis, error }
}
