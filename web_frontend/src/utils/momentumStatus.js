const LABELS = {
  WAIT_MARKET_OPEN: "장 시작 대기",
  MARKET_CLOSED: "장 종료 · 다음 거래일 대기",
  NO_CANDIDATES: "후보 0개 · 신규 가상매수 대기",
  OPENING_RANGE_MISSING: "장 시작 구간 미관측 · 오늘 신규 진입 보류",
  WAIT_SIGNAL: "실시간 가상매매 감시 중",
  CANDIDATE_REFRESH_FAILED: "후보 갱신 실패 · 기존 가상 손절 관리 유지",
};

export function momentumStatus(execution, now = Date.now()) {
  if (!execution?.paper_enabled) return { text: "실행 상태 미수신 · 매매 프로그램과 웹 API의 가상 DB 경로를 확인하세요.", stale: false };
  const stamp = execution.heartbeat_at || "";
  const at = /^\d{14}$/.test(stamp) ? Date.parse(`${stamp.slice(0,4)}-${stamp.slice(4,6)}-${stamp.slice(6,8)}T${stamp.slice(8,10)}:${stamp.slice(10,12)}:${stamp.slice(12,14)}+09:00`) : NaN;
  const marketPhase = !["WAIT_MARKET_OPEN", "MARKET_CLOSED"].includes(execution.phase);
  const stale = marketPhase && (!Number.isFinite(at) || now - at > 30000);
  return { text: stale ? "상태 갱신 지연 · 매매 프로그램 실행과 가상 DB 경로를 확인하세요." : LABELS[execution.phase] || "가상계좌 상태 확인 중", stale };
}
