const bridge = window.AstrBotPluginPage;
const errorNode = document.getElementById("bridge-error");
if (!bridge) errorNode.hidden = false;

const featureLabels = {
  silence: "沉默判断",
  chunking: "智能分段",
  image_intent: "图片意图",
  interrupt: "插话中断",
  steering: "插话引导",
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
// 首屏先看 4 个比率：它们比绝对计数更能说明“这轮对话被怎么处理”。
const ratioKpis = [
  { label: "沉默率", keys: ["silenced"], hint: "沉默 / 总请求" },
  { label: "分段率", keys: ["chunked"], hint: "分段 / 总请求" },
  { label: "插话合并率", keys: ["interrupted"], hint: "插话合并 / 总请求" },
  { label: "上下文拦截率", keys: ["intercepted", "air_guarded", "scene_guarded"], hint: "拦截命中 / 总请求" },
];

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
  // 骨架必须与 render() 的最终结构同序（关键比率 → 各统计组），避免加载完成后整块重排。
  document.getElementById("stats").innerHTML = `<section class="stat-group ratio-group"><div class="stat-group-head"><h2>关键比率</h2><span>…</span></div><div class="metric-grid ratio-grid">${metric.repeat(4)}</div></section>`
    + statGroups.map((group) => `<section class="stat-group"><div class="stat-group-head"><h2>${group.title}</h2><span>…</span></div><div class="metric-grid">${metric.repeat(group.keys.length)}</div></section>`).join("");
  document.getElementById("features").innerHTML = feature.repeat(6);
  document.getElementById("config-summary").innerHTML = feature.repeat(4);
}

function ratioValue(stats, keys) {
  const total = Number(stats.total_requests || 0);
  if (!total) return "—";
  const hit = keys.reduce((sum, key) => sum + Number(stats[key] || 0), 0);
  return `${((hit / total) * 100).toFixed(1)}%`;
}

function renderRatioKpis(stats) {
  return `
    <section class="stat-group ratio-group">
      <div class="stat-group-head"><h2>关键比率</h2><span>相对总请求 ${Number(stats.total_requests || 0)} 条</span></div>
      <div class="metric-grid ratio-grid">
        ${ratioKpis.map((item) => `<article class="metric ratio-metric"><span>${item.label}</span><strong>${ratioValue(stats, item.keys)}</strong><small>${item.hint}</small></article>`).join("")}
      </div>
    </section>
  `;
}

function render(data) {
  const stats = data.stats || {};
  document.getElementById("stats").innerHTML = renderRatioKpis(stats) + statGroups.map((group) => `
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
  const booleanLabels = {
    silence_enabled: "沉默判断",
    chunking_enabled: "智能分段",
    interrupt_enabled: "插话中断",
    group_context_enabled: "群聊上下文",
  };
  const entries = [
    ["silence_enabled", booleanLabels.silence_enabled],
    ["silence_strategy", "沉默策略"],
    ["chunking_enabled", booleanLabels.chunking_enabled],
    ["chunking_min_length", "分段最小长度"],
    ["interrupt_enabled", booleanLabels.interrupt_enabled],
    ["interrupt_mode", "插话模式"],
    ["interrupt_scope", "插话作用域"],
    ["group_context_enabled", booleanLabels.group_context_enabled],
  ];
  // 配置枚举值一律转成中文功能名，界面上不再出现 inject / steering / sender 这类内部值。
  const configValueLabels = {
    silence_strategy: { inject: "指令注入", prejudge: "独立预判", both: "两者结合" },
    interrupt_mode: { steering: "运行中插话归属", window: "固定时间窗" },
    interrupt_scope: { room: "本群任何新消息", sender: "仅同一发送者", mention_or_sender: "同一发送者或 @Bot" },
  };
  const formatConfigValue = (key, value) => {
    if (Object.prototype.hasOwnProperty.call(booleanLabels, key)) {
      return value === true ? "开启" : value === false ? "关闭" : "—";
    }
    if (value === null || value === undefined || value === "") return "—";
    const mapped = configValueLabels[key] && configValueLabels[key][value];
    return escapeHtml(mapped || value);
  };
  host.innerHTML = entries
    .map(([key, label]) => `<div class="feature"><span>${label}</span><strong>${formatConfigValue(key, config[key])}</strong></div>`)
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
    // 已有旧数据时用内联横幅说明（stale），不再叠加 toast；首次失败才用 toast 提升可见性。
    if (!lastUpdated && window.SeriesUI && typeof window.SeriesUI.toast === "function") {
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
