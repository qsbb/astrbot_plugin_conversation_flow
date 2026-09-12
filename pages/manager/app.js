const bridge = window.AstrBotPluginPage;
const errorNode = document.getElementById("bridge-error");
if (!bridge) errorNode.hidden = false;

const featureLabels = {
  silence: "沉默判断",
  chunking: "智能分段",
  image_intent: "图片意图",
  interrupt: "插话中断",
  steering: "steering",
  group_context: "群聊上下文"
};
const statLabels = {
  total_requests: "总请求",
  silenced: "沉默",
  chunked: "分段",
  interrupted: "插话",
  time_annotated_injections: "时间标注"
};

function render(data) {
  const stats = data.stats || {};
  document.getElementById("stats").innerHTML = Object.entries(statLabels)
    .map(([key, label]) => `<article class="metric"><span>${label}</span><strong>${Number(stats[key] || 0)}</strong></article>`)
    .join("");
  const features = data.features || {};
  document.getElementById("features").innerHTML = Object.entries(featureLabels)
    .map(([key, label]) => `<div class="feature"><span>${label}</span><strong>${features[key] ? "开启" : "关闭"}</strong></div>`)
    .join("");
}

async function load() {
  if (!bridge) return;
  try {
    render(await bridge.apiGet("status"));
  } catch (error) {
    errorNode.textContent = `状态读取失败：${error.message || error}`;
    errorNode.hidden = false;
  }
}

document.getElementById("refresh")?.addEventListener("click", load);
load();
