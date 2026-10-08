const SOURCES = {
  ValueQualityStrategy: {
    url: "https://www.youtube.com/watch?v=SskCOZ0yi9g",
    label: "전략 영상",
    title: "유튜브에서 전략 설명 영상 보기",
    video: true,
  },
  MomentumContractionStrategy: {
    url: "https://qullamaggie.com/my-3-timeless-setups-that-have-made-me-tens-of-millions/",
    label: "전략 원문",
    title: "Qullamaggie 전략 설명 원문 보기 (새 탭)",
    video: false,
  },
};

export default function StrategySourceLink({ name }) {
  const source = SOURCES[name];
  if (!source) return null;
  return (
    <a className="strategy-video-link" href={source.url} target="_blank"
      rel="noopener noreferrer" title={source.title} aria-label={source.title}>
      <svg viewBox="0 0 24 24" aria-hidden="true">
        {source.video ? (
          <path d="M21.6 7.2a2.9 2.9 0 0 0-2-2C17.8 4.7 12 4.7 12 4.7s-5.8 0-7.6.5a2.9 2.9 0 0 0-2 2A30 30 0 0 0 2 12a30 30 0 0 0 .4 4.8 2.9 2.9 0 0 0 2 2c1.8.5 7.6.5 7.6.5s5.8 0 7.6-.5a2.9 2.9 0 0 0 2-2A30 30 0 0 0 22 12a30 30 0 0 0-.4-4.8ZM10 15.2V8.8l5.5 3.2-5.5 3.2Z" />
        ) : (
          <path d="M14 3h7v7h-2V6.41l-9.29 9.3-1.42-1.42L17.59 5H14V3ZM5 5h6v2H5v12h12v-6h2v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2Z" />
        )}
      </svg>
      <span>{source.label}</span>
    </a>
  );
}
