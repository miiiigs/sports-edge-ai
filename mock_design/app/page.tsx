'use client'

import { useMemo, useState } from 'react'
import {
  Activity,
  BarChart3,
  Bell,
  CalendarDays,
  ChevronDown,
  CircleHelp,
  Command,
  Filter,
  Gauge,
  LayoutDashboard,
  Menu,
  RefreshCw,
  Search,
  Settings2,
  Shield,
  Star,
  Target,
  TrendingUp,
  Trophy,
  X,
  Zap,
} from 'lucide-react'

const matches = [
  { id: 1, league: 'Premier League', time: 'LIVE 72\'', home: 'Arsenal', away: 'Chelsea', score: '2 — 1', win: '78%', trend: '+4.8%', status: 'live', edge: 'Strong', color: 'cyan' },
  { id: 2, league: 'La Liga', time: 'Today · 15:00', home: 'Barcelona', away: 'Athletic Club', score: '—', win: '64%', trend: '+2.1%', status: 'upcoming', edge: 'Lean home', color: 'purple' },
  { id: 3, league: 'Serie A', time: 'Today · 17:30', home: 'Inter Milan', away: 'Napoli', score: '—', win: '57%', trend: '-0.6%', status: 'upcoming', edge: 'Tight', color: 'blue' },
  { id: 4, league: 'Bundesliga', time: 'Today · 19:30', home: 'Bayern Munich', away: 'Dortmund', score: '—', win: '71%', trend: '+3.4%', status: 'upcoming', edge: 'Strong', color: 'cyan' },
  { id: 5, league: 'Ligue 1', time: 'Tomorrow · 20:00', home: 'PSG', away: 'Lyon', score: '—', win: '69%', trend: '+1.8%', status: 'upcoming', edge: 'Strong', color: 'purple' },
]

const nav = [
  { label: 'Overview', icon: LayoutDashboard },
  { label: 'Live Monitor', icon: Activity },
  { label: 'Predictions', icon: Target },
  { label: 'Watchlist', icon: Star },
]

function StatusDot({ live = false }: { live?: boolean }) {
  return <span className={`status-dot ${live ? 'status-dot-live' : ''}`} aria-hidden="true" />
}

