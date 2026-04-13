import { useState, useCallback, useRef } from 'react'

export function useAnalysis(apiBase) {
  const [analysing, setAnalysing] = useState(false)
  const [progress, setProgress] = useState(0)
  const [progressMessage, setProgressMessage] = useState('')
  const [stage, setStage] = useState('')
  const [error, setError] = useState(null)
  const wsRef = useRef(null)

  const startAnalysis = useCallback((url, firecrawlKey) => {
    return new Promise((resolve) => {
      setAnalysing(true)
      setProgress(0)
      setProgressMessage('Connecting...')
      setStage('connecting')
      setError(null)

      const wsUrl = apiBase.replace('https://', 'wss://').replace('http://', 'ws://') + '/ws/analyse'
      const ws = new WebSocket(wsUrl)
      wsRef.current = ws

      ws.onopen = () => {
        ws.send(JSON.stringify({ url, firecrawl_key: firecrawlKey }))
      }

      ws.onmessage = (event) => {
        const msg = JSON.parse(event.data)

        if (msg.type === 'progress') {
          setProgress(msg.progress)
          setProgressMessage(msg.message)
          setStage(msg.stage)
        } else if (msg.type === 'complete') {
          setAnalysing(false)
          setProgress(100)
          setProgressMessage('Complete!')
          setStage('complete')
          ws.close()
          resolve(msg.data)
        } else if (msg.type === 'error') {
          setAnalysing(false)
          setError(msg.message)
          setStage('error')
          ws.close()
          resolve(null)
        }
      }

      ws.onerror = () => {
        setAnalysing(false)
        setError('WebSocket connection failed. Is the backend running?')
        setStage('error')
        resolve(null)
      }

      ws.onclose = () => {
        if (wsRef.current === ws) {
          wsRef.current = null
        }
      }
    })
  }, [apiBase])

  return { analysing, progress, progressMessage, stage, startAnalysis, error }
}
