import type { NextConfig } from "next";

/**
 * **このアプリはサーバに何も保存しない。** レポートの読み取り・digest の
 * 計算・差分の抽出はすべてブラウザの中で完結する（`crypto.subtle` を使う）。
 *
 * 口座の記録（`logs/`）を扱う画面ではないが、レポートには検証したファイルの
 * パスなど手元の情報が入りうる。**アップロード先を持たない**ことが、
 * その情報がどこへも出ていかないことの一番簡単な保証になる。
 */
// GitHub Pages のプロジェクトページは `/<リポジトリ名>/` の下に置かれるため、
// アセットの参照を丸ごとそこへ寄せる必要がある。**環境変数で渡す。**
// 設定ファイルに直接書くと、手元の `npm run dev`（`/` で配信される）が
// 404 だらけになり、開発と公開でどちらかが必ず壊れる。
const basePath = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // 静的書き出し。動かすのに実行中のサーバもデータベースも要らない。
  output: "export",
  images: { unoptimized: true },
  basePath,
  // 末尾スラッシュ付きで書き出す。Pages のような静的ホスティングでは
  // `/path` と `/path/` の解決がホスト側の実装しだいなので、
  // ディレクトリ + index.html の形に寄せておく方が事故が少ない。
  trailingSlash: true,
};

export default nextConfig;
