import { useState, useEffect, useRef } from 'react'

const STAGE_LABELS = {
  connecting: 'Connecting...',
  crawling: 'Crawling website',
  analysing: 'AI analysis',
  enriching: 'Enriching data',
  strategy: 'Sales strategy',
  saving: 'Saving report',
  complete: 'Complete',
  error: 'Error',
}

const SCORE_CONFIG = {
  Hot:  { emoji: '🔥', cls: 'score-hot' },
  Warm: { emoji: '⚡', cls: 'score-warm' },
  Cold: { emoji: '❄️', cls: 'score-cold' },
}

function extractDomain(url) {
  try {
    const u = new URL(url.startsWith('http') ? url : `https://${url}`)
    return u.hostname.replace('www.', '')
  } catch {
    return ''
  }
}

export function SearchBar({ onAnalyse, analysing, progress, progressMessage, stage, error, apiBase, activeReport, onLoadExisting }) {
  const [url, setUrl] = useState('')
  const [existing, setExisting] = useState(null)
  const [checking, setChecking] = useState(false)
  const debounceRef = useRef(null)

  // Populate URL from active report when one is loaded from sidebar
  useEffect(() => {
    if (activeReport?.domain) {
      setUrl(`https://${activeReport.domain}`)
      setExisting(null) // Don't show the warning for the currently loaded report
    }
  }, [activeReport?.domain])

  // Check if URL is already in history (debounced)
  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current)
    setExisting(null)

    const domain = extractDomain(url)
    if (!domain || domain === activeReport?.domain) return

    debounceRef.current = setTimeout(async () => {
      setChecking(true)
      try {
        const res = await fetch(`${apiBase}/api/history/${encodeURIComponent(domain)}`)
        if (res.ok) {
          const data = await res.json()
          setExisting(data)
        } else {
          setExisting(null)
        }
      } catch {
        setExisting(null)
      }
      setChecking(false)
    }, 400)

    return () => { if (debounceRef.current) clearTimeout(debounceRef.current) }
  }, [url, apiBase, activeReport?.domain])

  const handleSubmit = (e) => {
    e.preventDefault()
    if (!url.trim() || analysing) return
    setExisting(null)
    onAnalyse(url.trim())
  }

  const sc = existing ? (SCORE_CONFIG[existing.score] || SCORE_CONFIG.Cold) : null

  return (
    <div className="search-section">
      <form onSubmit={handleSubmit} className="search-form">
        <input
          type="text"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://target-engineering-company.com"
          className="search-input"
          disabled={analysing}
        />
        <button type="submit" className="search-btn" disabled={analysing || !url.trim()}>
          {analysing ? 'Analysing...' : 'Analyse →'}
        </button>
      </form>

      {/* Already analysed warning */}
      {existing && !analysing && (
        <div className="already-researched">
          <div className="already-researched-header">
            <span className="already-researched-icon">⚠</span>
            <span className="already-researched-title">Already researched</span>
          </div>
          <div className="already-researched-body">
            <strong>{existing.company || existing.domain}</strong> was last analysed{' '}
            <strong>{existing.days_ago || 'recently'}</strong> — scored as{' '}
            <span className={`score-badge ${sc.cls}`}>{sc.emoji} {existing.score}</span>
          </div>
          <div className="already-researched-actions">
            <button
              className="action-btn secondary"
              onClick={() => {
                if (onLoadExisting) onLoadExisting(existing)
                setExisting(null)
              }}
            >
              📂 View saved report
            </button>
            <button className="action-btn" onClick={handleSubmit}>
              🔄 Re-crawl fresh
            </button>
          </div>
        </div>
      )}

      {/* Progress bar during analysis */}
      {analysing && (
        <div className="progress-section">
          <div className="progress-bar-track">
            <div className="progress-bar-fill" style={{ width: `${progress}%` }} />
          </div>
          <div className="progress-info">
            <span className="progress-stage">{STAGE_LABELS[stage] || stage}</span>
            <span className="progress-message">{progressMessage}</span>
            <span className="progress-pct">{Math.round(progress)}%</span>
          </div>
        </div>
      )}

      {error && (
        <div className="error-banner">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <circle cx="12" cy="12" r="10" />
            <path d="M12 8v4M12 16h.01" />
          </svg>
          {error}
        </div>
      )}
    </div>
  )
}
