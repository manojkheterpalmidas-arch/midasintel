import { useState, useEffect, Component } from 'react'

// Error boundary to prevent tab crashes from blanking the whole page
class TabErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { hasError: false, error: null }
  }
  static getDerivedStateFromError(error) {
    return { hasError: true, error }
  }
  render() {
    if (this.state.hasError) {
      return (
        <div style={{ padding: '20px', background: '#fef2f2', border: '1px solid #fecaca', borderRadius: '10px', margin: '12px 0' }}>
          <div style={{ fontWeight: 600, fontSize: '14px', color: '#dc2626', marginBottom: '6px' }}>
            This tab encountered an error
          </div>
          <div style={{ fontSize: '13px', color: '#6b7280' }}>
            The data for this company may have an unexpected format. Other tabs should still work.
          </div>
          <pre style={{ fontSize: '11px', color: '#9ca3af', marginTop: '8px', whiteSpace: 'pre-wrap' }}>
            {this.state.error?.message || 'Unknown error'}
          </pre>
        </div>
      )
    }
    return this.props.children
  }
}

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'people', label: 'People' },
  { id: 'projects', label: 'Projects' },
  { id: 'opportunities', label: 'FEM Opps' },
  { id: 'strategy', label: 'Strategy' },
  { id: 'vacancies', label: 'Vacancies' },
  { id: 'email', label: 'Email' },
  { id: 'export', label: 'Export' },
]

const SCORE_CONFIG = {
  Hot:  { emoji: '🔥', cls: 'score-hot' },
  Warm: { emoji: '⚡', cls: 'score-warm' },
  Cold: { emoji: '❄️', cls: 'score-cold' },
}

// Safe render — handles cases where DeepSeek returns objects instead of strings
function safeStr(val) {
  if (val === null || val === undefined) return ''
  if (typeof val === 'string') return val
  if (typeof val === 'object') return JSON.stringify(val)
  return String(val)
}

// Safe array — ensures we always iterate over an array of strings
function safeArr(val) {
  if (!Array.isArray(val)) return []
  return val.map(item => safeStr(item))
}

function ScoreBadge({ score }) {
  const sc = SCORE_CONFIG[score] || SCORE_CONFIG.Cold
  return <span className={`score-badge ${sc.cls}`}>{sc.emoji} {score}</span>
}

function PillTag({ children, variant = 'default' }) {
  return <span className={`pill-tag ${variant}`}>{children}</span>
}

function SectionLabel({ children }) {
  return <div className="sec-label">{children}</div>
}

function InsightCard({ children, accent }) {
  return <div className="insight-card" style={accent ? { borderLeftColor: accent } : {}}>{children}</div>
}

// ── TAB: OVERVIEW ──
function OverviewTab({ cd, sd }) {
  return (
    <div className="tab-grid">
      <div className="tab-col">
        <SectionLabel>Company overview</SectionLabel>
        {safeArr(cd.overview).map((b, i) => (
          <InsightCard key={i}>{b}</InsightCard>
        ))}

        <SectionLabel>Engineering capabilities</SectionLabel>
        <div className="pill-wrap">
          {safeArr(cd.engineering_capabilities).map((c, i) => (
            <PillTag key={i}>{c}</PillTag>
          ))}
        </div>

        <SectionLabel>Project types</SectionLabel>
        <div className="pill-wrap">
          {safeArr(cd.project_types).map((p, i) => (
            <PillTag key={i}>{p}</PillTag>
          ))}
        </div>
      </div>

      <div className="tab-col">
        <SectionLabel>Software detected</SectionLabel>
        {safeArr(cd.software_mentioned).length > 0 ? (
          <div className="pill-wrap">
            {safeArr(cd.software_mentioned).map((s, i) => (
              <PillTag key={i} variant="red">{s}</PillTag>
            ))}
          </div>
        ) : (
          <div className="clean-opp">No competing FEA software detected — clean opportunity</div>
        )}

        <SectionLabel>Pain points</SectionLabel>
        {safeArr(sd.pain_points).map((p, i) => (
          <InsightCard key={i} accent="#d97706">{p}</InsightCard>
        ))}
      </div>
    </div>
  )
}

