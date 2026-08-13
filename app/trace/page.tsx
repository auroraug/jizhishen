"use client";

import { useEffect, useMemo, useState } from "react";
import "./trace.css";
import "./trace-extra.css";

const STAGE_LABELS: Record<string, string> = {
  document_classification: "资料分类",
  general_extraction: "通用字段抽取",
  payment_extraction: "付款台账抽取",
  change_extraction: "变更签证抽取",
  contract_extraction: "合同条款抽取",
  semantic_consistency: "语义一致性",
  policy_retrieval: "政策查询与重排",
  text2sql: "Text2SQL",
  result_summary: "结果摘要",
  ocr: "MinerU OCR",
  documents: "文档读取",
  document_parse: "文档解析",
  mineru_submit: "MinerU 批量提交",
  mineru_download_normalize: "MinerU 产物下载与统一规范化",
  visual_general: "通用视觉元素分析",
  visual_table_or_chart: "图表视觉复核",
  visual_seal_signature: "印章与签名分析",
  visual_form_checkbox: "表单、勾选与手写分析",
  deterministic_parser: "确定性解析",
  fact_persistence: "事实固化",
  deterministic_rules: "SQL/Python 规则",
};
const ALL_STAGES = [
  "document_classification",
  "general_extraction",
  "payment_extraction",
  "change_extraction",
  "contract_extraction",
  "semantic_consistency",
  "policy_retrieval",
  "text2sql",
  "result_summary",
  "ocr",
  "visual_general",
  "visual_table_or_chart",
  "visual_seal_signature",
  "visual_form_checkbox",
];
const DERIVED_STAGES = [
  "document_classification",
  "general_extraction",
  "payment_extraction",
  "change_extraction",
  "contract_extraction",
  "semantic_consistency",
  "policy_retrieval",
  "text2sql",
  "result_summary",
];
const pretty = (value: any) => JSON.stringify(value, null, 2);

