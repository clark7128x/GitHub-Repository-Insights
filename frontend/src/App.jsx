import { useState } from 'react'
import './index.css'

const MAX_POLL_ATTEMPTS = 60  // 60 × 2s = 2 minutes

function App() {
  const [repo, setRepo] = useState('')
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState('')
  const [pollCount, setPollCount] = useState(0)

  const handleAnalyze = async (e) => {
    e.preventDefault()
    setLoading(true)
    setError('')
    setResult(null)
    setPollCount(0)

    try {
      const apiUrl = import.meta.env.VITE_API_URL || 'http://localhost:8000'
      const response = await fetch(`${apiUrl}/api/analyze`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ repo })
      })

      if (!response.ok) throw new Error('Analysis request failed')
      const data = await response.json()
      
      pollResult(data.task_id, apiUrl, 0)
    } catch (err) {
      setError(err.message)
      setLoading(false)
    }
  }

  const pollResult = async (taskId, apiUrl, attempt) => {
    if (attempt >= MAX_POLL_ATTEMPTS) {
      setError('Analysis is taking longer than expected. Please try again later or check the API directly.')
      setLoading(false)
      return
    }

    try {
      const res = await fetch(`${apiUrl}/api/results/${taskId}`)
      if (!res.ok) throw new Error('Failed to fetch results')
      const data = await res.json()

      if (data.status === 'completed' || data.status === 'failed') {
        setResult(data)
        setLoading(false)
      } else {
        setPollCount(attempt + 1)
        setTimeout(() => pollResult(taskId, apiUrl, attempt + 1), 2000)
      }
    } catch (err) {
      setError(err.message)
      setLoading(false)
    }
  }

  return (
    <div className="container">
      <header>
        <h1>GitHub Repository Insights</h1>
        <p>Analyze engineering health metrics for public repositories.</p>
      </header>

      <main>
        <form onSubmit={handleAnalyze} className="search-form">
          <input 
            type="text" 
            placeholder="owner/repo (e.g., pallets/flask)" 
            value={repo}
            onChange={(e) => setRepo(e.target.value)}
            required
          />
          <button type="submit" disabled={loading}>
            {loading ? 'Analyzing...' : 'Analyze Repository'}
          </button>
        </form>

        {loading && (
          <div className="status-message">
            Analyzing repository… (attempt {pollCount + 1} of {MAX_POLL_ATTEMPTS})
          </div>
        )}

        {error && <div className="error">{error}</div>}

        {result && result.status === 'completed' && (
          <div className="results-card">
            <h2>Analysis for {result.repo_name}</h2>
            <div className="metrics-grid">
              <div className="metric">
                <h3>Bus Factor</h3>
                <div className="value">{result.bus_factor}</div>
                <div className="desc">Developers responsible for 50% of recent commits</div>
              </div>
              <div className="metric">
                <h3>Avg PR Lifetime</h3>
                <div className="value">{result.avg_pr_lifetime_hours != null ? `${result.avg_pr_lifetime_hours}h` : 'N/A'}</div>
                <div className="desc">Average hours to close/merge a PR</div>
              </div>
            </div>
            
            <h3>Hot Files (Most Modified)</h3>
            <ul className="hot-files">
              {result.hot_files && result.hot_files.map((f, i) => (
                <li key={i}>
                  <span className="file-name">{f.file}</span>
                  <span className="changes-badge">{f.changes} changes</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {result && result.status === 'failed' && (
          <div className="error">Analysis Failed: {result.error_message}</div>
        )}
      </main>
    </div>
  )
}

export default App
