"use client";

import { useState, useEffect, useCallback } from "react";
import {
  Activity,
  TrendingUp,
  TrendingDown,
  RefreshCw,
  Zap,
  Shield,
  BarChart2,
  AlertTriangle,
  CheckCircle,
  Clock,
  DollarSign,
} from "lucide-react";

// ── Types ──────────────────────────────────────────────────────────────────────

interface AccountInfo {
  portfolio_value?: number;
  cash?: number;
  buying_power?: number;
  equity?: number;
  error?: string;
}

interface StatusData {
  account: AccountInfo;
  stats: {
    open_positions: number;
    total_trades: number;
    total_signals: number;
    last_scan: string | null;
  };
}

interface Position {
  id: number;
  ticker: string;
  direction: string;
  shares: number;
  entry_price: number;
  current_price: number;
  stop_loss_price: number;
  unrealized_pnl: number;
  unrealized_pct: number;
  entry_time: string | null;
}

interface ConvictionSignal {
  id: number;
  ticker: string;
  score: number;
  direction: string;
  regime: string;
  above_threshold: boolean;
  signals_fired: string[];
  timestamp: string | null;
}

interface Trade {
  id: number;
  ticker: string;
  direction: string;
  entry_price: number;
  exit_price: number | null;
  shares: number;
  pnl: number | null;
  pnl_pct: number | null;
  exit_reason: string | null;
  entry_time: string | null;
  exit_time: string | null;
  conviction_score: number;
  regime_at_entry: string | null;
}

interface TradesData {
  trades: Trade[];
  summary: {
    total: number;
    closed: number;
    win_rate: number | null;
    total_pnl: number;
  };
}

interface Regime {
  regime: string;
  vix_level: number | null;
  spy_vs_200ma: number | null;
  yield_spread: number | null;
  fear_greed: number | null;
  regime_score: number | null;
  date: string | null;
}

interface ScanResult {
  success: boolean;
  summary?: {
    regime: string;
    tickers_scanned: number;
    total_signals: number;
    fire_signals: number;
    watch_signals: number;
    trades_executed: number;
    duration_seconds: number;
    timestamp: string;
  };
  error?: string;
}

// ── Helpers ────────────────────────────────────────────────────────────────────

function fmt(n: number | null | undefined, decimals = 2): string {
  if (n == null) return "—";
  return n.toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function fmtMoney(n: number | null | undefined): string {
  if (n == null) return "—";
  return "$" + fmt(n);
}

function fmtPct(n: number | null | undefined): string {
  if (n == null) return "—";
  return (n >= 0 ? "+" : "") + fmt(n) + "%";
}

function fmtDate(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function pnlColor(v: number | null | undefined): string {
  if (v == null) return "text-zinc-400";
  return v >= 0 ? "text-emerald-400" : "text-red-400";
}

const REGIME_STYLES: Record<string, { label: string; color: string; bg: string }> = {
  bull: { label: "Bull Market", color: "text-emerald-400", bg: "bg-emerald-400/10 border-emerald-400/20" },
  neutral: { label: "Neutral", color: "text-zinc-300", bg: "bg-zinc-700/40 border-zinc-600/20" },
  bear: { label: "Bear Market", color: "text-red-400", bg: "bg-red-400/10 border-red-400/20" },
  high_vol: { label: "High Volatility", color: "text-amber-400", bg: "bg-amber-400/10 border-amber-400/20" },
  crash: { label: "Crash Conditions", color: "text-red-500", bg: "bg-red-500/10 border-red-500/20" },
};

// ── Sub-components ─────────────────────────────────────────────────────────────

function Card({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return (
    <div className={`rounded-xl border border-zinc-800 bg-zinc-900 p-5 ${className}`}>
      {children}
    </div>
  );
}

function StatCard({
  label,
  value,
  sub,
  icon: Icon,
  valueClass = "",
}: {
  label: string;
  value: string;
  sub?: string;
  icon: React.ElementType;
  valueClass?: string;
}) {
  return (
    <Card>
      <div className="flex items-start justify-between">
        <div>
          <p className="text-xs font-medium uppercase tracking-wider text-zinc-500">{label}</p>
          <p className={`mt-1 text-2xl font-semibold tabular-nums ${valueClass || "text-zinc-100"}`}>
            {value}
          </p>
          {sub && <p className="mt-0.5 text-xs text-zinc-500">{sub}</p>}
        </div>
        <div className="rounded-lg bg-zinc-800 p-2">
          <Icon className="h-4 w-4 text-zinc-400" />
        </div>
      </div>
    </Card>
  );
}

function ConvictionBar({ score }: { score: number }) {
  const abs = Math.abs(score);
  const pct = Math.round(abs * 100);
  const color = score > 0 ? "bg-emerald-500" : "bg-red-500";
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-24 overflow-hidden rounded-full bg-zinc-800">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${pct}%` }} />
      </div>
      <span className={`tabular-nums text-xs font-medium ${score >= 0 ? "text-emerald-400" : "text-red-400"}`}>
        {(score >= 0 ? "+" : "") + score.toFixed(2)}
      </span>
    </div>
  );
}

function ActionBadge({ action }: { action: string }) {
  if (action === "FIRE") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-emerald-400/10 px-2 py-0.5 text-xs font-semibold text-emerald-400 border border-emerald-400/20">
        <Zap className="h-3 w-3" /> FIRE
      </span>
    );
  }
  return (
    <span className="inline-flex items-center rounded-full bg-amber-400/10 px-2 py-0.5 text-xs font-semibold text-amber-400 border border-amber-400/20">
      WATCH
    </span>
  );
}

