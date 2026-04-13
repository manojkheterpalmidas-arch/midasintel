import { useState } from 'react'

export function ApiKeyGate({ onSave }) {
  const [key, setKey] = useState('')
  const [error, setError] = useState('')

  const handleSubmit = (e) => {
    e.preventDefault()
    if (!key) {
      setError('Please enter a key')
      return
    }
    if (!key.startsWith('fc-')) {
      setError('Key must start with fc-')
      return
    }
    onSave(key)
  }

  return (
    <div className="gate-screen">
      <div className="gate-card">
        <div className="gate-brand">MIDAS IT</div>
        <h1 className="gate-title">Pre Sales Intelligence</h1>
        <div className="gate-line" />
        <p className="gate-desc">
          Enter your Firecrawl API key to get started.
          <br />
          Get one free at{' '}
          <a href="https://www.firecrawl.dev/app/api-keys" target="_blank" rel="noreferrer">
            firecrawl.dev ↗
          </a>
        </p>
        <form onSubmit={handleSubmit} className="gate-form">
          <input
            type="password"
            value={key}
            onChange={(e) => { setKey(e.target.value); setError('') }}
            placeholder="fc-..."
            className="gate-input"
            autoFocus
          />
          {error && <div className="gate-error">{error}</div>}
          <button type="submit" className="gate-btn">UNLOCK</button>
        </form>
      </div>
    </div>
  )
}
