import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // 既定の実行環境は Node。digest に使う crypto.subtle は Node 18以降に
    // 標準で入っているので、ブラウザを立ち上げずに同じ計算を検証できる。
    environment: "node",
    include: ["lib/**/*.test.ts"],
  },
});
