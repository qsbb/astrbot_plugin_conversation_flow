const bridge = window.AstrBotPluginPage;
const errorNode = document.getElementById("bridge-error");
if (!bridge) errorNode.hidden = false;

const featureLabels = {
  silence: "沉默判断",
  chunking: "智能分段",
  image_intent: "图片意图",
  interrupt: "插话中断",
  steering: "插话引导（steering）",
  group_context: "群聊上下文"
};
const statLabels = {
  total_requests: "总请求",
  silenced: "沉默",
  chunked: "分段",
  interrupted: "插话合并",
  time_annotated_injections: "时间标注",
  intercepted: "拦截命中",
  air_guarded: "读空气拦截",
  scene_guarded: "场景拦截",
  scene_hinted: "场景软提示",
  mood_silenced: "情绪静默",
  mood_hinted: "情绪软提示",
  private_context_bridged: "私聊承接",
  dynamic_context_injected: "动态续接",
  context_budget_shadow: "预算影子",
  context_budget_trimmed: "预算裁剪",
  recent_activity_recorded: "跨会话记录",
  recent_activity_selected: "跨会话选用"
};
const statGroups = [
  { title: "响应决策", keys: ["total_requests", "silenced", "intercepted", "air_guarded", "scene_guarded", "scene_hinted", "mood_silenced", "mood_hinted"] },
  { title: "输出节奏", keys: ["chunked", "interrupted", "time_annotated_injections"] },
  { title: "上下文预算", keys: ["context_budget_shadow", "context_budget_trimmed", "private_context_bridged", "dynamic_context_injected", "recent_activity_recorded", "recent_activity_selected"] }
];
let lastUpdated = null;

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#39;"
  })[ch]);
}

function formatUpdated(date) {
  return date ? date.toLocaleTimeString("zh-CN", { hour12: false }) : "";
}

function renderLoading({ replace = true } = {}) {
  const shell = document.querySelector(".shell");
  shell?.setAttribute("aria-busy", "true");
  if (!replace) return;
  const metric = '<article class="metric skeleton-card"><span></span><strong></strong></article>';
  const feature = '<div class="feature skeleton-card"><span></span><strong></strong></div>';
  document.getElementById("stats").innerHTML = `<section class="stat-group"><div class="stat-group-head"><h2>正在读取运行状态</h2><span>…</span></div><div class="metric-grid">${metric.repeat(6)}</div></section>`;
  document.getElementById("features").innerHTML = feature.repeat(6);
  document.getElementById("config-summary").innerHTML = feature.repeat(4);
}

function render(data) {
  const stats = data.stats || {};
  document.getElementById("stats").innerHTML = statGroups.map((group) => `
    <section class="stat-group">
      <div class="stat-group-head"><h2>${group.title}</h2><span>${group.keys.length} 项</span></div>
      <div class="metric-grid">
        ${group.keys.map((key) => `<article class="metric"><span>${statLabels[key] || key}</span><strong>${Number(stats[key] || 0)}</strong></article>`).join("")}
      </div>
    </section>
  `).join("");
  const features = data.features || {};
  document.getElementById("features").innerHTML = Object.entries(featureLabels)
    .map(([key, label]) => {
      const enabled = Boolean(features[key]);
      return `<div class="feature"><span>${label}</span><strong class="feature-state ${enabled ? "is-on" : "is-off"}">${enabled ? "开启" : "关闭"}</strong></div>`;
    })
    .join("");
  document.getElementById("plugin-meta").textContent = `版本 ${data.plugin?.version || "—"}`;
}

function renderConfig(data) {
  const host = document.getElementById("config-summary");
  if (!data) {
    host.innerHTML = '<p class="empty-state">配置摘要读取失败，请刷新重试。</p>';
    return;
  }
  const config = data.config || {};
  const entries = [
    ["silence_strategy", "沉默策略"],
    ["chunking_min_length", "分段最小长度"],
    ["interrupt_mode", "插话模式"],
    ["interrupt_scope", "插话作用域"],
  ];
  host.innerHTML = entries
    .map(([key, label]) => `<div class="feature"><span>${label}</span><strong>${escapeHtml(config[key] ?? "—")}</strong></div>`)
    .join("");
}

async function load() {
  if (!bridge) {
    document.querySelector(".shell")?.setAttribute("aria-busy", "false");
    if (!lastUpdated) {
      document.getElementById("stats").innerHTML = '<p class="empty-state">页面通信组件未加载，运行状态暂不可用。</p>';
      document.getElementById("features").innerHTML = '<p class="empty-state">页面通信组件未加载，能力状态暂不可用。</p>';
      document.getElementById("config-summary").innerHTML = '<p class="empty-state">页面通信组件未加载，配置摘要暂不可用。</p>';
    }
    return;
  }
  const firstLoad = lastUpdated === null;
  renderLoading({ replace: firstLoad });
  const button = document.getElementById("refresh");
  if (button) {
    button.disabled = true;
    button.textContent = "刷新中…";
  }
  try {
    const [statusResult, configResult] = await Promise.allSettled([
      bridge.apiGet("status"),
      bridge.apiGet("config"),
    ]);
    if (statusResult.status !== "fulfilled") throw statusResult.reason;
    const data = statusResult.value;
    render(data);
    renderConfig(configResult.status === "fulfilled" ? configResult.value : null);
    lastUpdated = new Date();
    document.getElementById("stale-notice").hidden = true;
    const meta = document.getElementById("plugin-meta");
    meta.textContent = `版本 ${data.plugin?.version || "—"} · 更新于 ${formatUpdated(lastUpdated)}`;
  } catch (error) {
    const message = `状态读取失败：${error.message || error}`;
    const stale = document.getElementById("stale-notice");
    if (lastUpdated) {
      stale.textContent = `${message}；当前显示的是 ${formatUpdated(lastUpdated)} 的旧数据`;
      stale.hidden = false;
    } else {
      errorNode.textContent = message;
      errorNode.hidden = false;
      document.getElementById("stats").innerHTML = '<p class="empty-state">运行状态暂不可用，请刷新重试。</p>';
      document.getElementById("features").innerHTML = '<p class="empty-state">能力状态暂不可用，请刷新重试。</p>';
      document.getElementById("config-summary").innerHTML = '<p class="empty-state">配置摘要暂不可用，请刷新重试。</p>';
    }
    if (window.SeriesUI && typeof window.SeriesUI.toast === "function") {
      window.SeriesUI.toast(message, "error");
    }
  } finally {
    document.querySelector(".shell")?.setAttribute("aria-busy", "false");
    if (button) {
      button.disabled = false;
      button.textContent = "刷新";
    }
  }
}

document.getElementById("refresh")?.addEventListener("click", load);
renderLoading();
load();
