import { useEffect, useState } from "react";
import { getDashboard } from "../api/tradingApi";
import StrategyCard from "../components/StrategyCard";

export default function DashboardPage() {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    const load = async () => {
      try {
        const result = await getDashboard();
        if (active) {
          setData(result);
          setError("");
        }
      } catch (e) {
        if (active) setError(e.message);
      }
    };
    load();
    const timer = setInterval(load, 5000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, []);

  return (
    <main className="page-shell">
      <header className="hero">
        <div>
          <span className="eyebrow">SYSTEMTRADING</span>
          <h1>전략 이벤트 모니터링</h1>
          <p>계좌 자산과 무관하게, 전략 조건을 충족한 모든 가상 체결을 추적합니다.</p>
        </div>
        <div className="live-badge"><i /> 5초 자동 갱신</div>
      </header>

      {error && <div className="error-box">{error}</div>}

      <section className="strategy-grid">
        {(data?.strategies || []).map((strategy) => (
          <StrategyCard key={strategy.strategy_name} strategy={strategy} />
        ))}
      </section>

      <footer className="footnote">
        가상 체결가는 신호가 검출된 실시간 현재가를 사용합니다. 실제 주문 성공 여부와는 별도입니다.
      </footer>
    </main>
  );
}
