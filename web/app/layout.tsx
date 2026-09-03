import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "レポートの突き合わせ | ibkralgoTrade",
  description:
    "バックテストのレポートJSONを2つ読み込み、result_digest を計算し直して照合し、" +
    "一致しない場合は入力・パラメータ・結果のどこが動いたかを示す。",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ja">
      <body>{children}</body>
    </html>
  );
}