export default function Page() {
  const [active, setActive] = useState('Overview')
  const [filter, setFilter] = useState('All matches')
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState(1)
  const [refreshing, setRefreshing] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)

  const visibleMatches = useMemo(() => matches.filter((match) => {
    const matchesQuery = `${match.home} ${match.away} ${match.league}`.toLowerCase().includes(query.toLowerCase())
    const matchesFilter = filter === 'All matches' || (filter === 'Live now' ? match.status === 'live' : match.edge === filter)
    return matchesQuery && matchesFilter
  }), [filter, query])

  function refresh() {
    setRefreshing(true)
    window.setTimeout(() => setRefreshing(false), 900)
  }

  return (
    <main className="app-shell">
      <aside className={`sidebar ${menuOpen ? 'sidebar-open' : ''}`}>
        <div className="brand"><span className="brand-mark"><Zap size={16} fill="currentColor" /></span><span>SPORTS EDGE <em>AI</em></span></div>
        <div className="workspace"><span className="avatar">SE</span><span><b>Edge workspace</b><small>Analyst terminal</small></span><ChevronDown size={14} /></div>
        <nav aria-label="Primary navigation">
          <p className="eyebrow">WORKSPACE</p>
          {nav.map(({ label, icon: Icon }) => <button key={label} className={`nav-item ${active === label ? 'nav-active' : ''}`} onClick={() => { setActive(label); setMenuOpen(false) }}><Icon size={17} /><span>{label}</span>{label === 'Live Monitor' && <StatusDot live />}</button>)}
          <p className="eyebrow nav-gap">TOOLS</p>
          <button className="nav-item"><BarChart3 size={17} /><span>Performance</span></button>
          <button className="nav-item"><CalendarDays size={17} /><span>Fixtures</span></button>
        </nav>
        <div className="sidebar-bottom"><button className="nav-item"><Settings2 size={17} /><span>Settings</span></button><button className="nav-item"><CircleHelp size={17} /><span>Help center</span></button><div className="pro-card"><div><span className="pulse-line" /><small>MODEL STATUS</small></div><strong>All systems nominal</strong><span className="pro-version">v2.8.4 · updated 4m ago</span></div></div>
      </aside>

      <section className="content-shell">
        <header className="topbar"><button className="mobile-menu" onClick={() => setMenuOpen(!menuOpen)} aria-label="Open navigation">{menuOpen ? <X size={20} /> : <Menu size={20} />}</button><div className="crumb"><span>Workspace</span><span>/</span><b>{active}</b></div><div className="top-actions"><button className="icon-button" aria-label="Notifications"><Bell size={17} /><i /></button><button className="profile-button"><span className="avatar avatar-small">JD</span><span>Jordan Davis</span><ChevronDown size={14} /></button></div></header>
        <div className="page-wrap">
          <div className="page-heading"><div><p className="eyebrow cyan-text">THURSDAY · AUGUST 20, 2026</p><h1>Good morning, Jordan.</h1><p className="subhead">Your edge across today&apos;s football slate.</p></div><button className="refresh-button" onClick={refresh}><RefreshCw size={15} className={refreshing ? 'spin' : ''} />{refreshing ? 'Syncing...' : 'Refresh data'}</button></div>
          <div className="kpi-grid"><div className="kpi"><span className="kpi-label">MODEL ACCURACY <span className="help">?</span></span><strong>84.6<span>%</span></strong><small className="positive"><TrendingUp size={12} /> +2.4% <i>vs last 30 days</i></small></div><div className="kpi"><span className="kpi-label">ACTIVE SIGNALS</span><strong>12</strong><small className="positive"><Activity size={12} /> 4 high confidence</small></div><div className="kpi"><span className="kpi-label">VALUE INDEX</span><strong>+18.2<span>%</span></strong><small className="positive"><TrendingUp size={12} /> Strong market edge</small></div><div className="kpi kpi-muted"><span className="kpi-label">NEXT KICKOFF</span><strong>02<span>h</span> 18<span>m</span></strong><small>Barcelona vs Athletic Club</small></div></div>

          <div className="section-row"><div><p className="eyebrow cyan-text">REAL-TIME FEED</p><h2>Live now</h2></div><span className="live-badge"><StatusDot live /> 1 match in progress</span></div>
          <section className="live-card"><div className="live-card-top"><div className="league-label"><span className="league-icon">PL</span><span><b>Premier League</b><small>Matchday 2 · Emirates Stadium</small></span></div><span className="live-clock"><StatusDot live /> LIVE 72:14</span></div><div className="score-row"><div className="team"><span className="team-crest">A</span><strong>Arsenal</strong><small>Home</small></div><div className="score"><strong>2 <span>—</span> 1</strong><small>HALF TIME · 2 — 0</small></div><div className="team team-away"><span className="team-crest crest-blue">C</span><strong>Chelsea</strong><small>Away</small></div></div><div className="live-insights"><div><span>WIN PROBABILITY</span><div className="probability"><b>78%</b><div className="bar"><i style={{ width: '78%' }} /></div><b>22%</b></div><small>Model favors Arsenal <em>strongly</em></small></div><div><span>LIVE MOMENTUM</span><div className="momentum"><i /><i /><i /><i /><i /><i /><i /><i /><i /><i /></div><small>Arsenal controlling the tempo</small></div><button className="details-button" onClick={() => setSelected(1)}>View match <span>→</span></button></div></section>

          <div className="workspace-grid"><section className="watchlist-panel"><div className="panel-heading"><div><p className="eyebrow">YOUR SLATE</p><h2>Watchlist <span>5</span></h2></div><button className="filter-button"><Filter size={14} /> Filters</button></div><div className="table-toolbar"><div className="segmented">{['All matches', 'Live now', 'Strong'].map((item) => <button key={item} className={filter === item ? 'segment-active' : ''} onClick={() => setFilter(item)}>{item}</button>)}</div><label className="search-box"><Search size={15} /><input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search matches" aria-label="Search matches" /></label></div><div className="match-table"><div className="table-head"><span>MATCH</span><span>MODEL EDGE</span><span>PROBABILITY</span><span /></div>{visibleMatches.map((match) => <button className={`match-row ${selected === match.id ? 'match-selected' : ''}`} key={match.id} onClick={() => setSelected(match.id)}><span className="match-info"><span className={`club-dot ${match.color}`} /> <span><b>{match.home} <small>vs</small> {match.away}</b><em>{match.league} · {match.time}</em></span></span><span className="edge-cell"><b>{match.edge}</b><small className={match.trend.startsWith('+') ? 'positive' : 'negative'}>{match.trend}</small></span><span className="prob-cell"><b>{match.win}</b><span className="mini-bar"><i style={{ width: match.win }} /></span></span><span className="row-arrow">→</span></button>)}</div></section>

            <aside className="right-rail"><section className="rail-panel"><div className="panel-heading"><div><p className="eyebrow">MODEL INTELLIGENCE</p><h2>Signal health</h2></div><Gauge size={19} className="cyan-text" /></div><div className="health-score"><strong>92</strong><span>/ 100</span><div className="health-ring"><span>GOOD</span></div></div><div className="health-list"><div><span><StatusDot />Market data</span><b>Live</b></div><div><span><StatusDot />Injury feed</span><b>Synced 4m</b></div><div><span><StatusDot />Model output</span><b>Stable</b></div></div></section><section className="rail-panel next-match"><p className="eyebrow cyan-text">UP NEXT</p><div className="next-time"><CalendarDays size={14} /> 02h 18m</div><h3>Barcelona <span>vs</span> Athletic Club</h3><p>La Liga · Today, 15:00</p><div className="next-prediction"><span>MODEL PREDICTION</span><strong>Barcelona win <b>64%</b></strong><small>Fair odds <b>1.56</b></small></div><button className="rail-link" onClick={() => setActive('Predictions')}>Open prediction <span>↗</span></button></section></aside></div>
          <footer><span><StatusDot /> Data updated 4 minutes ago</span><span>Sports Edge AI · For informational purposes only</span></footer>
        </div>
      </section>
    </main>
  )
}
