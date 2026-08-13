import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

async function render(path = "/") {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  return worker.fetch(
    new Request(`http://localhost${path}`, { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("server-renders the intelligent audit workspace", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);
  const html = await response.text();
  assert.match(html, /<title>集智审｜农村集体建设工程全过程智能审查<\/title>/);
  assert.match(html, /集体工程智能审查/);
  assert.match(html, /真实 AI 审查工作台/);
  assert.match(html, /当前真实启用文件上传适配器/);
  assert.match(html, /未配置连接器/);
  assert.match(html, /开发者 Trace/);
  assert.doesNotMatch(html, /村文化广场改造工程|模拟只读连接/);
  assert.doesNotMatch(html, /codex-preview|Your site is taking shape|react-loading-skeleton/);
});

test("server-renders the developer trace console", async () => {
  const response = await render("/trace");
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /开发者调试控制台/);
  assert.match(html, /运行 Trace/);
  assert.match(html, /模型配置/);
  assert.match(html, /Prompt 版本/);
  assert.match(html, /无访问保护/);
});

test("removes the disposable starter and keeps product metadata", async () => {
  const [page, layout, packageJson] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
  ]);
  assert.match(page, /EvidenceDrawerV2/);
  assert.match(page, /\/api\/projects/);
  assert.match(page, /\/api\/dashboard/);
  assert.match(page, /批量上传/);
  assert.match(layout, /集智审/);
  assert.doesNotMatch(packageJson, /react-loading-skeleton/);
  await assert.rejects(access(new URL("../app/_sites-preview", import.meta.url)));
});
