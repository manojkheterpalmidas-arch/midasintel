import { useState, useRef, useCallback } from 'react'

const SCORE_EMOJI = { Hot: '🔥', Warm: '⚡', Cold: '❄️' }

export function BatchMode({ apiBase, onComplete }) {
  const [urls, setUrls] = useState('')
  const [recrawl, setRecrawl] = useState(false)
  const [running, setRunning] = useState(false)
  const runningRef = useRef(false)
  const [progress, setProgress] = useState(0)
  const [currentDomain, setCurrentDomain] = useState('')
  const [currentMessage, setCurrentMessage] = useState('')
  const [results, setResults] = useState([])
  const [summary, setSummary] = useState(null)
  const wsRef = useRef(null)
  const allUrlsRef = useRef([])
  const completedDomainsRef = useRef(new Set())
  const stoppedRef = useRef(false)
  const resumingRef = useRef(false)  // Prevent double auto-resume from onerror + onclose

  const getRemainingUrls = useCallback(() => {
    return allUrlsRef.current.filter(u => {
      try {
        const d = new URL(u.startsWith('http') ? u : `https://${u}`).hostname.replace('www.', '')
        return !completedDomainsRef.current.has(d)
      } catch {
        return true
      }
    })
  }, [])

  const startBatch = useCallback((urlLines, isResume = false) => {
    if (urlLines.length === 0) return

    setRunning(true)
    runningRef.current = true
    stoppedRef.current = false
    resumingRef.current = false

    if (!isResume) {
      setProgress(0)
      setResults([])
      setSummary(null)
      completedDomainsRef.current = new Set()
      allUrlsRef.current = urlLines
    }

    const wsUrl = apiBase.replace('https://', 'wss://').replace('http://', 'ws://') + '/ws/batch'
    const ws = new WebSocket(wsUrl)
    wsRef.current = ws

    ws.onopen = () => {
      ws.send(JSON.stringify({ urls: urlLines, recrawl }))
    }

    ws.onmessage = (event) => {
      const msg = JSON.parse(event.data)

      if (msg.type === 'heartbeat') return

      if (msg.type === 'batch_start') {
        // total count
      } else if (msg.type === 'batch_progress') {
        setCurrentDomain(msg.domain)
        setCurrentMessage(msg.message)
        setProgress(msg.item_progress || 0)
      } else if (msg.type === 'batch_item') {
        setResults(prev => [...prev, msg])
        setProgress(msg.progress)
        if (msg.domain) {
          completedDomainsRef.current.add(msg.domain)
        }
        if (msg.status === 'done') {
          onComplete()
        }
      } else if (msg.type === 'batch_complete') {
        setSummary(msg)
        setRunning(false)
        runningRef.current = false
        onComplete()
        ws.close()
      } else if (msg.type === 'error') {
        setRunning(false)
        runningRef.current = false
        onComplete()
        ws.close()
      }
    }

    // Auto-resume on unexpected disconnect (but not if user clicked Stop)
    const handleDisconnect = () => {
      if (stoppedRef.current || resumingRef.current) return
      if (!runningRef.current) return

      resumingRef.current = true  // Prevent double-fire from onerror + onclose
      onComplete()

      const remaining = getRemainingUrls()
      if (remaining.length > 0) {
        // Keep running=true so the Stop button stays visible
        setCurrentMessage(`Reconnecting... ${remaining.length} remaining`)
        setTimeout(() => {
          resumingRef.current = false
          startBatch(remaining, true)
        }, 2000)
      } else {
        setRunning(false)
        runningRef.current = false
        setCurrentMessage('')
      }
    }

    ws.onerror = handleDisconnect
    ws.onclose = handleDisconnect
  }, [apiBase, recrawl, onComplete, getRemainingUrls])

  const handleRun = () => {
    const lines = urls.split('\n').map(l => l.trim()).filter(Boolean)
    startBatch(lines)
  }

  const handleStop = () => {
    stoppedRef.current = true
    runningRef.current = false
    setRunning(false)
    setCurrentMessage('Stopped by user')
    if (wsRef.current) {
      try { wsRef.current.close() } catch {}
      wsRef.current = null
    }
    onComplete()
  }

  return (
    <div className="batch-section">
      <div className="batch-info">
        <div className="batch-info-title">Batch analysis</div>
        <p className="text-muted">
          Paste one URL per line. Each company will be crawled, analysed and saved automatically.
          Already-researched companies will be skipped unless you tick Re-crawl all.
        </p>
      </div>

      <textarea
        className="batch-textarea"
        value={urls}
        onChange={(e) => setUrls(e.target.value)}
        placeholder={"https://company-one.com\nhttps://company-two.com\nhttps://company-three.com"}
        rows={8}
        disabled={running}
      />

      <div className="batch-controls">
        <button className="action-btn" onClick={handleRun} disabled={running || !urls.trim()}>
          {running ? 'Running...' : '🚀 Run batch'}
        </button>
        {running && (
          <button className="action-btn stop-btn" onClick={handleStop}>
            ■ Stop
          </button>
        )}
        <label className="batch-checkbox">
          <input type="checkbox" checked={recrawl} onChange={(e) => setRecrawl(e.target.checked)} />
          Re-crawl all
        </label>
      </div>

      {running && (
        <div className="progress-section mt-md">
          <div className="progress-bar-track">
            <div className="progress-bar-fill" style={{ width: `${progress}%` }} />
          </div>
          <div className="progress-info">
            <span className="progress-stage">{currentDomain}</span>
            <span className="progress-message">{currentMessage}</span>
          </div>
        </div>
      )}

      {results.length > 0 && (
        <div className="batch-results mt-md">
          {results.map((r, i) => (
            <div key={i} className={`batch-result-item ${r.status}`}>
              {r.status === 'done' && <span className="batch-icon done">✓</span>}
              {r.status === 'skipped' && <span className="batch-icon skipped">⏭</span>}
              {r.status === 'failed' && <span className="batch-icon failed">✗</span>}
              <span className="batch-company">{r.company}</span>
              {r.score && r.score !== '—' && (
                <span className={`score-badge score-${r.score?.toLowerCase()}`}>
                  {SCORE_EMOJI[r.score] || ''} {r.score}
                </span>
              )}
              {r.error && <span className="text-muted">{r.error}</span>}
            </div>
          ))}
        </div>
      )}

      {summary && (
        <div className="batch-summary mt-md">
          <div className="batch-summary-title">Batch complete</div>
          <div className="batch-summary-stats">
            <div className="batch-stat">
              <span className="batch-stat-num green">{summary.succeeded}</span>
              <span className="batch-stat-label">Analysed</span>
            </div>
            <div className="batch-stat">
              <span className="batch-stat-num gray">{summary.skipped}</span>
              <span className="batch-stat-label">Skipped</span>
            </div>
            <div className="batch-stat">
              <span className="batch-stat-num red">{summary.failed}</span>
              <span className="batch-stat-label">Failed</span>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
