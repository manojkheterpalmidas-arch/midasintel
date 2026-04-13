import { useState, useEffect } from 'react'
import { Sidebar } from './components/Sidebar'
import { SearchBar } from './components/SearchBar'
import { Report } from './components/Report'
import { BatchMode } from './components/BatchMode'
import { ApiKeyGate } from './components/ApiKeyGate'
import { useHistory } from './hooks/useHistory'
import { useAnalysis } from './hooks/useAnalysis'

const API_BASE = import.meta.env.VITE_API_URL || 'http://localhost:8000'

export default function App() {
  const [firecrawlKey, setFirecrawlKey] = useState(() => localStorage.getItem('firecrawl_key') || '')
  const [mode, setMode] = useState('single')
  const [activeReport, setActiveReport] = useState(null)
  const [sidebarOpen, setSidebarOpen] = useState(true)
  const [credits, setCredits] = useState(null)

  const { history, refreshHistory, searchHistory, deleteFromHistory } = useHistory(API_BASE)
  const { analysing, progress, progressMessage, stage, startAnalysis, error: analysisError } = useAnalysis(API_BASE)

  // Fetch Firecrawl credits
  useEffect(() => {
    if (!firecrawlKey) return
    localStorage.setItem('firecrawl_key', firecrawlKey)

    const fetchCredits = async () => {
      try {
        const res = await fetch(`${API_BASE}/api/credits?firecrawl_key=${encodeURIComponent(firecrawlKey)}`)
        const data = await res.json()
        setCredits(data.credits)
      } catch {
        setCredits(null)
      }
    }
    fetchCredits()
    const interval = setInterval(fetchCredits, 300000)
    return () => clearInterval(interval)
  }, [firecrawlKey])

  const handleAnalyse = async (url) => {
    const result = await startAnalysis(url, firecrawlKey)
    if (result) {
      setActiveReport(result)
      refreshHistory()
      // Refresh credits after analysis
      try {
        const res = await fetch(`${API_BASE}/api/credits?firecrawl_key=${encodeURIComponent(firecrawlKey)}`)
        const data = await res.json()
        setCredits(data.credits)
      } catch {}
    }
  }

  const handleLoadReport = (report) => {
    setActiveReport(report)
  }

  const handleDelete = async (domain) => {
    await deleteFromHistory(domain)
    if (activeReport?.domain === domain) {
      setActiveReport(null)
    }
  }

  const handleSaveKey = (key) => {
    setFirecrawlKey(key)
  }

  if (!firecrawlKey) {
    return <ApiKeyGate onSave={handleSaveKey} />
  }

  return (
    <div className="app-shell">
      <header className="top-bar">
        <div className="top-bar-left">
          <button className="sidebar-toggle" onClick={() => setSidebarOpen(!sidebarOpen)}>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M3 12h18M3 6h18M3 18h18" />
            </svg>
          </button>
          <div className="brand">
            <span className="brand-name">MIDAS</span>
            <span className="brand-badge">PRESALES INTEL</span>
          </div>
        </div>
        <div className="top-bar-right">
          <span className="user-label">Manoj · MIDAS IT</span>
          <span className="credits-label">
            {credits !== null ? `Firecrawl: ${credits} credits` : 'Firecrawl: —'}
          </span>
          <button
            className="key-btn"
            onClick={() => { localStorage.removeItem('firecrawl_key'); setFirecrawlKey(''); setCredits(null) }}
            title="Change Firecrawl key"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4" />
            </svg>
          </button>
        </div>
      </header>

      <div className="app-body">
        <Sidebar
          open={sidebarOpen}
          history={history}
          activeReport={activeReport}
          onLoadReport={handleLoadReport}
          onDelete={handleDelete}
          onSearch={searchHistory}
          apiBase={API_BASE}
        />

        <main className={`main-content ${sidebarOpen ? '' : 'full-width'}`}>
          <div className="input-row">
            <div className="mode-toggle">
              <button className={mode === 'single' ? 'active' : ''} onClick={() => setMode('single')}>Single</button>
              <button className={mode === 'batch' ? 'active' : ''} onClick={() => setMode('batch')}>Batch</button>
            </div>
          </div>

          {mode === 'single' ? (
            <>
              <SearchBar
                onAnalyse={handleAnalyse}
                analysing={analysing}
                progress={progress}
                progressMessage={progressMessage}
                stage={stage}
                error={analysisError}
                apiBase={API_BASE}
                activeReport={activeReport}
                onLoadExisting={handleLoadReport}
              />
              {activeReport && !analysing && (
                <Report
                  report={activeReport}
                  apiBase={API_BASE}
                  firecrawlKey={firecrawlKey}
                  onRefresh={() => handleAnalyse(`https://${activeReport.domain}`)}
                />
              )}
            </>
          ) : (
            <BatchMode
              apiBase={API_BASE}
              firecrawlKey={firecrawlKey}
              onComplete={refreshHistory}
            />
          )}
        </main>
      </div>
    </div>
  )
}
