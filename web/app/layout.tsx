import type { Metadata } from "next";

import "./globals.css";

/**
 * OG画像と canonical には絶対URLが要る。**basePath は含めない**——
 * `opengraph-image.png` のようなファイル規約側が既に basePath を前置するので、
 * ここに入れるとリポジトリ名が二重になる（`/ibkralgoTrade/ibkralgoTrade/...`）。
 */
const SITE_ORIGIN = "https://atsushi196323.github.io";

const DESCRIPTION =
  "バックテストのレポートJSONを2つ読み込み、result_digest を計算し直して照合し、" +
  "一致しない場合は入力・パラメータ・結果のどこが動いたかを示す。";

export const metadata: Metadata = {
  metadataBase: new URL(SITE_ORIGIN),
  title: "レポートの突き合わせ | ibkralgoTrade",
  description: DESCRIPTION,
  // **貼られたときに何の画面か分かるようにする。** OG画像が無いと、
  // SlackやSNSに貼っても白いカードにURLが並ぶだけで、開く理由が伝わらない。
  openGraph: {
    type: "website",
    locale: "ja_JP",
    siteName: "ibkralgoTrade",
    title: "レポートの突き合わせ | ibkralgoTrade",
    description: DESCRIPTION,
  },
  twitter: { card: "summary_large_image" },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ja">
      <body>
        {/* 公開した画面は単体で開かれる。**どのプロジェクトの一部で、
            本体がどこにあるのか**をここで示さないと、読み手は戻る先を持たない。 */}
        <header className="topbar">
          <span className="topbar-title">
            <strong>ibkralgoTrade</strong> <span aria-hidden="true">/</span> レポートの突き合わせ
          </span>
          <a href="https://github.com/atsushi196323/ibkralgoTrade">GitHub リポジトリ</a>
        </header>
        {children}
      </body>
    </html>
  );
}