export default function TraceConsole() {
  const [tab, setTab] = useState("trace");
  const [projects, setProjects] = useState<any[]>([]);
  const [projectId, setProjectId] = useState("");
  const [runs, setRuns] = useState<any[]>([]);
  const [runId, setRunId] = useState<number | undefined>();
  const [trace, setTrace] = useState<any>(null);
  const [spanId, setSpanId] = useState<number | undefined>();
  const [toast, setToast] = useState("");
  const [providerConfig, setProviderConfig] = useState<any>(null);
  const [replayProvider, setReplayProvider] = useState("");
  const [replayModel, setReplayModel] = useState("");
  async function loadProjects() {
    const d = await fetch("/api/trace/projects").then((r) => r.json());
    setProjects(d.items || []);
    if (
      !projectId &&
      !new URLSearchParams(location.search).get("run") &&
      d.items?.length
    )
      setProjectId(d.items[0].id);
  }
  async function loadRuns(id = projectId) {
    if (!id) return;
    const d = await fetch(`/api/trace/projects/${id}/runs`).then((r) =>
      r.json(),
    );
    setRuns(d.items || []);
    if (d.items?.length && !d.items.some((x: any) => x.id === runId))
      setRunId(d.items[0].id);
    if (!d.items?.length) {
      setRunId(undefined);
      setTrace(null);
    }
  }
  async function loadTrace(id = runId) {
    if (!id) return;
    const r = await fetch(`/api/trace/runs/${id}`);
    if (!r.ok) return;
    const d = await r.json();
    setTrace(d);
    if (d.spans?.length && !d.spans.some((x: any) => x.id === spanId))
      setSpanId(d.spans[0].id);
  }
  useEffect(() => {
    if (location.hash === "#models") setTab("models");
    else if (location.hash === "#prompts") setTab("prompts");
    const requested = Number(new URLSearchParams(location.search).get("run"));
    if (requested)
      fetch(`/api/trace/runs/${requested}`)
        .then((r) => r.json())
        .then((d) => {
          setRunId(requested);
          setProjectId(d.run.project_id);
          setTrace(d);
        })
        .catch(() => setToast("指定 Trace 不存在"));
    loadProjects().catch(() => setToast("追踪服务未连接"));
    fetch("/api/providers")
      .then((r) => r.json())
      .then(setProviderConfig)
      .catch(() => setProviderConfig(null));
  }, []);
  useEffect(() => {
    loadRuns();
  }, [projectId]);
  useEffect(() => {
    loadTrace();
  }, [runId]);
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(""), 2600);
    return () => clearTimeout(t);
  }, [toast]);
  async function removeRun() {
    if (!runId) return;
    if (
      !confirm(
        `永久删除 Run #${runId} 的完整 trace、事实和异常？\n派生运行保留，但父链接会标记为已删除。`,
      )
    )
      return;
    const r = await fetch(`/api/trace/runs/${runId}?confirm=run-${runId}`, {
      method: "DELETE",
    });
    if (!r.ok) {
      setToast("删除失败");
      return;
    }
    setToast(`Run #${runId} 已永久删除`);
    setTrace(null);
    await loadRuns();
  }
  async function replay(mode: string) {
    if (!runId || !span) return;
    const route_overrides = replayProvider
      ? { [span.stage]: { provider_id: replayProvider, model: replayModel } }
      : {};
    const r = await fetch(`/api/trace/runs/${runId}/replay`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stage: span.stage, mode, route_overrides }),
    });
    const d = await r.json();
    if (!r.ok) {
      setToast(d.detail || "重放失败");
      return;
    }
    setToast(
      `${mode === "probe" ? "单阶段测试" : "派生模型链"}已创建项目内新 Trace（技术 Run ID ${d.run_id}）`,
    );
    await loadRuns();
    setRunId(d.run_id);
  }
  const span = trace?.spans?.find((x: any) => x.id === spanId);
  const artifacts = useMemo(
    () =>
      Object.fromEntries((trace?.artifacts || []).map((x: any) => [x.id, x])),
    [trace],
  );
  const modelProviders = (providerConfig?.providers || []).filter(
    (p: any) => p.kind === "openai" && p.enabled,
  );
  const selectedReplayProvider = modelProviders.find(
    (p: any) => p.id === replayProvider,
  );
  const probeAllowed = !!span?.input_artifact_id && span?.kind === "model_call";
  const derivedAllowed =
    !!span &&
    DERIVED_STAGES.includes(span.stage) &&
    trace?.spans?.some(
      (s: any) =>
        s.stage === span.stage &&
        s.kind === "model_call" &&
        s.input_artifact_id,
    );
  useEffect(() => {
    if (!span || !providerConfig) return;
    const route = providerConfig.stage_routes?.[span.stage] || {};
    const provider =
      modelProviders.find(
        (p: any) => p.id === (span.provider_id || route.provider_id),
      ) || modelProviders[0];
    setReplayProvider(provider?.id || "");
    setReplayModel(span.model || route.model || provider?.models?.[0] || "");
  }, [spanId, span?.stage, providerConfig]);
  return (
    <div className="trace-console">
      <header className="trace-top">
        <a href="/">← 返回审查工作台</a>
        <div>
          <b>开发者调试控制台</b>
          <small>完整原始输入输出永久留存 · 密钥字段自动脱敏</small>
        </div>
        <nav>
          <button
            className={tab === "trace" ? "active" : ""}
            onClick={() => setTab("trace")}
          >
            运行 Trace
          </button>
          <button
            className={tab === "models" ? "active" : ""}
            onClick={() => setTab("models")}
          >
            模型配置
          </button>
          <button
            className={tab === "prompts" ? "active" : ""}
            onClick={() => setTab("prompts")}
          >
            Prompt 版本
          </button>
        </nav>
        <span className="unsafe">无访问保护 · 仅限可信内网</span>
      </header>
      {tab === "trace" ? (
        <div className="trace-layout">
          <aside className="trace-projects">
            <h2>项目</h2>
            {projects.map((p) => (
              <button
                className={projectId === p.id ? "active" : ""}
                key={p.id}
                onClick={() => setProjectId(p.id)}
              >
                <b>{p.name}</b>
                <small>
                  {p.id} · {p.run_count} 次运行 {p.is_demo ? "· 示例" : ""}
                </small>
              </button>
            ))}
            {!projects.length && <p>暂无项目</p>}
          </aside>
          <aside className="trace-runs">
            <h2>运行</h2>
            {runs.map((r) => (
              <button
                className={runId === r.id ? "active" : ""}
                key={r.id}
                onClick={() => setRunId(r.id)}
              >
                <span>
                  <b>项目内第 {r.project_run_no} 次</b>
                  <em className={r.status}>{r.status}</em>
                </span>
                <small>
                  技术 Run ID {r.id} · {r.run_kind} · {r.started_at}
                </small>
                <i>
                  {r.parent_run_id ? `源 Run ID ${r.parent_run_id} · ` : ""}
                  {r.fact_count} facts / {r.anomaly_count} findings
                </i>
              </button>
            ))}
            {!runs.length && <p>该项目还没有运行记录</p>}
          </aside>
          <main className="trace-main">
            {trace ? (
              <>
                <section className="trace-run-head">
                  <div>
                    <p>
                      {trace.run.project_id} / 项目内第{" "}
                      {trace.run.project_run_no} 次 Trace / 技术 Run ID{" "}
                      {trace.run.id}
                    </p>
                    <h1>
                      {trace.run.status} · {trace.run.current_stage}
                    </h1>
                    <small>
                      Prompt
                      和本次生效模型配置已固化；历史运行不受后续配置修改影响。
                    </small>
                  </div>
                  <div>
                    <a href={`/api/trace/runs/${runId}/export`} target="_blank">
                      导出完整 JSON
                    </a>
                    <a href={`/api/trace/runs/${runId}/bundle`} target="_blank">
                      JSON + 原件 ZIP
                    </a>
                    <button className="danger" onClick={removeRun}>
                      永久删除
                    </button>
                  </div>
                </section>
                <section className="trace-summary">
                  <span>
                    <small>类型</small>
                    <b>{trace.run.run_kind}</b>
                  </span>
                  <span>
                    <small>模型路由</small>
                    <b>{trace.run.provider || "—"}</b>
                  </span>
                  <span>
                    <small>事实</small>
                    <b>{trace.run.fact_count}</b>
                  </span>
                  <span>
                    <small>异常</small>
                    <b>{trace.run.anomaly_count}</b>
                  </span>
                </section>
                <div className="span-workspace">
                  <section className="span-list">
                    <h2>执行顺序</h2>
                    {trace.spans.map((s: any) => (
                      <button
                        key={s.id}
                        className={spanId === s.id ? "active" : ""}
                        style={{
                          paddingLeft: `${16 + (s.parent_span_id ? 16 : 0)}px`,
                        }}
                        onClick={() => setSpanId(s.id)}
                      >
                        <i className={s.status}></i>
                        <span>
                          <b>{STAGE_LABELS[s.stage] || s.stage}</b>
                          <small>
                            {s.kind} · {s.name}
                          </small>
                        </span>
                        <time>
                          {s.duration_ms == null ? "—" : `${s.duration_ms} ms`}
                        </time>
                      </button>
                    ))}
                  </section>
                  <section className="span-detail">
                    {span ? (
                      <>
                        <header>
                          <div>
                            <small>
                              {span.kind} / Span #{span.id}
                            </small>
                            <h2>{STAGE_LABELS[span.stage] || span.stage}</h2>
                            <p>
                              {span.provider_id || "未调用模型"}{" "}
                              {span.model ? `· ${span.model}` : ""} ·{" "}
                              {span.status}
                            </p>
                          </div>
                        </header>
                        {(probeAllowed || derivedAllowed) && (
                          <section className="replay-panel">
                            <div>
                              <label>
                                本次实验使用模型
                                <select
                                  value={replayProvider}
                                  onChange={(e) => {
                                    const p = modelProviders.find(
                                      (x: any) => x.id === e.target.value,
                                    );
                                    setReplayProvider(e.target.value);
                                    setReplayModel(p?.models?.[0] || "");
                                  }}
                                >
                                  {modelProviders.map((p: any) => (
                                    <option key={p.id} value={p.id}>
                                      {p.name}
                                    </option>
                                  ))}
                                </select>
                              </label>
                              <label>
                                Model ID
                                <select
                                  value={replayModel}
                                  onChange={(e) =>
                                    setReplayModel(e.target.value)
                                  }
                                >
                                  {(selectedReplayProvider?.models || []).map(
                                    (m: string) => (
                                      <option key={m} value={m}>
                                        {m}
                                      </option>
                                    ),
                                  )}
                                </select>
                              </label>
                            </div>
                            <footer>
                              <button
                                disabled={
                                  !probeAllowed ||
                                  !replayProvider ||
                                  !replayModel
                                }
                                onClick={() => replay("probe")}
                              >
                                仅测试此阶段
                              </button>
                              <button
                                disabled={
                                  !derivedAllowed ||
                                  !replayProvider ||
                                  !replayModel
                                }
                                onClick={() => replay("derived")}
                              >
                                从此阶段派生模型链
                              </button>
                            </footer>
                            <p>
                              <b>单阶段测试</b>只重放该阶段的首个模型调用；
                              <b>派生模型链</b>
                              复用上游快照并重跑此阶段及下游模型调用。两者都创建新
                              Trace，均不修改项目事实和异常。
                            </p>
                          </section>
                        )}
                        {!probeAllowed && !derivedAllowed && (
                          <p className="replay-unavailable">
                            这是确定性、OCR
                            或容器环节，没有可直接重放的模型输入；请选中其下方的
                            model_call。视觉模型调用可做单阶段测试，但暂不进入派生链。
                          </p>
                        )}
                        <Artifact
                          title="完整输入"
                          item={artifacts[span.input_artifact_id]}
                        />
                        <Artifact
                          title="完整输出"
                          item={artifacts[span.output_artifact_id]}
                        />
                        <Artifact
                          title="错误"
                          item={artifacts[span.error_artifact_id]}
                        />
                        <Artifact
                          title="Span 元数据"
                          item={{
                            content: span.metadata_json,
                            size_bytes: pretty(span.metadata_json).length,
                            stored: "inline",
                          }}
                        />
                      </>
                    ) : (
                      <div className="trace-empty">
                        选择一个执行环节查看原始输入输出
                      </div>
                    )}
                  </section>
                </div>
              </>
            ) : (
              <div className="trace-empty">选择一个有运行记录的项目</div>
            )}
          </main>
        </div>
      ) : tab === "models" ? (
        <ModelConfig notify={setToast} />
      ) : (
        <PromptManager runs={runs} runId={runId} notify={setToast} />
      )}{" "}
      {toast && <div className="trace-toast">{toast}</div>}
    </div>
  );
}

