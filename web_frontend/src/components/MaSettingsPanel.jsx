import { useEffect, useState } from "react";
import { updateMaSettings } from "../api/tradingApi";
function formatTime(value) {
  const text = String(value || "");
  return text.length === 14 ? `${text.slice(0,4)}-${text.slice(4,6)}-${text.slice(6,8)} ${text.slice(8,10)}:${text.slice(10,12)}:${text.slice(12,14)}` : text || "-";
}
export default function MaSettingsPanel({ name, status, onSaved }) {
  const [period, setPeriod] = useState("5");
  const [ratio, setRatio] = useState("0.97");
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");

  useEffect(() => {
    if (!dirty && status?.saved) {
      setPeriod(String(status.saved.ma_period));
      setRatio(String(status.saved.exit_ratio));
    }
  }, [status?.saved?.revision, dirty]);

  const save = async (event) => {
    event.preventDefault();
    setMessage("");
    const maPeriod = Number(period);
    const exitRatio = Number(ratio);
    if (!Number.isInteger(maPeriod) || maPeriod < 2 || maPeriod > 60 ||
        !Number.isFinite(exitRatio) || exitRatio < 0.8 || exitRatio >= 1) {
      setMessage("MA 기간은 2~60의 정수, 기준 비율은 0.80 이상 1.00 미만으로 입력하세요.");
      return;
    }
    setSaving(true);
    try {
      const result = await updateMaSettings(name, {
        ma_period: maPeriod,
        exit_ratio: exitRatio,
        expected_revision: status.saved.revision,
      });
      setDirty(false);
      onSaved(result);
      setMessage("저장했습니다. 저장일 이후 첫 거래일에 비교 엔진이 적용합니다.");
    } catch (error) {
      setMessage(error.message);
    } finally {
      setSaving(false);
    }
  };

  const active = status?.applied;
  const saved = status?.saved;
  return (
    <section className="panel ma-settings-panel">
      <div className="panel-title"><h2>A/B 비교 계좌 MA 수익 청산 설정</h2><span>{status?.pending ? "적용 대기" : "저장 / 적용 현황"}</span></div>
      <p>A/B의 수익 중인 보유 종목은 15:19~15:20에 MA 기준선을 검사합니다. ATR 청산은 기존대로 계속 작동합니다.</p>
      <div className="ma-settings-status">
        <div><span>비교 엔진 적용 확인</span><strong>{active ? `v${active.revision} · MA${active.ma_period} × ${Number(active.exit_ratio).toFixed(3)}` : "미확인 (기본값 MA5 × 0.970)"}</strong><small>{active ? `적용 확인: ${formatTime(active.applied_at)}` : "비교 엔진 실행 후 확인됩니다"}</small></div>
        <div><span>저장한 설정</span><strong>{saved ? `v${saved.revision} · MA${saved.ma_period} × ${Number(saved.exit_ratio).toFixed(3)}` : "불러오는 중"}</strong><small>{saved?.saved_at ? `저장: ${formatTime(saved.saved_at)}` : "기본값"}</small></div>
      </div>
      {status?.pending && <p className="ma-settings-pending">저장된 새 설정은 저장일 이후 첫 거래일에 적용됩니다. 비교 엔진이 적용하면 위 적용 확인 값이 바뀝니다.</p>}
      <form className="ma-settings-form" onSubmit={save}>
        <label>이동평균 기간 (거래일)<input type="number" min="2" max="60" step="1" value={period} onChange={(e) => { setPeriod(e.target.value); setDirty(true); }} /></label>
        <label>청산 기준 비율<input type="number" min="0.8" max="0.999" step="0.001" value={ratio} onChange={(e) => { setRatio(e.target.value); setDirty(true); }} /></label>
        <button type="submit" disabled={!saved || saving || !dirty}>{saving ? "저장 중…" : "다음 거래일 설정 저장"}</button>
      </form>
      <p className="panel-subtitle">예: MA10 × 0.95면 기준선 아래로 내려온 수익 포지션의 잔량을 시장가로 청산합니다.</p>
      {message && <p role="status" className="ma-settings-feedback">{message}</p>}
    </section>
  );
}
