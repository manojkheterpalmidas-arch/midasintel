import { useState, useCallback, useRef } from 'react'

function websocketUrl(apiBase) {
  const base = apiBase.replace(/\/$/, '')
  if (base.startsWith('https://')) return base.replace('https://', 'wss://')
  if (base.startsWith('http://')) return base.replace('http://', 'ws://')
  return base
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

      ws.onerror = () => {
        setStage('error')
        setError('Analysis connection failed. Please try again.')
        finish(null)
      }

      ws.onclose = () => {
        if (!finished) {
          setStage('error')
          setError('Analysis connection closed before completion. Please try again.')
          finish(null)
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
