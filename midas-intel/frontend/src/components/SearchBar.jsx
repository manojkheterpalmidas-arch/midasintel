import { useState } from 'react'

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

export function SearchBar({ onAnalyse, analysing, progress, progressMessage, stage, error }) {
  const [url, setUrl] = useState('')

  const handleSubmit = (e) => {
    e.preventDefault()
    if (!url.trim() || analysing) return
    onAnalyse(url.trim())
  }

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

      {analysing && (
        <div className="progress-section">
          <div className="progress-bar-track">
            <div
              className="progress-bar-fill"
              style={{ width: `${progress}%` }}
            />
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
