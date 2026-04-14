import { useState } from 'react'

const SCORE_CONFIG = {
  Hot:  { emoji: '🔥', cls: 'score-hot' },
  Warm: { emoji: '⚡', cls: 'score-warm' },
  Cold: { emoji: '❄️', cls: 'score-cold' },
}

export function Sidebar({ open, history, activeReport, onLoadReport, onDelete, onSearch, apiBase }) {
  const [search, setSearch] = useState('')
  const [confirmDelete, setConfirmDelete] = useState(null)

  const handleSearch = (e) => {
    setSearch(e.target.value)
    onSearch(e.target.value)
  }

  const handleDelete = (domain) => {
    if (confirmDelete === domain) {
      onDelete(domain)
      setConfirmDelete(null)
    } else {
      setConfirmDelete(domain)
    }
  }

  const handleExportCsv = async () => {
    window.open(`${apiBase}/api/export/csv`, '_blank')
  }

  if (!open) return null

  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        <span className="sec-label">Recent searches</span>
      </div>

      <input
        type="text"
        value={search}
        onChange={handleSearch}
        placeholder="Search companies..."
        className="sidebar-search"
      />

      <div className="sidebar-list">
        {history.length === 0 ? (
          <p className="sidebar-empty">No searches yet</p>
        ) : (
          history.map((h, i) => {
            const sc = SCORE_CONFIG[h.score] || SCORE_CONFIG.Cold
            const isActive = activeReport?.domain === h.domain
            const leadScore = h.lead_score ?? h.sales_data?.lead_score ?? null
            return (
              <div
                key={h.domain}
                className={`sidebar-item ${isActive ? 'active' : ''}`}
              >
                <div className="sidebar-item-info" onClick={() => onLoadReport(h)}>
                  <div className="sidebar-item-name">{h.company || h.domain}</div>
                  <div className="sidebar-item-meta">
                    <span className={`score-badge ${sc.cls}`}>
                      {leadScore !== null && <span className="score-num">{leadScore}</span>}
                      {sc.emoji} {h.score}
                    </span>
                    <span className="sidebar-item-date">{h.days_ago}</span>
                  </div>
                </div>
                <div className="sidebar-item-actions">
                  {confirmDelete === h.domain ? (
                    <button className="sidebar-btn danger" onClick={() => handleDelete(h.domain)} title="Confirm delete">
                      ✓
                    </button>
                  ) : (
                    <button className="sidebar-btn" onClick={() => handleDelete(h.domain)} title="Delete">
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                        <path d="M3 6h18M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2m3 0v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6h14" />
                      </svg>
                    </button>
                  )}
                </div>
              </div>
            )
          })
        )}
      </div>

      <div className="sidebar-footer">
        <span className="sec-label">Bulk export</span>
        {history.length > 0 && (
          <button className="export-btn" onClick={handleExportCsv}>
            Download all as CSV
          </button>
        )}
        {history.length > 0 && (
          <span className="sidebar-count">{history.length} companies</span>
        )}
      </div>
    </aside>
  )
}
