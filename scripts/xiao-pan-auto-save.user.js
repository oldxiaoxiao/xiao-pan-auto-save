// ==UserScript==
// @name         小盘自动转存 - 分享页一键追更
// @namespace    https://github.com/xiao-pan-auto-save
// @version      0.1.0
// @description  在夸克网盘分享页一键把链接添加进 xiao-pan-auto-save 追更任务（兼容旧版 /api/add_task 协议）
// @match        https://pan.quark.cn/s/*
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_xmlhttpRequest
// @connect      *
// @run-at       document-idle
// ==/UserScript==
(function () {
  "use strict";

  const cfg = {
    server: GM_getValue("xps_server", ""),
    token: GM_getValue("xps_token", ""),
    savepath: GM_getValue("xps_savepath", "/自动转存"),
    pattern: GM_getValue("xps_pattern", ""),
    replace: GM_getValue("xps_replace", ""),
    ignoreExt: GM_getValue("xps_ignore_ext", true),
  };
  const save = (k, v) => { cfg[k] = v; GM_setValue("xps_" + k.replace(/([A-Z])/g, "_$1").toLowerCase(), v); };

  const shareUrl = location.href.split("#")[0];
  const guessName = (document.title || "未命名任务").replace(/[-— 夸克网盘]+$/g, "").trim() || "未命名任务";

  const panel = document.createElement("div");
  panel.style.cssText = "position:fixed;right:20px;bottom:80px;z-index:99999;background:#fff;border:1px solid #e5e8ef;border-radius:12px;box-shadow:0 8px 28px rgba(31,35,41,.14);padding:14px;width:300px;font:13px/1.5 system-ui;color:#1f2329";
  panel.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
      <b style="font-size:14px">📥 添加到追更</b><span data-x="toggle" style="cursor:pointer;color:#8a919f">收起</span>
    </div>
    <div data-x="body">
      <input data-x="name" value="${esc(guessName)}" placeholder="任务名" style="width:100%;margin-bottom:6px">
      <input data-x="savepath" value="${esc(cfg.savepath)}" placeholder="保存路径，如 /动漫/某剧" style="width:100%;margin-bottom:6px">
      <details style="margin-bottom:6px"><summary style="cursor:pointer;color:#8a919f">正则与设置</summary>
        <input data-x="pattern" value="${esc(cfg.pattern)}" placeholder="匹配正则或 $魔法关键字" style="width:100%;margin:6px 0">
        <input data-x="replace" value="${esc(cfg.replace)}" placeholder="替换式（可含 {E} {II} 等）" style="width:100%;margin-bottom:6px">
        <label><input type="checkbox" data-x="ignoreExt" ${cfg.ignoreExt ? "checked" : ""}> 忽略扩展名</label>
      </details>
      <details style="margin-bottom:8px"><summary style="cursor:pointer;color:#8a919f">服务设置</summary>
        <input data-x="server" value="${esc(cfg.server)}" placeholder="http://127.0.0.1:8432" style="width:100%;margin:6px 0">
        <input data-x="token" value="${esc(cfg.token)}" placeholder="API Token" style="width:100%">
      </details>
      <button data-x="add" style="width:100%;padding:8px;border:0;border-radius:8px;background:#3b6cf0;color:#fff;cursor:pointer;font-weight:600">添加任务</button>
      <div data-x="msg" style="margin-top:8px;min-height:18px;color:#8a919f"></div>
    </div>`;
  document.body.appendChild(panel);

  const $ = (x) => panel.querySelector(`[data-x=${x}]`);
  let collapsed = false;
  $("toggle").onclick = () => { collapsed = !collapsed; $("body").style.display = collapsed ? "none" : ""; $("toggle").textContent = collapsed ? "展开" : "收起"; };

  $("add").onclick = async () => {
    const server = $("server").value.trim().replace(/\/$/, "");
    if (!server || !$("token").value.trim()) return msg("请先填写服务地址和 Token", true);
    save("server", server); save("token", $("token").value.trim());
    save("savepath", $("savepath").value.trim()); save("pattern", $("pattern").value.trim());
    save("replace", $("replace").value.trim()); save("ignoreExt", $("ignoreExt").checked);
    if (!server.startsWith("http")) return msg("服务地址需以 http 开头", true);
    const task = {
      taskname: $("name").value.trim() || guessName,
      shareurl: shareUrl,
      savepath: $("savepath").value.trim(),
      pattern: $("pattern").value.trim(),
      replace: $("replace").value.trim(),
      ignore_extension: $("ignoreExt").checked,
    };
    const res = await gmPost(`${server}/api/add_task?token=${encodeURIComponent($("token").value.trim())}`, task);
    if (res && res.success) msg(`✅ ${res.message}：《${task.taskname}》`, false);
    else msg("❌ " + ((res && res.message) || (res && res.detail) || "请求失败"), true);
  };

  function msg(text, isErr) { const m = $("msg"); m.textContent = text; m.style.color = isErr ? "#e5484d" : "#2f9e44"; }
  function esc(s) { return String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;"); }
  function gmPost(url, data) {
    return new Promise((resolve) => {
      GM_xmlhttpRequest({
        method: "POST", url, headers: { "content-type": "application/json" },
        data: JSON.stringify(data), timeout: 15000,
        onload: (r) => { try { resolve(JSON.parse(r.responseText)); } catch { resolve({ success: false, message: "HTTP " + r.status }); } },
        onerror: () => resolve({ success: false, message: "网络错误" }),
        ontimeout: () => resolve({ success: false, message: "超时" }),
      });
    });
  }
})();
