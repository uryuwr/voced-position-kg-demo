/*!
 * 能力测评组件：步骤引导式的 简历解析推断 → 对话问答测评 → 综合能力报告。
 *
 * 步骤条固定三节点，**状态完全由服务端 graph 给的 `stages` 驱动**
 * （pending 灰 / active 高亮 / done 打勾），前端不自己推断进度。
 *
 * 题目是分批懒加载的：服务端 `question_end=false` 表示后面还会有新题，
 * 组件把收到的题压进内存队列、一次只展示一道，答完再弹下一道。
 *
 * SSE 用 fetch + ReadableStream，不能用 EventSource——UC 的 MAC 签名必须放
 * Authorization 头，而 EventSource 不支持自定义 header。
 *
 * 用法：
 *   AssessmentWidget.mount(document.getElementById("box"), {
 *     getOccupationId: () => "CN:occupation:...",   // 可选，缺省用后端的活跃目标
 *     onReport: (report) => {},                     // 可选，报告生成后回调
 *   });
 */
(function (global) {
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  const STEP_DEFS = [
    { key: "parse", name: "简历解析推断", ico: "📄" },
    { key: "assess", name: "对话问答测评", ico: "💬" },
    { key: "report", name: "综合能力报告", ico: "🏆" },
  ];

  const SAMPLE = "从事汽车维修5年，负责整车故障诊断与发动机检修，熟练使用示波器和解码器定位"
    + "间歇性电路故障，主导过变速箱大修，季度返修率从8%降到3%，带过2名学徒。";

  const CSS = `
  .aw{--aw-brand:#10b981;--aw-brand2:#34d399;--aw-purple:#818cf8;--aw-warn:#f59e0b;
      --aw-panel:#111a2b;--aw-panel2:#0b1220;--aw-line:#1e2b45;--aw-muted:#8fa3c0;color:#e6edf7}
  .aw *{box-sizing:border-box}
  .aw .steps{display:flex;gap:10px;background:var(--aw-panel);border:1px solid var(--aw-line);
             border-radius:14px;padding:10px;margin-bottom:14px}
  .aw .step{flex:1;display:flex;align-items:center;justify-content:center;gap:8px;padding:10px 8px;
            border-radius:10px;color:var(--aw-muted);font-weight:600;font-size:13px;transition:.2s}
  .aw .step.active{background:var(--aw-brand);color:#04231a}
  .aw .step.done{background:rgba(16,185,129,.12);color:var(--aw-brand2);border:1px solid rgba(16,185,129,.35)}
  .aw .card2{background:var(--aw-panel);border:1px solid var(--aw-line);border-radius:14px;padding:18px}
  .aw .tag2{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;
            border:1px solid var(--aw-brand);color:var(--aw-brand2);background:rgba(16,185,129,.1)}
  .aw h3{margin:10px 0 6px;font-size:18px}
  .aw .m{color:var(--aw-muted)}
  .aw .btn2{background:var(--aw-brand);color:#04231a;border:0;border-radius:10px;
            padding:9px 16px;font-weight:700;cursor:pointer;font-size:13px}
  .aw .btn2:disabled{opacity:.45;cursor:not-allowed}
  .aw .btn2.g{background:transparent;color:#e6edf7;border:1px solid var(--aw-line)}
  .aw textarea,.aw select{background:var(--aw-panel2);color:#e6edf7;border:1px solid var(--aw-line);
            border-radius:10px;padding:10px 12px;width:100%;font:inherit}
  .aw .drop2{border:1px dashed var(--aw-line);border-radius:12px;padding:22px;text-align:center;
             color:var(--aw-muted);cursor:pointer;margin-top:10px}
  .aw .drop2:hover{border-color:var(--aw-brand)}
  .aw .drop2.over{border-color:var(--aw-brand);background:rgba(16,185,129,.08);color:#e6edf7}
  .aw .qbox{background:var(--aw-panel2);border:1px solid var(--aw-line);border-radius:12px;padding:16px;margin-top:12px}
  .aw .opt2{display:block;width:100%;text-align:left;background:var(--aw-panel);color:#e6edf7;
            border:1px solid var(--aw-line);border-radius:10px;padding:11px 14px;margin-top:8px;cursor:pointer}
  .aw .opt2:hover{border-color:var(--aw-brand)}
  .aw .opt2.sel{border-color:var(--aw-brand);background:rgba(16,185,129,.12)}
  .aw .badge2{display:inline-block;background:rgba(129,140,248,.15);color:var(--aw-purple);
              border-radius:6px;padding:1px 8px;font-size:12px;margin-left:6px}
  .aw .g2{display:grid;grid-template-columns:1.1fr 1fr;gap:14px}
  @media(max-width:820px){.aw .g2{grid-template-columns:1fr}}
  .aw .box2{border:1px solid var(--aw-line);border-radius:12px;padding:14px}
  .aw .box2.good{border-color:rgba(16,185,129,.4);background:rgba(16,185,129,.06)}
  .aw .box2.bad{border-color:rgba(245,158,11,.4);background:rgba(245,158,11,.06)}
  .aw .chip2{display:inline-block;margin:6px 6px 0 0;padding:5px 11px;border-radius:8px;font-size:12px}
  .aw .chip2.good{background:rgba(16,185,129,.14);color:var(--aw-brand2)}
  .aw .chip2.bad{background:rgba(245,158,11,.14);color:var(--aw-warn)}
  .aw .score2{font-size:32px;font-weight:800;color:var(--aw-brand2);line-height:1.1}
  .aw .lg{display:flex;gap:16px;justify-content:center;font-size:12px;margin-top:6px;color:var(--aw-muted)}
  .aw .dot2{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px}
  .aw .awlog{font-size:12px;color:var(--aw-muted);min-height:18px;margin-top:10px}
  .aw .sp{display:inline-block;width:11px;height:11px;border:2px solid var(--aw-line);
          border-top-color:var(--aw-brand);border-radius:50%;animation:awr .8s linear infinite;margin-right:6px}
  @keyframes awr{to{transform:rotate(360deg)}}`;

  function injectCss() {
    if (document.getElementById("aw-css")) return;
    const s = document.createElement("style");
    s.id = "aw-css"; s.textContent = CSS;
    document.head.appendChild(s);
  }

  function mount(root, opts) {
    opts = opts || {};
    injectCss();
    const api = global.VocedApi;
    root.classList.add("aw");
    root.innerHTML = `<div class="steps" data-steps></div><div data-body></div><div class="awlog" data-log></div>`;
    const elSteps = root.querySelector("[data-steps]");
    const elBody = root.querySelector("[data-body]");
    const elLog = root.querySelector("[data-log]");

    const S = {
      sessionId: null, stageMap: {}, queue: [], current: null, total: 0,
      questionEnd: false, report: null, busy: false, seen: new Set(), answered: 0,
      fatal: null,
    };

    const log = (m, spin) => { elLog.innerHTML = m ? `${spin ? '<span class="sp"></span>' : ""}${esc(m)}` : ""; };

    /* 步骤条状态完全来自服务端 stages，前端不自己算进度 */
    function renderSteps() {
      const byKey = S.stageMap || {};
      elSteps.innerHTML = STEP_DEFS.map((d, i) => {
        const st = byKey[d.key] || { status: i === 0 && !Object.keys(byKey).length ? "active" : "pending" };
        return `<div class="step ${st.status}">
          <span>${d.ico}</span>${i + 1}. ${d.name}${st.status === "done" ? " ✓" : ""}</div>`;
      }).join("");
    }

    /* ── 阶段 1 ── */
    function renderParse() {
      const occName = opts.occupationName ? `【${esc(opts.occupationName)}】` : "目标岗位";
      elBody.innerHTML = `
        <div class="card2">
          <span class="tag2">F-03 · 简历智能化能力推断</span>
          <h3>上传或粘贴个人简历，自动解析提取技能等级</h3>
          <div class="m">通过大模型结合知识图谱结构化提取，推断你在${occName}各维度能力的基准分。</div>
          <div style="text-align:right;margin-top:8px">
            <a href="javascript:void(0)" data-sample style="color:var(--aw-brand2)">✨ 使用标准范例简历一键体验解析</a>
          </div>
          <div class="drop2" data-drop>
            <div style="font-size:26px">⬆</div>
            <b>拖拽简历文件到此处，或点击浏览上传</b>
            <div style="font-size:12px;margin-top:4px">支持 PDF、DOCX、TXT，单文件不超过 20MB</div>
            <div data-fileinfo style="font-size:12px;margin-top:6px;color:var(--aw-brand2)"></div>
          </div>
          <input type="file" data-file accept=".pdf,.docx,.txt,.md" hidden />
          <textarea data-resume rows="5" placeholder="也可直接粘贴简历/自述正文…（留空则跳过解析，直接开始测评）"
                    style="margin-top:10px"></textarea>
          <div style="margin-top:12px;display:flex;gap:10px;flex-wrap:wrap">
            <button class="btn2" data-start>开始测评 →</button>
            <button class="btn2 g" data-skip>跳过解析，直接测评</button>
          </div>
        </div>`;
      const ta = elBody.querySelector("[data-resume]");
      const fi = elBody.querySelector("[data-file]");
      const drop = elBody.querySelector("[data-drop]");
      const info = elBody.querySelector("[data-fileinfo]");
      elBody.querySelector("[data-sample]").onclick = () => { ta.value = SAMPLE; };

      drop.onclick = () => fi.click();
      // 三个事件都要 preventDefault，否则浏览器会直接打开被拖进来的文件
      drop.addEventListener("dragenter", (e) => { e.preventDefault(); drop.classList.add("over"); });
      drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
      drop.addEventListener("dragleave", () => drop.classList.remove("over"));
      drop.addEventListener("drop", (e) => {
        e.preventDefault(); drop.classList.remove("over");
        takeFile((e.dataTransfer.files || [])[0], ta, info);
      });
      fi.onchange = () => takeFile(fi.files && fi.files[0], ta, info);
      elBody.querySelector("[data-start]").onclick = () => start(ta.value.trim());
      elBody.querySelector("[data-skip]").onclick = () => start("");
    }

    /** 取文件 → 文本。txt 本地读；PDF/DOCX 交服务端解析器（共用简历上传那套）。 */
    async function takeFile(f, ta, info) {
      if (!f) return;
      const kb = Math.max(1, Math.round(f.size / 1024));
      if (f.size > 20 * 1024 * 1024) {
        info.textContent = `✗ ${f.name} 超过 20MB`; log("文件过大，上限 20MB", false); return;
      }
      if (/\.(txt|md)$/i.test(f.name)) {
        const r = new FileReader();
        r.onload = () => {
          ta.value = String(r.result || "").slice(0, 20000);
          info.textContent = `✓ 已读取 ${f.name}（${kb}KB，${ta.value.length} 字）`;
        };
        r.readAsText(f);
        return;
      }
      info.textContent = `⏳ 正在解析 ${f.name}…`;
      log(`正在解析 ${f.name}…`, true);
      try {
        const url = "/v1/student/diagnosis/resume/extract";
        const fd = new FormData();
        fd.append("file", f, f.name);
        // 不能用 apiFetch：它会把 body 当 JSON 序列化，multipart 需要浏览器自己带 boundary
        const headers = await api.buildHeaders(url, "POST");
        const res = await fetch(api.absUrl(url), { method: "POST", headers, body: fd });
        const txt = await res.text();
        if (!res.ok) {
          let msg = txt.slice(0, 200);
          try { msg = (JSON.parse(txt).detail) || msg; } catch (_) {}
          throw new Error(msg);
        }
        const d = JSON.parse(txt);
        ta.value = d.content_text || "";
        info.textContent = `✓ 已解析 ${d.filename}（${kb}KB，提取 ${d.chars} 字）`;
        log("", false);
      } catch (e) {
        info.textContent = `✗ 解析失败：${(e.message || e)}`;
        log("解析失败，可改为直接粘贴简历文本", false);
      }
    }

    /* ── SSE ── */
    async function sse(url, body) {
      const headers = await api.buildHeaders(url, "POST", { "Content-Type": "application/json" });
      const res = await fetch(api.absUrl(url), { method: "POST", headers, body: JSON.stringify(body) });
      if (!res.ok || !res.body) throw new Error("HTTP " + res.status + " " + (await res.text()).slice(0, 160));
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const raw = buf.slice(0, i); buf = buf.slice(i + 2);
          let type = "message", data = "";
          raw.split("\n").forEach((ln) => {
            if (ln.startsWith("event:")) type = ln.slice(6).trim();
            else if (ln.startsWith("data:")) data += ln.slice(5).trim();
          });
          if (data) { try { onEvent(type, JSON.parse(data)); } catch (_) { /* 半包 */ } }
        }
      }
    }

    function onEvent(type, d) {
      if (type === "session") S.sessionId = d.session_id;
      else if (type === "stage") {
        // 服务端逐段推阶段状态，前端只负责渲染，不自己推断进度
        S.stageMap[d.stage] = { status: d.status, output: d.output || {} };
        renderSteps();
        if (d.message) log(d.message, d.status === "active");
      } else if (type === "plan") {
        S.total = d.total || 0;
        log(d.message || "", true);
      } else if (type === "question") {
        const q = d.question;
        if (q && !S.seen.has(q.index)) { S.seen.add(q.index); S.queue.push(q); }
        // 首题一到就开始答，不等其余题目生成完
        if (!S.current) next();
      } else if (type === "question_end") {
        S.questionEnd = true;
        S.total = d.total || S.total;
        log("", false);
        if (!S.current) next();
      } else if (type === "report") {
        S.report = d.report;
        if (opts.onReport) { try { opts.onReport(d.report); } catch (_) {} }
      } else if (type === "warn") {
        log(d.message || "", false);
      } else if (type === "error") {
        // 出题失败是终止态，不能让界面停在「正在出题…」或误显示「已答完」
        S.fatal = d.message || "出题失败";
        log("", false);
        renderAssess();
      } else if (type === "done") {
        log("", false);
      }
    }

    /* ── 阶段 2：一次只展示一道，答完从队列弹下一道 ── */
    function next() {
      if (!S.current && S.queue.length) S.current = S.queue.shift();
      renderAssess();
    }

    function renderAssess() {
      if (S.report) return renderReport();
      if (S.fatal) {
        elBody.innerHTML = `<div class="card2">
          <span class="tag2" style="border-color:var(--aw-warn);color:var(--aw-warn);
                background:rgba(245,158,11,.1)">无法开始测评</span>
          <h3>${esc(S.fatal)}</h3>
          <div style="margin-top:12px"><button class="btn2 g" data-retry>← 返回重新选择</button></div>
        </div>`;
        const r = elBody.querySelector("[data-retry]");
        if (r) r.onclick = () => { S.fatal = null; S.questionEnd = false; renderParse(); };
        return;
      }
      const q = S.current;
      if (!q) {
        // 题目分批产出，总题数事先不可知，所以不显示「共 N 题」——
        // 曾出现过「第 7 题 / 约 6 题」这种自相矛盾的进度
        // 只有「出过题且都答完」才是收卷；一道没出就 question_end 属于出题失败
        const waiting = S.questionEnd
          ? (S.seen.size ? "题目已答完，正在生成报告…" : "该岗位没有可出的题目")
          : (S.seen.size ? "正在生成后续题目…" : "AI 正在按岗位标准出题，请稍候…");
        elBody.innerHTML = `<div class="card2"><span class="tag2">F-04 · 多轮实战对话考评</span>
          <h3>实战场景出题考评</h3>
          <div class="m"><span class="sp"></span>${waiting}</div></div>`;
        return;
      }
      const opt = (q.options || []).map((o) =>
        `<button class="opt2" data-v="${o.value}">${esc(o.text)}</button>`).join("");
      elBody.innerHTML = `
        <div class="card2">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap">
            <span class="tag2">F-04 · 多轮实战对话考评</span>
            <button class="btn2 g" data-finish ${S.questionEnd ? "" : "disabled"}>生成最终报告 →</button>
          </div>
          <h3>针对【${esc(q.skill_key)}】实战场景出题考评</h3>
          <div class="m">第 ${q.index + 1} 题${S.total ? " / 共 " + S.total + " 题" : ""}
            <span class="badge2">${q.variant === "sjt" ? "情景判断" : "自评"}</span>
            ${q.required_level ? `<span class="badge2">岗位要求 L${q.required_level}</span>` : ""}</div>
          <div class="qbox">
            <div style="margin-bottom:10px">${esc(q.prompt)}</div>
            ${q.type === "choice" ? opt : `
              <textarea data-ans rows="4" placeholder="请结合具体经历作答：任务、方法、结果（有数据请一并给出）"></textarea>
              ${(q.rubric || []).length ? `<div class="m" style="font-size:12px;margin-top:8px">评分要点：${q.rubric.map(esc).join(" / ")}</div>` : ""}
              <div style="margin-top:10px"><button class="btn2" data-send>提交回答 →</button></div>`}
          </div>
        </div>`;
      if (q.type === "choice") {
        elBody.querySelectorAll(".opt2").forEach((b) => {
          b.onclick = () => { if (!S.busy) { b.classList.add("sel"); submit(Number(b.dataset.v)); } };
        });
      } else {
        elBody.querySelector("[data-send]").onclick = () => {
          if (!S.busy) submit(elBody.querySelector("[data-ans]").value.trim());
        };
      }
      const fin = elBody.querySelector("[data-finish]");
      if (fin) fin.onclick = () => { if (S.report) renderReport(); };
    }

    async function start(resumeText) {
      const occ = opts.getOccupationId ? opts.getOccupationId() : null;
      S.occupationId = occ;
      log("正在启动测评…", true);
      // 出题长连接：不 await，让题目边到边答；首题到达时 onEvent 会自动 next()
      sse("/v1/student/assessment/sessions/questions/stream",
          { occupation_id: occ || null, resume_text: resumeText || null })
        .catch((e) => log("出题失败：" + (e.message || e), false));
    }

    async function submit(answer) {
      if (answer === "" || answer == null) { log("请先作答", false); return; }
      const q = S.current;
      S.busy = true;
      try {
        // 提交即返回：选择题当场判分，问答题后台判，不阻塞下一题
        await api.apiFetch(`/v1/student/assessment/sessions/${S.sessionId}/answers`,
          { method: "POST", body: { index: q.index, answer } });
        S.answered += 1;
        S.current = null;
      } catch (e) {
        S.busy = false;
        log("提交失败：" + (e.message || e), false);
        return;
      }
      S.busy = false;
      if (S.queue.length) { next(); return; }
      if (S.questionEnd) { await settle(); return; }
      renderAssess();                       // 题还没出完，显示等待态
    }

    /** 全部答完 → 结算：等后台判分收尾并取报告 */
    async function settle() {
      log("正在生成综合能力报告…", true);
      renderAssess();
      try {
        await sse(`/v1/student/assessment/sessions/${S.sessionId}/report/stream`
          + (S.occupationId ? "?occupation_id=" + encodeURIComponent(S.occupationId) : ""), {});
      } catch (e) { log("结算失败：" + (e.message || e), false); }
      if (S.report) renderReport();
    }

    /* ── 阶段 3 ── */
    function renderReport() {
      const r = S.report || {};
      const st = (r.strengths || []).slice(0, 6);
      const gp = (r.gaps || []).slice(0, 6);
      elBody.innerHTML = `
        <div class="card2">
          <div style="display:flex;justify-content:space-between;gap:14px;flex-wrap:wrap">
            <div>
              <span class="tag2">知识图谱 × AI 评估生成</span>
              <h3>【${esc(r.target_occupation_name || "目标岗位")}】AI 智能能力诊断报告</h3>
              <div class="m">${esc(r.summary || "")}</div>
            </div>
            <div class="box2" style="text-align:center;min-width:140px;height:fit-content">
              <div class="m" style="font-size:12px">综合能力匹配度</div>
              <div class="score2">${r.match_score != null ? r.match_score + "%" : "—"}</div>
              <div class="m" style="font-size:12px">覆盖权重 ${r.coverage != null ? r.coverage + "%" : "—"}</div>
            </div>
          </div>
          <div class="g2" style="margin-top:14px">
            <div class="box2">
              <b>能力维度与目标极域图</b>
              <canvas data-radar width="420" height="340" style="width:100%;max-width:420px;display:block;margin:6px auto 0"></canvas>
              <div class="lg">
                <span><i class="dot2" style="background:#10b981"></i>学员实测能力</span>
                <span><i class="dot2" style="background:#818cf8"></i>岗位标准要求</span>
              </div>
            </div>
            <div>
              <div class="box2 good">
                <b style="color:var(--aw-brand2)">✓ 优势能力领域</b>
                <span class="m" style="font-size:12px">（已达到或超越基准）</span>
                <div>${st.length ? st.map((x) =>
                  `<span class="chip2 good">✓ ${esc(x.skill_key)} L${x.measured_level}</span>`).join("")
                  : '<div class="m" style="margin-top:6px">本次未测出达标项</div>'}</div>
              </div>
              <div class="box2 bad" style="margin-top:12px">
                <b style="color:var(--aw-warn)">⚠ 关键能力短板</b>
                <span class="m" style="font-size:12px">（需优先攻关提升）</span>
                <div>${gp.length ? gp.map((x) =>
                  `<span class="chip2 bad">⚠ ${esc(x.skill_key)} ${x.measured_level ? "L" + x.measured_level : "待补强"} → 需 L${x.required_level || "?"}</span>`).join("")
                  : '<div class="m" style="margin-top:6px">未发现明显短板</div>'}</div>
              </div>
              <button class="btn2" style="width:100%;margin-top:12px" data-path>
                基于短板一键生成个人自适应学习计划 →</button>
            </div>
          </div>
        </div>`;
      drawRadar(elBody.querySelector("[data-radar]"), r.radar || {});
      elBody.querySelector("[data-path]").onclick = () => {
        if (opts.onGotoPath) opts.onGotoPath(r);
        else location.href = "/student?tab=path";
      };
    }

    /** 双系列雷达：绿=学员实测，紫=岗位标准要求 */
    function drawRadar(cv, radar) {
      if (!cv) return;
      const axes = radar.categories || [], series = radar.series || [];
      const ctx = cv.getContext("2d");
      const W = cv.width, H = cv.height, cx = W / 2, cy = H / 2 + 4, R = Math.min(W, H) / 2 - 56;
      ctx.clearRect(0, 0, W, H);
      if (axes.length < 3) {
        ctx.fillStyle = "#8fa3c0"; ctx.font = "13px sans-serif"; ctx.textAlign = "center";
        ctx.fillText("测评维度不足，无法绘制雷达图", cx, cy); return;
      }
      const pt = (i, v) => {
        const a = -Math.PI / 2 + (i * 2 * Math.PI) / axes.length;
        const rr = R * Math.max(0, Math.min(100, v)) / 100;
        return [cx + rr * Math.cos(a), cy + rr * Math.sin(a)];
      };
      ctx.strokeStyle = "#1e2b45";
      for (let g = 1; g <= 4; g++) {
        ctx.beginPath();
        axes.forEach((_, i) => { const [x, y] = pt(i, (g / 4) * 100); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
        ctx.closePath(); ctx.stroke();
      }
      axes.forEach((_, i) => {
        ctx.beginPath(); ctx.moveTo(cx, cy);
        const [x, y] = pt(i, 100); ctx.lineTo(x, y); ctx.stroke();
      });
      const colors = { user: ["rgba(16,185,129,.35)", "#10b981"], required: ["rgba(129,140,248,.10)", "#818cf8"] };
      series.forEach((s) => {
        const [fill, line] = colors[s.key] || ["rgba(255,255,255,.1)", "#fff"];
        ctx.beginPath();
        (s.scores || []).forEach((v, i) => { const [x, y] = pt(i, v); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
        ctx.closePath(); ctx.fillStyle = fill; ctx.fill();
        ctx.strokeStyle = line; ctx.lineWidth = 2; ctx.stroke();
      });
      ctx.fillStyle = "#c7d5ea"; ctx.font = "12px sans-serif";
      axes.forEach((name, i) => {
        const [x, y] = pt(i, 122);
        ctx.textAlign = Math.abs(x - cx) < 12 ? "center" : (x > cx ? "left" : "right");
        ctx.fillText(String(name).slice(0, 8), x, y);
      });
    }

    /** 刷新恢复：会话状态在 LangGraph checkpointer 里 */
    async function resume(sessionId) {
      try {
        const s = await api.apiFetch(`/v1/student/assessment/sessions/${sessionId}`);
        S.sessionId = s.session_id; S.report = s.report;
        S.questionEnd = !!s.question_end;
        S.total = (s.progress || {}).target_total || (s.questions || []).length;
        (s.stages || []).forEach((x) => { S.stageMap[x.key] = { status: x.status, output: x.output }; });
        // 未答的题全部回队列，接着上次继续
        const answered = new Set((s.answers || []).map((a) => a.index));
        (s.questions || []).forEach((q) => {
          if (!answered.has(q.index) && !S.seen.has(q.index)) { S.seen.add(q.index); S.queue.push(q); }
        });
        S.answered = answered.size;
        renderSteps();
        return S.report ? renderReport() : next();
      } catch (_) { renderSteps(); renderParse(); }
    }

    renderSteps();
    renderParse();
    return { resume, getState: () => S };
  }

  global.AssessmentWidget = { mount };
})(window);
