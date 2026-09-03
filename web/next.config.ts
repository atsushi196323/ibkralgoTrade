import type { NextConfig } from "next";

/**
 * **このアプリはサーバに何も保存しない。** レポートの読み取り・digest の
 * 計算・差分の抽出はすべてブラウザの中で完結する（`crypto.subtle` を使う）。
 *
 * 口座の記録（`logs/`）を扱う画面ではないが、レポートには検証したファイルの
 * パスなど手元の情報が入りうる。**アップロード先を持たない**ことが、
 * その情報がどこへも出ていかないことの一番簡単な保証になる。
 */
const nextConfig: NextConfig = {
  reactStrictMode: true,
  // 静的書き出し。動かすのに実行中のサーバもデータベースも要らない。
  output: "export",
  images: { unoptimized: true },
};

export default nextConfig;