// ── TAB: PEOPLE ──
function PeopleTab({ cd }) {
  const people = cd.people || []
  const tierOrder = { Owner: 0, Founder: 1, Director: 2, Principal: 3, Senior: 4, Engineer: 5, Graduate: 6, Technician: 7, Other: 8 }
  const sorted = [...people].sort((a, b) => (tierOrder[a.tier] ?? 9) - (tierOrder[b.tier] ?? 9))

  return (
    <div>
      <SectionLabel>Key people ({people.length})</SectionLabel>
      {sorted.length === 0 ? (
        <p className="text-muted">No people found on the website.</p>
      ) : (
        <div className="people-grid">
          {sorted.map((p, i) => (
            <div key={i} className="person-card">
              <div className="person-avatar">{p.name?.split(' ').map(n => n[0]).join('').slice(0, 2).toUpperCase()}</div>
              <div className="person-info">
                <div className="person-name">{p.name}</div>
                <div className="person-role">{p.role}</div>
                <PillTag>{p.tier}</PillTag>
              </div>
              <a
                href={`https://www.linkedin.com/search/results/people/?keywords=${encodeURIComponent(p.name)}`}
                target="_blank"
                rel="noreferrer"
                className="person-linkedin"
                title="Search on LinkedIn"
              >
                in
              </a>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ── TAB: PROJECTS ──
function ProjectsTab({ cd }) {
  const projects = cd.projects || []
  const TYPE_COLORS = {
    Bridge: '#2563eb', Building: '#7c3aed', Metro: '#0891b2',
    Infrastructure: '#059669', Residential: '#d97706', Industrial: '#dc2626', Other: '#6b7280'
  }

  return (
    <div>
      <SectionLabel>Delivered projects ({projects.length})</SectionLabel>
      {projects.length === 0 ? (
        <p className="text-muted">No projects found. The site may not have a public portfolio.</p>
      ) : (
        <div className="projects-list">
          {projects.map((p, i) => (
            <div key={i} className="project-card">
              <div className="project-header">
                <span className="project-name">{p.name}</span>
                <div className="project-tags">
                  {p.type && <PillTag variant="default" >{p.type}</PillTag>}
                  {p.fem_relevant && <PillTag variant="red">FEM relevant</PillTag>}
                </div>
              </div>
              {p.description && <div className="project-desc">{p.description}</div>}
              <div className="project-meta">
                {p.location && <span>{p.location}</span>}
                {p.client && <span>Client: {p.client}</span>}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ── TAB: FEM OPPORTUNITIES ──
function OpportunitiesTab({ sd }) {
  return (
    <div className="tab-grid">
      <div className="tab-col">
        <SectionLabel>FEM / FEA opportunities</SectionLabel>
        {safeArr(sd.fem_opportunities).map((o, i) => (
          <InsightCard key={i}>
            <span className="opp-num">{String(i + 1).padStart(2, '0')}</span>
            {o}
          </InsightCard>
        ))}
      </div>
      <div className="tab-col">
        <SectionLabel>Hiring signals</SectionLabel>
        {safeArr(sd.hiring_signals).map((s, i) => (
          <div key={i} className="signal-card">▲ {s}</div>
        ))}
        <SectionLabel>Expansion signals</SectionLabel>
        {safeArr(sd.expansion_signals).map((s, i) => (
          <div key={i} className="signal-card">◆ {s}</div>
        ))}
      </div>
    </div>
  )
}

// ── TAB: STRATEGY ──
function StrategyTab({ sd }) {
  return (
    <div className="tab-grid">
      <div className="tab-col">
        <SectionLabel>Entry point</SectionLabel>
        <div className="info-box blue">{safeStr(sd.entry_point) || 'Not determined'}</div>

        <SectionLabel>Value positioning</SectionLabel>
        <div className="info-box green">{safeStr(sd.value_positioning) || 'Not determined'}</div>

        <SectionLabel>Likely objections</SectionLabel>
        {safeArr(sd.likely_objections).map((o, i) => (
          <InsightCard key={i} accent="#d97706">⚠ {o}</InsightCard>
        ))}

        <SectionLabel>Recommended MIDAS products</SectionLabel>
        <div className="pill-wrap">
          {safeArr(sd.recommended_products).map((p, i) => (
            <PillTag key={i} variant="red">{p}</PillTag>
          ))}
        </div>
        {sd.product_reason && <p className="text-muted mt-sm">{safeStr(sd.product_reason)}</p>}
      </div>

      <div className="tab-col">
        <SectionLabel>Pre-meeting cheat sheet</SectionLabel>
        <div className="cheat-label">3 THINGS TO MENTION</div>
        {safeArr(sd.pre_meeting_mention).map((m, i) => (
          <div key={i} className="cheat-item">✓ {m}</div>
        ))}

        <div className="cheat-label mt-md">3 SMART QUESTIONS</div>
        {safeArr(sd.smart_questions).map((q, i) => (
          <div key={i} className="cheat-item">? {q}</div>
        ))}

        <SectionLabel>Opening line</SectionLabel>
        {sd.opening_line && (
          <blockquote className="opening-line">
            <span className="quote-mark">"</span>
            {safeStr(sd.opening_line)}
          </blockquote>
        )}
      </div>
    </div>
  )
}

// ── TAB: VACANCIES ──
function VacanciesTab({ cd }) {
  const roles = cd.open_roles || []
  const femCount = roles.filter(r => r.fem_mentioned).length

  return (
    <div>
      {femCount > 0 && (
        <div className="info-box green">
          🎯 {femCount} role(s) explicitly mention FEM/FEA — strong buying signal
        </div>
      )}
      <SectionLabel>Open roles</SectionLabel>
      {roles.length === 0 ? (
        <p className="text-muted">No relevant vacancies found.</p>
      ) : (
        <div className="roles-list">
          {roles.map((r, i) => (
            <div key={i} className="role-card">
              <div className="role-header">
                <span className="role-title">{r.title}</span>
                {r.fem_mentioned && <PillTag variant="red">FEM MENTIONED</PillTag>}
              </div>
              <div className="pill-wrap">
                {(r.skills || []).map((s, j) => <PillTag key={j}>{s}</PillTag>)}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ── TAB: EMAIL ──
function EmailTab({ cd, sd, apiBase }) {
  const [email, setEmail] = useState('')
  const [generating, setGenerating] = useState(false)
  const [copied, setCopied] = useState(false)

  const handleGenerate = async () => {
    setGenerating(true)
    try {
      const res = await fetch(`${apiBase}/api/email`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ company_data: cd, sales_data: sd })
      })
      const data = await res.json()
      setEmail(data.email || '')
    } catch (e) {
      console.error('Failed to generate email:', e)
    }
    setGenerating(false)
  }

  const handleCopy = () => {
    navigator.clipboard.writeText(email)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  // Parse subject line
  const lines = email.split('\n')
  const subjectLine = lines.find(l => l.startsWith('Subject:'))
  const subject = subjectLine ? subjectLine.replace('Subject:', '').trim() : ''
  const body = lines.filter(l => !l.startsWith('Subject:')).join('\n').trim()

  return (
    <div>
      <SectionLabel>Cold outreach email</SectionLabel>
      <p className="text-muted mb-md">Generate a personalised cold email based on the company intelligence. Edit before sending.</p>

      <button className="action-btn" onClick={handleGenerate} disabled={generating}>
        {generating ? 'Generating...' : '✉ Generate email draft'}
      </button>

      {email && (
        <div className="email-preview mt-md">
          {subject && (
            <>
              <div className="cheat-label">SUBJECT LINE</div>
              <div className="email-subject">{subject}</div>
            </>
          )}
          <div className="cheat-label mt-sm">EMAIL BODY</div>
          <textarea
            className="email-body"
            value={body}
            onChange={(e) => {
              const newEmail = subject ? `Subject: ${subject}\n\n${e.target.value}` : e.target.value
              setEmail(newEmail)
            }}
            rows={14}
          />
          <div className="email-actions">
            <button className="action-btn secondary" onClick={handleCopy}>
              {copied ? '✓ Copied!' : '📋 Copy to clipboard'}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

// ── TAB: EXPORT ──
function ExportTab({ report, apiBase }) {
  const [note, setNote] = useState('')
  const [noteLoaded, setNoteLoaded] = useState(false)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    if (!report?.domain) return
    fetch(`${apiBase}/api/notes/${encodeURIComponent(report.domain)}`)
      .then(r => r.json())
      .then(d => { setNote(d.text || ''); setNoteLoaded(true) })
      .catch(() => setNoteLoaded(true))
  }, [report?.domain, apiBase])

  const handleSaveNote = async () => {
    setSaving(true)
    try {
      await fetch(`${apiBase}/api/notes`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ domain: report.domain, note })
      })
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } catch (e) {
      console.error('Failed to save note:', e)
    }
    setSaving(false)
  }

  return (
    <div className="tab-grid">
      <div className="tab-col">
        <SectionLabel>PDF export</SectionLabel>
        <div className="export-card">
          <div className="export-title">PDF sales dossier</div>
          <div className="text-muted">Ready to print or share before a meeting.</div>
        </div>
        <a
          href={`${apiBase}/api/export/pdf/${encodeURIComponent(report.domain)}`}
          target="_blank"
          rel="noreferrer"
          className="action-btn mt-sm"
          style={{ display: 'inline-block', textDecoration: 'none', textAlign: 'center' }}
        >
          📥 Download PDF
        </a>
      </div>

      <div className="tab-col">
        <SectionLabel>Rep notes</SectionLabel>
        <textarea
          className="notes-textarea"
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="Add your notes — call outcome, follow-up date, key contacts spoken to..."
          rows={8}
        />
        <button className="action-btn mt-sm" onClick={handleSaveNote} disabled={saving}>
          {saved ? '✓ Saved!' : saving ? 'Saving...' : '💾 Save notes'}
        </button>
      </div>
    </div>
  )
}


// ── MAIN REPORT COMPONENT ──
export function Report({ report, apiBase, firecrawlKey, onRefresh }) {
  const [activeTab, setActiveTab] = useState('overview')

  const cd = report.company_data || {}
  const sd = report.sales_data || {}
  const score = sd.overall_score || report.score || 'Cold'
  const companyName = cd.company_name || report.company || report.domain

  const renderTab = () => {
    switch (activeTab) {
      case 'overview':      return <OverviewTab cd={cd} sd={sd} />
      case 'people':        return <PeopleTab cd={cd} />
      case 'projects':      return <ProjectsTab cd={cd} />
      case 'opportunities': return <OpportunitiesTab sd={sd} />
      case 'strategy':      return <StrategyTab sd={sd} />
      case 'vacancies':     return <VacanciesTab cd={cd} />
      case 'email':         return <EmailTab cd={cd} sd={sd} apiBase={apiBase} />
      case 'export':        return <ExportTab report={report} apiBase={apiBase} />
      default:              return null
    }
  }

  return (
    <div className="report">
      {/* Report header */}
      <div className="report-header">
        <div className="report-header-left">
          <h1 className="report-company">{companyName}</h1>
          <div className="report-domain">
            <a href={`https://${report.domain}`} target="_blank" rel="noreferrer">
              {report.domain} ↗
            </a>
            {report.date && <span className="report-date">Analysed {report.days_ago || report.date}</span>}
            <button className="recrawl-btn" onClick={onRefresh} title="Re-crawl this company fresh">
              🔄 Re-crawl
            </button>
          </div>
          <div className="report-meta">
            <ScoreBadge score={score} />
            {cd.locations?.length > 0 && <span>{cd.locations.join(', ')}</span>}
            {cd.employee_count && <span>{cd.employee_count}</span>}
            {cd.founded && <span>Est. {cd.founded}</span>}
            {cd.confidence && <PillTag>{cd.confidence} confidence</PillTag>}
          </div>
          {sd.score_reason && <p className="report-reason">{sd.score_reason}</p>}
        </div>
        <div className="report-header-right">
          <div className="report-stat">
            <span className="stat-value">{report.pages_count || 0}</span>
            <span className="stat-label">Pages</span>
          </div>
          <div className="report-stat">
            <span className="stat-value">{(cd.people || []).length}</span>
            <span className="stat-label">People</span>
          </div>
          <div className="report-stat">
            <span className="stat-value">{(cd.projects || []).length}</span>
            <span className="stat-label">Projects</span>
          </div>
        </div>
      </div>

      {/* Tabs — instant client-side switching, no re-renders */}
      <div className="tab-bar">
        {TABS.map(tab => (
          <button
            key={tab.id}
            className={`tab-btn ${activeTab === tab.id ? 'active' : ''}`}
            onClick={() => setActiveTab(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Tab content */}
      <div className="tab-content">
        <TabErrorBoundary key={activeTab}>
          {renderTab()}
        </TabErrorBoundary>
      </div>
    </div>
  )
}
