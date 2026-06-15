import { getBriefings } from '@/lib/data';

export default function BriefingsPage() {
  const briefings = getBriefings();
  return (
    <div className="space-y-8">
      <div>
        <div className="eyebrow">video archive</div>
        <h1 className="font-display text-4xl gradient-title">Briefings</h1>
        <p className="text-accent-slate text-sm mt-3 max-w-2xl">
          Daily 3–5 minute briefs: LLM-written news script → Marp slides → Piper
          TTS → ffmpeg → YouTube (unlisted).
        </p>
      </div>

      {briefings.length === 0 ? (
        <p className="text-accent-slate">No briefings yet.</p>
      ) : (
        <ul className="space-y-4">
          {briefings.map((b) => (
            <li key={b.briefing_date} className="panel p-5">
              <div className="flex items-baseline justify-between mb-2">
                <div className="font-display text-xl text-white">
                  {b.script?.title || `Inference Radar — ${b.briefing_date}`}
                </div>
                <span className="text-xs text-accent-slate">{b.briefing_date}</span>
              </div>
              <p className="text-accent-slate text-sm mb-3">{b.script?.intro}</p>
              <div className="flex gap-4 text-sm">
                {b.video_url ? (
                  <a
                    href={b.video_url}
                    target="_blank" rel="noreferrer"
                    className="text-accent-sky hover:underline"
                  >
                    YouTube ↗
                  </a>
                ) : b.video_path ? (
                  <span className="text-accent-slate">local mp4 (not uploaded)</span>
                ) : (
                  <span className="text-accent-amber">script only</span>
                )}
                {b.duration_s && (
                  <span className="text-accent-slate">{b.duration_s}s</span>
                )}
                <span className="text-accent-slate">
                  {(b.script?.slides?.length ?? 0)} slides
                </span>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
