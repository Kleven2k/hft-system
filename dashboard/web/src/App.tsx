import { BrowserRouter, Routes, Route, NavLink } from 'react-router-dom'
import './App.css'
import OverviewPage from './pages/OverviewPage'
import BacktestsPage from './pages/BacktestsPage'
import LivePage from './pages/LivePage'
import MarketPage from './pages/MarketPage'
import TickDataPage from './pages/TickDataPage'

function App() {
  return (
    <BrowserRouter>
      <nav>
        <NavLink to="/" end>Overview</NavLink>
        <NavLink to="/backtests">Backtests</NavLink>
        <NavLink to="/live">Live</NavLink>
        <NavLink to="/market">Market</NavLink>
        <NavLink to="/ticks">Tick Data</NavLink>
      </nav>

      <main>
        <Routes>
          <Route path="/" element={<OverviewPage />} />
          <Route path="/backtests" element={<BacktestsPage />} />
          <Route path="/live" element={<LivePage />} />
          <Route path="/market" element={<MarketPage />} />
          <Route path="/ticks" element={<TickDataPage />} />
        </Routes>
      </main>
    </BrowserRouter>
  )
}

export default App
