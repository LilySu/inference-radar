import './globals.css';
import Link from 'next/link';

export const metadata = {
  title: 'Inference Radar',
  description:
    'Daily ingest + classifier + brief for vllm, sglang, Megatron-LM, and TensorRT-LLM.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen">
        <header className="border-b border-border bg-panel/60 backdrop-blur sticky top-0 z-10">
          <div className="mx-auto max-w-6xl px-6 py-4 flex items-center justify-between">
            <Link href="/" className="font-display text-xl font-bold gradient-title">
              Inference Radar
            </Link>
            <nav className="flex items-center gap-6 text-sm">
              <Link href="/firsts/" className="hover:text-accent-sky">Firsts</Link>
              <Link href="/briefings/" className="hover:text-accent-sky">Briefings</Link>
              <a
                href="https://github.com/LilySu/inference-radar"
                className="hover:text-accent-sky"
                target="_blank" rel="noreferrer"
              >
                GitHub
              </a>
            </nav>
          </div>
        </header>
        <main className="mx-auto max-w-6xl px-6 py-10">{children}</main>
        <footer className="border-t border-border mt-16">
          <div className="mx-auto max-w-6xl px-6 py-6 text-xs text-accent-slate">
            Built nightly · ingest → classify → brief · {new Date().getUTCFullYear()}
          </div>
        </footer>
      </body>
    </html>
  );
}