function SectionHeader({ title, count }: { title: string; count?: number }) {
  return (
    <div className="mb-4 flex items-center gap-2">
      <h2 className="text-sm font-semibold uppercase tracking-wider text-zinc-400">{title}</h2>
      {count != null && (
        <span className="rounded-full bg-zinc-800 px-2 py-0.5 text-xs text-zinc-500">{count}</span>
      )}
    </div>
  );
}

function EmptyState({ message }: { message: string }) {
  return (
    <div className="flex h-20 items-center justify-center rounded-lg border border-dashed border-zinc-800 text-sm text-zinc-600">
      {message}
    </div>
  );
}

// ── Main Page ──────────────────────────────────────────────────────────────────

export default function Dashboard() {
  const [status, setStatus] = useState<StatusData | null>(null);
  const [positions, setPositions] = useState<Position[]>([]);
  const [signals, setSignals] = useState<ConvictionSignal[]>([]);
  const [tradesData, setTradesData] = useState<TradesData | null>(null);
  const [regime, setRegime] = useState<Regime | null>(null);
  const [loading, setLoading] = useState(true);
  const [scanning, setScanning] = useState(false);
  const [scanResult, setScanResult] = useState<ScanResult | null>(null);
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null);
  const [errors, setErrors] = useState<string[]>([]);

  const fetchAll = useCallback(async () => {
    const errs: string[] = [];
    const safe = async <T,>(url: string): Promise<T | null> => {
      try {
        const r = await fetch(url);
        if (!r.ok) throw new Error(`${r.status}`);
        return r.json();
      } catch (e) {
        errs.push(`${url}: ${e}`);
        return null;
      }
    };

    const [s, p, sig, t, reg] = await Promise.all([
      safe<StatusData>("/api/status"),
      safe<{ positions: Position[] }>("/api/portfolio"),
      safe<{ signals: ConvictionSignal[] }>("/api/signals"),
      safe<TradesData>("/api/trades"),
      safe<{ regime: Regime | null }>("/api/regime"),
    ]);

    if (s) setStatus(s);
    if (p) setPositions(p.positions);
    if (sig) setSignals(sig.signals);
    if (t) setTradesData(t);
    if (reg) setRegime(reg.regime);
    setErrors(errs);
    setLastUpdate(new Date());
    setLoading(false);
  }, []);

  useEffect(() => {
    fetchAll();
    const id = setInterval(fetchAll, 60_000);
    return () => clearInterval(id);
  }, [fetchAll]);

  const runScan = async () => {
    setScanning(true);
    setScanResult(null);
    try {
      const r = await fetch("/api/scan", { method: "POST" });
      const data: ScanResult = await r.json();
      setScanResult(data);
      await fetchAll();
    } catch (e) {
      setScanResult({ success: false, error: String(e) });
    } finally {
      setScanning(false);
    }
  };

  // Derived data
  const fireSignals = signals.filter((s) => s.above_threshold);
  const watchSignals = signals.filter((s) => !s.above_threshold).slice(0, 10);
  const recentTrades = tradesData?.trades.slice(0, 10) ?? [];
  const regimeStyle = regime ? (REGIME_STYLES[regime.regime] ?? REGIME_STYLES.neutral) : null;

  const portfolioValue = status?.account?.portfolio_value;
  const cash = status?.account?.cash;
  const winRate = tradesData?.summary?.win_rate;
  const totalPnl = tradesData?.summary?.total_pnl ?? 0;

  return (
    <div className="min-h-screen bg-[#09090b]">
      {/* Header */}
      <header className="sticky top-0 z-10 border-b border-zinc-800 bg-[#09090b]/90 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-6 py-3">
          <div className="flex items-center gap-3">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-emerald-500/10 border border-emerald-500/20">
              <Activity className="h-4 w-4 text-emerald-400" />
            </div>
            <div>
              <h1 className="text-sm font-semibold tracking-wide">ATLAS</h1>
              <p className="text-[10px] text-zinc-500 leading-none">Algorithmic Trading System</p>
            </div>
          </div>

          <div className="flex items-center gap-4">
            {lastUpdate && (
              <span className="hidden text-xs text-zinc-600 sm:flex items-center gap-1">
                <Clock className="h-3 w-3" />
                {lastUpdate.toLocaleTimeString()}
              </span>
            )}
            <button
              onClick={fetchAll}
              className="rounded-lg border border-zinc-800 bg-zinc-900 p-1.5 text-zinc-400 hover:text-zinc-100 transition-colors"
              title="Refresh"
            >
              <RefreshCw className="h-3.5 w-3.5" />
            </button>
            <button
              onClick={runScan}
              disabled={scanning}
              className="flex items-center gap-2 rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-emerald-500 disabled:opacity-60 disabled:cursor-not-allowed transition-colors"
            >
              {scanning ? (
                <>
                  <RefreshCw className="h-3.5 w-3.5 animate-spin" /> Scanning…
                </>
              ) : (
                <>
                  <Zap className="h-3.5 w-3.5" /> Run Scan
                </>
              )}
            </button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-7xl space-y-6 px-6 py-6">
        {/* Scan result banner */}
        {scanResult && (
          <div
            className={`rounded-xl border p-4 text-sm ${
              scanResult.success
                ? "border-emerald-500/20 bg-emerald-500/5 text-emerald-300"
                : "border-red-500/20 bg-red-500/5 text-red-300"
            }`}
          >
            {scanResult.success && scanResult.summary ? (
              <div className="flex flex-wrap items-center gap-x-6 gap-y-1">
                <span className="flex items-center gap-1 font-semibold">
                  <CheckCircle className="h-4 w-4" /> Scan complete
                </span>
                <span>Regime: <strong>{scanResult.summary.regime}</strong></span>
                <span>{scanResult.summary.tickers_scanned} tickers</span>
                <span>{scanResult.summary.fire_signals} FIRE · {scanResult.summary.watch_signals} WATCH</span>
                <span>{scanResult.summary.trades_executed} trade(s) executed</span>
                <span className="text-emerald-500">{scanResult.summary.duration_seconds}s</span>
              </div>
            ) : (
              <span className="flex items-center gap-1">
                <AlertTriangle className="h-4 w-4" /> {scanResult.error ?? "Scan failed"}
              </span>
            )}
          </div>
        )}

        {/* Top stat cards */}
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <StatCard
            label="Portfolio Value"
            value={loading ? "…" : fmtMoney(portfolioValue)}
            sub={cash != null ? `${fmtMoney(cash)} cash` : undefined}
            icon={DollarSign}
          />
          <StatCard
            label="Total P&L"
            value={loading ? "…" : fmtMoney(totalPnl)}
            icon={totalPnl >= 0 ? TrendingUp : TrendingDown}
            valueClass={pnlColor(totalPnl)}
          />
          <StatCard
            label="Open Positions"
            value={loading ? "…" : String(status?.stats?.open_positions ?? 0)}
            sub={`of ${positions.length} tracked`}
            icon={BarChart2}
          />
          <StatCard
            label="Win Rate"
            value={loading ? "…" : winRate != null ? `${winRate}%` : "—"}
            sub={tradesData ? `${tradesData.summary.closed} closed trades` : undefined}
            icon={Shield}
            valueClass={
              winRate != null
                ? winRate >= 55
                  ? "text-emerald-400"
                  : winRate >= 45
                  ? "text-amber-400"
                  : "text-red-400"
                : ""
            }
          />
        </div>

        {/* Regime + Signals row */}
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
          {/* Market Regime */}
          <Card>
            <SectionHeader title="Market Regime" />
            {regime ? (
              <div className="space-y-3">
                <div
                  className={`inline-flex items-center rounded-lg border px-3 py-1.5 text-sm font-semibold ${regimeStyle?.bg} ${regimeStyle?.color}`}
                >
                  {regimeStyle?.label ?? regime.regime}
                </div>
                <div className="grid grid-cols-2 gap-2 text-sm">
                  <div className="rounded-lg bg-zinc-800/50 p-2">
                    <p className="text-[10px] text-zinc-500 uppercase tracking-wide">VIX</p>
                    <p className={`font-semibold tabular-nums ${(regime.vix_level ?? 0) > 25 ? "text-amber-400" : "text-zinc-200"}`}>
                      {regime.vix_level != null ? regime.vix_level.toFixed(1) : "—"}
                    </p>
                  </div>
                  <div className="rounded-lg bg-zinc-800/50 p-2">
                    <p className="text-[10px] text-zinc-500 uppercase tracking-wide">SPY vs 200MA</p>
                    <p className={`font-semibold tabular-nums ${pnlColor(regime.spy_vs_200ma)}`}>
                      {regime.spy_vs_200ma != null ? fmtPct(regime.spy_vs_200ma) : "—"}
                    </p>
                  </div>
                  <div className="rounded-lg bg-zinc-800/50 p-2">
                    <p className="text-[10px] text-zinc-500 uppercase tracking-wide">Yield Spread</p>
                    <p className={`font-semibold tabular-nums ${pnlColor(regime.yield_spread)}`}>
                      {regime.yield_spread != null ? regime.yield_spread.toFixed(2) + "%" : "—"}
                    </p>
                  </div>
                  <div className="rounded-lg bg-zinc-800/50 p-2">
                    <p className="text-[10px] text-zinc-500 uppercase tracking-wide">Fear &amp; Greed</p>
                    <p
                      className={`font-semibold tabular-nums ${
                        (regime.fear_greed ?? 50) < 25
                          ? "text-red-400"
                          : (regime.fear_greed ?? 50) > 75
                          ? "text-emerald-400"
                          : "text-zinc-200"
                      }`}
                    >
                      {regime.fear_greed ?? "—"}
                      <span className="ml-1 text-[10px] text-zinc-500">/ 100</span>
                    </p>
                  </div>
                </div>
                {regime.date && (
                  <p className="text-[10px] text-zinc-600">Updated {fmtDate(regime.date)}</p>
                )}
              </div>
            ) : (
              <EmptyState message="No regime data — run a scan first" />
            )}
          </Card>

          {/* FIRE Signals */}
          <Card className="lg:col-span-2">
            <SectionHeader title="Conviction Board" count={fireSignals.length + watchSignals.length} />
            {signals.length === 0 ? (
              <EmptyState message="No signals yet — run a scan to populate" />
            ) : (
              <div className="overflow-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-zinc-800 text-[10px] uppercase tracking-wider text-zinc-600">
                      <th className="pb-2 text-left">Ticker</th>
                      <th className="pb-2 text-left">Action</th>
                      <th className="pb-2 text-left">Conviction</th>
                      <th className="pb-2 text-left hidden sm:table-cell">Regime</th>
                      <th className="pb-2 text-left hidden md:table-cell">Signals</th>
                      <th className="pb-2 text-right">Time</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-zinc-800/50">
                    {[...fireSignals, ...watchSignals].map((s) => (
                      <tr key={s.id} className="hover:bg-zinc-800/30 transition-colors">
                        <td className="py-2 pr-4 font-semibold">{s.ticker}</td>
                        <td className="py-2 pr-4">
                          <ActionBadge action={s.above_threshold ? "FIRE" : "WATCH"} />
                        </td>
                        <td className="py-2 pr-4">
                          <ConvictionBar score={s.score} />
                        </td>
                        <td className="py-2 pr-4 hidden sm:table-cell">
                          <span className={`text-xs capitalize ${REGIME_STYLES[s.regime]?.color ?? "text-zinc-400"}`}>
                            {s.regime ?? "—"}
                          </span>
                        </td>
                        <td className="py-2 pr-4 hidden md:table-cell">
                          <span className="text-xs text-zinc-500">
                            {s.signals_fired?.length ?? 0} signals
                          </span>
                        </td>
                        <td className="py-2 text-right text-xs text-zinc-500">
                          {fmtDate(s.timestamp)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </div>

        {/* Open Positions */}
        <Card>
          <SectionHeader title="Open Positions" count={positions.length} />
          {positions.length === 0 ? (
            <EmptyState message="No open positions" />
          ) : (
            <div className="overflow-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-zinc-800 text-[10px] uppercase tracking-wider text-zinc-600">
                    <th className="pb-2 text-left">Ticker</th>
                    <th className="pb-2 text-left">Dir</th>
                    <th className="pb-2 text-right">Shares</th>
                    <th className="pb-2 text-right">Entry</th>
                    <th className="pb-2 text-right">Current</th>
                    <th className="pb-2 text-right">Stop</th>
                    <th className="pb-2 text-right">P&L $</th>
                    <th className="pb-2 text-right">P&L %</th>
                    <th className="pb-2 text-right hidden md:table-cell">Entered</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-zinc-800/50">
                  {positions.map((p) => (
                    <tr key={p.id} className="hover:bg-zinc-800/30 transition-colors">
                      <td className="py-2 pr-4 font-semibold">{p.ticker}</td>
                      <td className="py-2 pr-4">
                        <span
                          className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase ${
                            p.direction === "long"
                              ? "bg-emerald-400/10 text-emerald-400"
                              : "bg-red-400/10 text-red-400"
                          }`}
                        >
                          {p.direction}
                        </span>
                      </td>
                      <td className="py-2 pr-4 text-right tabular-nums">{fmt(p.shares, 0)}</td>
                      <td className="py-2 pr-4 text-right tabular-nums">{fmtMoney(p.entry_price)}</td>
                      <td className="py-2 pr-4 text-right tabular-nums">{fmtMoney(p.current_price)}</td>
                      <td className="py-2 pr-4 text-right tabular-nums text-zinc-500">{fmtMoney(p.stop_loss_price)}</td>
                      <td className={`py-2 pr-4 text-right tabular-nums font-medium ${pnlColor(p.unrealized_pnl)}`}>
                        {fmtMoney(p.unrealized_pnl)}
                      </td>
                      <td className={`py-2 pr-4 text-right tabular-nums font-medium ${pnlColor(p.unrealized_pct)}`}>
                        {fmtPct(p.unrealized_pct)}
                      </td>
                      <td className="py-2 text-right text-xs text-zinc-500 hidden md:table-cell">
                        {fmtDate(p.entry_time)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        {/* Recent Trades */}
        <Card>
          <div className="mb-4 flex items-center justify-between">
            <SectionHeader
              title="Recent Trades"
              count={tradesData?.summary?.total}
            />
            {tradesData && (
              <div className="flex gap-4 text-xs text-zinc-500">
                <span>
                  Win rate:{" "}
                  <span className={winRate != null ? (winRate >= 50 ? "text-emerald-400" : "text-red-400") : ""}>
                    {winRate != null ? `${winRate}%` : "—"}
                  </span>
                </span>
                <span>
                  Total P&L:{" "}
                  <span className={pnlColor(totalPnl)}>{fmtMoney(totalPnl)}</span>
                </span>
              </div>
            )}
          </div>
          {recentTrades.length === 0 ? (
            <EmptyState message="No trades yet" />
          ) : (
            <div className="overflow-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-zinc-800 text-[10px] uppercase tracking-wider text-zinc-600">
                    <th className="pb-2 text-left">Ticker</th>
                    <th className="pb-2 text-left">Dir</th>
                    <th className="pb-2 text-right">Entry</th>
                    <th className="pb-2 text-right">Exit</th>
                    <th className="pb-2 text-right">P&L $</th>
                    <th className="pb-2 text-right">P&L %</th>
                    <th className="pb-2 text-left hidden sm:table-cell">Exit Reason</th>
                    <th className="pb-2 text-right hidden md:table-cell">Conviction</th>
                    <th className="pb-2 text-right hidden lg:table-cell">Date</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-zinc-800/50">
                  {recentTrades.map((t) => (
                    <tr key={t.id} className="hover:bg-zinc-800/30 transition-colors">
                      <td className="py-2 pr-4 font-semibold">{t.ticker}</td>
                      <td className="py-2 pr-4">
                        <span
                          className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase ${
                            t.direction === "long"
                              ? "bg-emerald-400/10 text-emerald-400"
                              : "bg-red-400/10 text-red-400"
                          }`}
                        >
                          {t.direction}
                        </span>
                      </td>
                      <td className="py-2 pr-4 text-right tabular-nums">{fmtMoney(t.entry_price)}</td>
                      <td className="py-2 pr-4 text-right tabular-nums text-zinc-400">
                        {t.exit_price != null ? fmtMoney(t.exit_price) : <span className="text-zinc-600">open</span>}
                      </td>
                      <td className={`py-2 pr-4 text-right tabular-nums font-medium ${pnlColor(t.pnl)}`}>
                        {t.pnl != null ? fmtMoney(t.pnl) : "—"}
                      </td>
                      <td className={`py-2 pr-4 text-right tabular-nums font-medium ${pnlColor(t.pnl_pct)}`}>
                        {t.pnl_pct != null ? fmtPct(t.pnl_pct) : "—"}
                      </td>
                      <td className="py-2 pr-4 hidden sm:table-cell">
                        <span className="text-xs capitalize text-zinc-500">
                          {t.exit_reason?.replace(/_/g, " ") ?? "—"}
                        </span>
                      </td>
                      <td className="py-2 pr-4 text-right hidden md:table-cell">
                        <ConvictionBar score={t.conviction_score ?? 0} />
                      </td>
                      <td className="py-2 text-right text-xs text-zinc-500 hidden lg:table-cell">
                        {fmtDate(t.entry_time)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        {/* Footer */}
        <footer className="pb-6 text-center text-xs text-zinc-700">
          ATLAS · Paper trading mode · Data updates every 60s ·{" "}
          {status?.stats?.total_signals != null && `${status.stats.total_signals.toLocaleString()} signals recorded`}
        </footer>
      </main>
    </div>
  );
}