function Artifact({ title, item }: { title: string; item: any }) {
  const [open, setOpen] = useState(
    title === "完整输入" || title === "完整输出",
  );
  if (!item)
    return (
      <section className="artifact empty">
        <header>
          <b>{title}</b>
          <span>本环节未产生</span>
        </header>
      </section>
    );
  return (
    <section className="artifact">
      <header onClick={() => setOpen(!open)}>
        <b>{title}</b>
        <span>
          {item.size_bytes || 0} bytes · {item.stored || "inline"}{" "}
          {open ? "▾" : "▸"}
        </span>
      </header>
      {open && <pre>{pretty(item.content)}</pre>}
    </section>
  );
}

function ModelConfig({ notify }: { notify: (x: string) => void }) {
  const [config, setConfig] = useState<any>(null);
  const [saving, setSaving] = useState(false);
  const [draft, setDraft] = useState<any>(null);
  const [testing, setTesting] = useState(false);
  const [tested, setTested] = useState(false);
  const [templates, setTemplates] = useState<any[] | null>(null);
  async function load() {
    setConfig(await fetch("/api/providers").then((r) => r.json()));
  }
  useEffect(() => {
    load();
  }, []);
  function updateProvider(i: number, key: string, value: any) {
    const next = structuredClone(config);
    next.providers[i][key] = value;
    setConfig(next);
  }
  function add(kind = "openai") {
    setDraft(
      kind === "mineru-local"
        ? {
            id: `mineru_${Date.now()}`,
            name: "MinerU 本地 OCR（VLM）",
            kind: "mineru-local",
            base_url: "http://host.docker.internal:8000",
            api_key: "",
            model_id: "vlm-engine",
            enabled: true,
          }
        : {
            id: `model_${Date.now()}`,
            name: "",
            kind: "openai",
            base_url: "",
            api_key: "",
            model_id: "",
            enabled: true,
          },
    );
    setTested(false);
  }
  function remove(i: number) {
    const p = config.providers[i];
    if (
      Object.values(config.stage_routes).some(
        (r: any) => r.provider_id === p.id,
      )
    ) {
      notify("该供应商仍被阶段路由使用，请先切换路由");
      return;
    }
    const next = structuredClone(config);
    next.providers.splice(i, 1);
    setConfig(next);
  }
  async function save() {
    setSaving(true);
    const r = await fetch("/api/providers", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        schema_version: 1,
        providers: config.providers,
        stage_routes: config.stage_routes,
      }),
    });
    const d = await r.json();
    setSaving(false);
    if (!r.ok) {
      notify(d.detail || "保存失败");
      return;
    }
    setConfig({ ...d.config, items: d.config.providers });
    notify("模型配置已原子写入后端配置文件");
  }
  async function discover(i: number) {
    const p = config.providers[i];
    const r = await fetch(`/api/providers/${p.id}/models`);
    const d = await r.json();
    if (!r.ok) {
      notify(d.detail || "发现失败");
      return;
    }
    const next = structuredClone(config);
    next.providers[i].models = d.items;
    Object.values(next.stage_routes).forEach((route: any) => {
      if (
        route.provider_id === p.id &&
        d.items.length &&
        (!route.model ||
          d.raw_items?.includes(route.model) ||
          String(route.model).toLowerCase().endsWith(".gguf"))
      )
        route.model = d.items[0];
    });
    setSaving(true);
    const saved = await fetch("/api/providers", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        schema_version: 1,
        providers: next.providers,
        stage_routes: next.stage_routes,
      }),
    });
    const result = await saved.json();
    setSaving(false);
    if (!saved.ok) {
      notify(result.detail || "发现结果保存失败");
      return;
    }
    setConfig({ ...result.config, items: result.config.providers });
    notify(
      `已发现并保存 ${d.items.length} 个 Model ID${d.normalized ? "；服务返回的 GGUF 路径已转换为稳定短名称" : ""}`,
    );
  }
  async function test(p: any, isDraft = false) {
    setTesting(true);
    const payload = isDraft
      ? {
          provider_id: p.id,
          provider: { ...p, models: p.model_id ? [p.model_id] : [] },
        }
      : { provider_id: p.id };
    const d = await fetch("/api/providers/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then((r) => r.json());
    setTesting(false);
    if (isDraft) setTested(!!d.ok);
    notify(
      `${p.name || "新模型"}：${d.ok ? `连接正常${d.latency_ms ? `（${d.latency_ms}ms）` : ""}` : d.message}`,
    );
  }
  async function saveDraft() {
    if (!tested) return notify("请先测试连接");
    const next = structuredClone(config);
    next.providers.push({
      ...draft,
      models: [draft.model_id],
      model_id: undefined,
      source: "user",
    });
    setConfig(next);
    setDraft(null);
    setTested(false);
    setSaving(true);
    const r = await fetch("/api/providers", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        schema_version: 1,
        providers: next.providers,
        stage_routes: next.stage_routes,
      }),
    });
    const d = await r.json();
    setSaving(false);
    if (!r.ok) return notify(d.detail || "保存失败");
    setConfig({ ...d.config, items: d.config.providers });
    notify("模型连接已保存，下次可直接按显示名称选择");
  }
  async function showTemplates() {
    const r = await fetch("/api/providers/route-templates");
    const d = await r.json();
    if (!r.ok) return notify(d.detail || "历史参考模板读取失败");
    setTemplates(d.items || []);
  }
  function applyTemplate(t: any) {
    if (!t.applicable)
      return notify(
        `无法套用：当前缺少连接 ${t.missing_provider_ids.join("、")}`,
      );
    setConfig({ ...config, stage_routes: structuredClone(t.stage_routes) });
    setTemplates(null);
    notify(
      `已载入技术 Run ID ${t.source_run_id} 的历史路由；检查后请点击“保存路由修改”`,
    );
  }
  if (!config)
    return (
      <div className="config-page">
        <p>读取配置中…</p>
      </div>
    );
  const templateDialog = templates && (
    <div className="model-dialog-backdrop">
      <section className="route-template-dialog">
        <header>
          <div>
            <small>IMMUTABLE AUDIT SNAPSHOTS</small>
            <h2>历史智能审查参考模板</h2>
            <p>
              来自过去已完成正式审查当次固化的真实模型路由。套用只进入编辑区，不会自动保存。
            </p>
          </div>
          <button onClick={() => setTemplates(null)}>×</button>
        </header>
        <div className="route-template-list">
          {templates.map((t) => (
            <article key={t.id}>
              <header>
                <div>
                  <b>{t.project_name}</b>
                  <small>
                    {t.project_id} · 项目内第 {t.project_run_no} 次 ·{" "}
                    {t.started_at} · 技术 Run ID {t.source_run_id}
                  </small>
                </div>
                <button
                  disabled={!t.applicable}
                  onClick={() => applyTemplate(t)}
                >
                  {t.applicable ? "套用到编辑区" : "缺少历史连接"}
                </button>
              </header>
              <div>
                {ALL_STAGES.map((stage) => {
                  const route = t.stage_routes[stage] || {};
                  return (
                    <span key={stage}>
                      <b>{STAGE_LABELS[stage]}</b>
                      <small>
                        {t.provider_names[route.provider_id] ||
                          route.provider_id ||
                          "—"}{" "}
                        · {route.model || "—"}
                      </small>
                    </span>
                  );
                })}
              </div>
              {!t.applicable && (
                <p>
                  当前缺少：{t.missing_provider_ids.join("、")}。恢复同 ID
                  模型连接后才能无损套用。
                </p>
              )}
            </article>
          ))}
          {!templates.length && (
            <div className="trace-empty">尚无已完成正式审查的路由快照</div>
          )}
        </div>
        <footer>
          <span>载入后请逐环节核对，再点击“保存路由修改”。</span>
          <button onClick={() => setTemplates(null)}>关闭</button>
        </footer>
      </section>
    </div>
  );
  return (
    <main className="config-page">
      {templateDialog}
      <header>
        <div>
          <h1>模型与阶段路由</h1>
          <p>
            先添加并测试连接，成功后保存为可复用模型；运行时按显示名称选择，实际请求使用
            Model ID。
          </p>
        </div>
        <button onClick={showTemplates}>▣ 历史参考模板</button>
        <button onClick={() => add("openai")}>＋ 添加 OpenAI 兼容模型</button>
        <button onClick={() => add("mineru-local")}>＋ 添加本地 MinerU</button>
        <button className="primary" disabled={saving} onClick={save}>
          {saving ? "保存中…" : "保存路由修改"}
        </button>
      </header>
      {draft && (
        <div className="model-dialog-backdrop">
          <section className="model-add-flow model-dialog">
            <header>
              <div>
                <small>NEW MODEL CONNECTION</small>
                <h2>添加可复用模型连接</h2>
                <p>
                  完成连接信息、在线验证和保存后，才会出现在各阶段模型选择器中。
                </p>
              </div>
              <button
                onClick={() => setDraft(null)}
                aria-label="关闭添加模型表单"
              >
                ×
              </button>
            </header>
            <nav>
              <span className="active">1 填写连接</span>
              <i></i>
              <span className={tested ? "done" : ""}>2 测试连接</span>
              <i></i>
              <span className={tested ? "active" : ""}>3 保存添加</span>
            </nav>
            <div className="model-form-grid">
              <label>
                <span>
                  显示名称 <b>必填</b>
                </span>
                <small>只用于页面识别，不会发送给模型服务。</small>
                <input
                  autoFocus
                  value={draft.name}
                  onChange={(e) => {
                    setDraft({ ...draft, name: e.target.value });
                    setTested(false);
                  }}
                  placeholder="例如：本地 Qwen3.5 9B"
                />
              </label>
              <label>
                <span>
                  连接类型 <b>必填</b>
                </span>
                <small>LLM 使用 OpenAI-compatible；OCR 仅支持 MinerU。</small>
                <select
                  value={draft.kind}
                  onChange={(e) => {
                    setDraft({ ...draft, kind: e.target.value });
                    setTested(false);
                  }}
                >
                  <option value="openai">OpenAI-compatible LLM</option>
                  <option value="mineru">MinerU 云端 OCR</option>
                  <option value="mineru-local">MinerU 本地 OCR（VLM）</option>
                </select>
              </label>
              <label className="wide">
                <span>
                  Base URL <b>必填</b>
                </span>
                <small>
                  填写完整 API 根地址；例如 llama.cpp 服务通常为
                  http://host:8051/v1。
                </small>
                <input
                  value={draft.base_url}
                  onChange={(e) => {
                    setDraft({ ...draft, base_url: e.target.value });
                    setTested(false);
                  }}
                  placeholder="http://host:port/v1"
                />
              </label>
              <label>
                <span>
                  Model ID <b>必填</b>
                </span>
                <small>
                  API 请求 model 字段；使用稳定短名称，不填写服务器 GGUF 路径。
                </small>
                <input
                  value={draft.model_id}
                  onChange={(e) => {
                    setDraft({ ...draft, model_id: e.target.value });
                    setTested(false);
                  }}
                  placeholder="Qwen3.5-9B-Q4_K_M"
                />
              </label>
              <label>
                <span>
                  API Key <em>可留空</em>
                </span>
                <small>局域网无鉴权模型可留空；云端模型按供应商填写。</small>
                <input
                  type="password"
                  value={draft.api_key}
                  onChange={(e) => {
                    setDraft({ ...draft, api_key: e.target.value });
                    setTested(false);
                  }}
                  placeholder="sk-…"
                />
              </label>
            </div>
            <aside
              className={
                tested ? "connection-state success" : "connection-state"
              }
            >
              <i>{tested ? "✓" : "!"}</i>
              <span>
                <b>{tested ? "连接测试已通过" : "尚未完成连接测试"}</b>
                <small>
                  {tested
                    ? "配置可以保存；后续可在阶段路由中直接选择。"
                    : "任何字段修改后都需要重新测试，未测试配置不能保存。"}
                </small>
              </span>
            </aside>
            <footer>
              <button onClick={() => setDraft(null)}>取消</button>
              <button
                disabled={
                  testing || !draft.name || !draft.base_url || !draft.model_id
                }
                onClick={() => test(draft, true)}
              >
                {testing ? "正在验证服务…" : "测试连接"}
              </button>
              <button
                className="primary"
                disabled={!tested || saving}
                onClick={saveDraft}
              >
                {saving ? "正在保存…" : "保存并添加模型"}
              </button>
            </footer>
          </section>
        </div>
      )}
      <div className="provider-editor">
        {config.providers.map((p: any, i: number) => (
          <section key={p.id}>
            <div>
              <label>
                连接 ID
                <input value={p.id} readOnly />
              </label>
              <label>
                显示名称
                <input
                  value={p.name}
                  onChange={(e) => updateProvider(i, "name", e.target.value)}
                />
              </label>
              <label>
                类型
                <select
                  value={p.kind}
                  onChange={(e) => updateProvider(i, "kind", e.target.value)}
                >
                  <option value="openai">OpenAI-compatible LLM</option>
                  <option value="mineru">MinerU 云端 OCR</option>
                  <option value="mineru-local">MinerU 本地 OCR（VLM）</option>
                </select>
              </label>
            </div>
            <label>
              Base URL
              <input
                value={p.base_url || ""}
                onChange={(e) => updateProvider(i, "base_url", e.target.value)}
                placeholder="https://host/v1"
              />
            </label>
            <label>
              API Key
              <input
                type="password"
                value={p.api_key || ""}
                onChange={(e) => updateProvider(i, "api_key", e.target.value)}
                placeholder={
                  p.api_key_masked
                    ? `已保存 ${p.api_key_masked}；留空保持不变`
                    : "输入密钥"
                }
              />
            </label>
            <footer>
              <label>
                <input
                  type="checkbox"
                  checked={!!p.enabled}
                  onChange={(e) =>
                    updateProvider(i, "enabled", e.target.checked)
                  }
                />
                启用
              </label>
              <button onClick={() => test(p)}>测试已保存连接</button>
              {p.kind === "openai" && (
                <button disabled={saving} onClick={() => discover(i)}>
                  读取并保存 Model ID
                </button>
              )}
              <button className="danger" onClick={() => remove(i)}>
                删除
              </button>
              <span>Model ID：{(p.models || []).join("、") || "尚未配置"}</span>
            </footer>
          </section>
        ))}
      </div>
      <section className="route-editor">
        <h2>审查与视觉解析环节的全局默认模型</h2>
        {ALL_STAGES.map((stage) => {
          const route = config.stage_routes[stage] || {};
          const providers = config.providers.filter((p: any) =>
            stage === "ocr" ? p.kind.startsWith("mineru") : p.kind === "openai",
          );
          const selected = config.providers.find(
            (p: any) => p.id === route.provider_id,
          );
          return (
            <div key={stage}>
              <b>{STAGE_LABELS[stage]}</b>
              <select
                value={route.provider_id || ""}
                onChange={(e) => {
                  const picked = config.providers.find(
                    (p: any) => p.id === e.target.value,
                  );
                  setConfig({
                    ...config,
                    stage_routes: {
                      ...config.stage_routes,
                      [stage]: {
                        provider_id: e.target.value,
                        model: picked?.models?.[0] || "",
                      },
                    },
                  });
                }}
              >
                {providers.map((p: any) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
              <select
                value={route.model || ""}
                onChange={(e) =>
                  setConfig({
                    ...config,
                    stage_routes: {
                      ...config.stage_routes,
                      [stage]: { ...route, model: e.target.value },
                    },
                  })
                }
              >
                {(selected?.models || []).map((m: string) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </div>
          );
        })}
      </section>
    </main>
  );
}

function PromptManager({
  runs,
  runId,
  notify,
}: {
  runs: any[];
  runId?: number;
  notify: (x: string) => void;
}) {
  const [items, setItems] = useState<any[]>([]);
  const [selected, setSelected] = useState<any>(null);
  const [schemaText, setSchemaText] = useState("");
  const groups = useMemo(
    () =>
      items.reduce((acc: any, item: any) => {
        (acc[item.stage] ??= []).push(item);
        return acc;
      }, {}),
    [items],
  );
  function choose(item: any) {
    setSelected(item);
    setSchemaText(item ? pretty(item.json_schema) : "");
  }
  async function load(selectId?: number) {
    const d = await fetch("/api/prompts").then((r) => r.json());
    setItems(d.items || []);
    choose(
      (d.items || []).find((x: any) => x.id === (selectId || selected?.id)) ||
        d.items?.[0] ||
        null,
    );
  }
  useEffect(() => {
    load();
  }, []);
  async function clone() {
    if (!selected) return;
    const d = await fetch("/api/prompts/drafts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stage: selected.stage, based_on_id: selected.id }),
    }).then((r) => r.json());
    await load(d.id);
    notify(`已创建 ${selected.stage} v${d.version} 草稿`);
  }
  async function save() {
    let schema;
    try {
      schema = JSON.parse(schemaText);
    } catch {
      notify("JSON Schema 格式无效，请修正后再保存");
      return;
    }
    const r = await fetch(`/api/prompts/${selected.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        system_prompt: selected.system_prompt,
        user_prompt: selected.user_prompt,
        json_schema: schema,
        parser_version: selected.parser_version,
      }),
    });
    const d = await r.json();
    if (!r.ok) {
      notify(d.detail);
      return;
    }
    await load(d.id);
    notify("草稿已保存");
  }
  async function publishNow() {
    const r = await fetch(`/api/prompts/${selected.id}/publish`, {
      method: "POST",
    });
    const d = await r.json();
    if (!r.ok) {
      notify(d.detail);
      return;
    }
    await load(d.id);
    notify(`v${d.version} 已发布并成为当前版本`);
  }
  async function test() {
    if (!runId || !selected)
      return notify("请先在“运行 Trace”中选择一个含该阶段输入的历史运行");
    const r = await fetch(`/api/trace/runs/${runId}/replay`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        stage: selected.stage,
        mode: "probe",
        prompt_id: selected.id,
      }),
    });
    const d = await r.json();
    notify(
      r.ok
        ? `Prompt v${selected.version} 测试已保存为 Run #${d.run_id}`
        : d.detail || "测试失败",
    );
  }
  return (
    <main className="prompt-page">
      <aside>
        {Object.entries(groups).map(([stage, versions]: any) => (
          <section key={stage}>
            <h3>{STAGE_LABELS[stage] || stage}</h3>
            {versions.map((p: any) => (
              <button
                className={selected?.id === p.id ? "active" : ""}
                key={p.id}
                onClick={() => choose(p)}
              >
                v{p.version}
                <span className={p.status}>
                  {p.status}
                  {p.is_current ? " · 当前" : ""}
                </span>
              </button>
            ))}
          </section>
        ))}
      </aside>
      {selected ? (
        <section className="prompt-editor">
          <header>
            <div>
              <small>
                {STAGE_LABELS[selected.stage]} / v{selected.version}
              </small>
              <h1>
                {selected.status}
                {selected.is_current ? " · 当前发布版本" : ""}
              </h1>
              <p>
                发布版本不可修改；运行时会把完整 Prompt、Schema 和
                parser_version 固化。
              </p>
            </div>
            <button onClick={clone}>复制为新草稿</button>
            <button onClick={test}>用历史输入测试</button>
            {selected.status === "draft" && (
              <button className="primary" onClick={publishNow}>
                发布版本
              </button>
            )}
          </header>
          <label>
            System Prompt
            <textarea
              readOnly={selected.status !== "draft"}
              value={selected.system_prompt}
              onChange={(e) =>
                setSelected({ ...selected, system_prompt: e.target.value })
              }
            />
          </label>
          <label>
            User Prompt 模板
            <textarea
              readOnly={selected.status !== "draft"}
              value={selected.user_prompt}
              onChange={(e) =>
                setSelected({ ...selected, user_prompt: e.target.value })
              }
            />
          </label>
          <label>
            Structured Output JSON Schema
            <textarea
              readOnly={selected.status !== "draft"}
              value={schemaText}
              onChange={(e) => setSchemaText(e.target.value)}
            />
          </label>
          <label>
            Parser 版本
            <input
              readOnly={selected.status !== "draft"}
              value={selected.parser_version}
              onChange={(e) =>
                setSelected({ ...selected, parser_version: e.target.value })
              }
            />
          </label>
          {selected.status === "draft" && (
            <button className="primary save-prompt" onClick={save}>
              保存草稿
            </button>
          )}
        </section>
      ) : (
        <div className="trace-empty">暂无 Prompt</div>
      )}
    </main>
  );
}
