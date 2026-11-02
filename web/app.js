/* 问卷星作答器 —— 前端逻辑
 *
 * 界面只有两块：答题区（含作答控制坞）+ 日志。
 * 控制坞的状态由两路数据合成：quick（环境/解析/答案三阶段）与
 * browser（逐题填写进度），最终对外只呈现一个状态灯、一个进度条、一套按钮。
 */

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const state = {
  survey: null,
  sig: '',              // 问卷指纹：答案与这份问卷是否对得上，靠它判断
  logSeq: 0,
  busy: false,          // 合成后的「正在运行」
  liveTopic: 0,         // 浏览器正在填的题号，与答题区高亮同源
  phase: 'idle',
};

/* ---------------------------------------------------------------- 网络 */
async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  return r.json().catch(() => ({ ok: false, error: '响应解析失败' }));
}

const post = (path, body) =>
  api(path, { method: 'POST', body: JSON.stringify(body || {}) });

/* ---------------------------------------------------------------- 提示 */
let toastTimer = null;
function toast(msg, isErr = false) {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast show' + (isErr ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = 'toast'; }, 2600);
}

/* ---------------------------------------------------------------- 渲染 */
const SEC_COLORS = ['#4c9aff', '#b07ce8', '#4caf7d', '#d9a441'];

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]
  ));
}

function renderSurvey(survey) {
  const box = $('#questions');
  if (!survey || !survey.questions || !survey.questions.length) {
    box.innerHTML = '<div class="empty">没有题目数据</div>';
    return;
  }

  let html = '';
  let curSec = null;
  let secIdx = 0;

  for (const q of survey.questions) {
    if (q.section !== curSec) {
      curSec = q.section;
      const color = SEC_COLORS[secIdx % SEC_COLORS.length];
      html += `<div class="sec"><i style="background:${color}"></i>
                 <span style="color:${color}">${esc(curSec)}</span></div>`;
      secIdx++;
    }

    let tag = q.type_name;
    if (q.max_select) tag += ` · 最多${q.max_select}项`;
    if (q.exact_select) tag += ` · 限选${q.exact_select}项`;

    html += `<div class="q" data-topic="${q.topic}">
      <div class="q-head">
        <span class="q-no">Q${q.topic}</span>
        ${q.required ? '<span class="q-req">*</span>' : ''}
        <span class="q-type">${esc(tag)}</span>
      </div>
      <div class="q-title">${esc(q.prompt)}</div>`;

    if (q.type_name === '单选') {
      html += '<div class="opts">';
      for (const o of q.options) {
        html += `<label class="opt">
          <input type="radio" name="q${q.topic}" value="${esc(o.value)}">
          <span>${esc(o.text)}</span></label>`;
      }
      html += '</div>';
    } else if (q.type_name === '多选') {
      // 上限取 max_select（最多选N项）或 exact_select（限选N项），
      // 两者都没有就不限制；data-max 是 checkMax 的唯一依据
      const cap = q.max_select || q.exact_select || '';
      html += `<div class="opts" data-max="${cap}" data-topic="${q.topic}">`;
      for (const o of q.options) {
        html += `<label class="opt">
          <input type="checkbox" name="q${q.topic}" value="${esc(o.value)}">
          <span>${esc(o.text)}</span></label>`;
      }
      html += '</div>';
    } else if (q.type_name === '矩阵量表') {
      const scale = q.matrix.scale;
      const mid = Math.floor(scale.length / 2) + 1;
      html += '<table class="matrix"><tr><th>子项</th><th>评分</th></tr>';
      q.matrix.rows.forEach((row, i) => {
        let opts = '';
        scale.forEach((s, si) => {
          const v = si + 1;
          opts += `<option value="${v}"${v === mid ? ' selected' : ''}>${v} · ${esc(s)}</option>`;
        });
        html += `<tr>
          <td>${i + 1}. ${esc(row)}</td>
          <td><select data-topic="${q.topic}" data-row="${i}">${opts}</select></td>
        </tr>`;
      });
      html += '</table>';
    } else if (q.type_name.startsWith('填空')) {
      // 单行用 input、多行用 textarea；模板接管后这个框只作预览
      const multi = q.type_name === '填空(多行)';
      const field = multi
        ? `<textarea class="fillin" data-topic="${q.topic}" rows="3" placeholder="输入答案"></textarea>`
        : `<input class="fillin" type="text" data-topic="${q.topic}" placeholder="输入答案">`;
      html += `<div class="fill-box">${field}<span class="fill-tag" data-tag="${q.topic}" hidden></span></div>`;
    }

    html += '</div>';
  }

  box.innerHTML = html;

  // 选中态高亮
  box.querySelectorAll('.opt input').forEach((inp) => {
    inp.addEventListener('change', () => {
      const name = inp.name;
      box.querySelectorAll(`input[name="${name}"]`).forEach((x) => {
        x.closest('.opt').classList.toggle('on', x.checked);
      });
      if (inp.type === 'checkbox') checkMax(inp);
      updateCounts();   // 手工作答也要实时更新「已填」，否则用户以为没选上
    });
  });

  // 矩阵：每行独立
  box.querySelectorAll('.matrix select').forEach((s) => {
    s.addEventListener('change', updateCounts);
  });

  // 矩阵：点行内文字也能聚焦到该行下拉，放大可点区域
  box.querySelectorAll('.matrix tr').forEach((tr) => {
    tr.addEventListener('click', (e) => {
      if (e.target.tagName === 'SELECT') return;
      const s = tr.querySelector('select');
      if (s) s.focus();
    });
  });

  // 填空：边打字边更新计数
  box.querySelectorAll('.fillin').forEach((el) => {
    el.addEventListener('input', updateCounts);
  });

  updateCounts();
  if (tplReady) applyTplToAnswerArea();
}

/* 多选超限：回滚本次勾选并给明确视觉反馈，避免用户以为「点了没反应」 */
function checkMax(inp) {
  const group = inp.closest('.opts');
  const max = parseInt(group.dataset.max || '0', 10);
  if (!max) return;
  const checked = group.querySelectorAll('input:checked');
  if (checked.length > max) {
    inp.checked = false;
    const opt = inp.closest('.opt');
    opt.classList.remove('on');
    // 重放动画：先移除再强制重排，否则连续超限时第二次不触发
    opt.classList.remove('shake');
    void opt.offsetWidth;
    opt.classList.add('shake');
    setTimeout(() => opt.classList.remove('shake'), 400);
    toast(`Q${group.dataset.topic} 最多可选 ${max} 项，已取消本次勾选`, true);
  }
}

/* ---------------------------------------------------------------- 取值 */
function collectAnswers() {
  const s = state.survey;
  if (!s) return {};
  const ans = {};

  for (const q of s.questions) {
    const t = q.topic;
    if (q.type_name === '单选') {
      const el = document.querySelector(`input[name="q${t}"]:checked`);
      if (el) ans[t] = el.value;
    } else if (q.type_name === '多选') {
      const picked = $$(`input[name="q${t}"]:checked`).map((x) => x.value);
      if (picked.length) ans[t] = picked;
    } else if (q.type_name === '矩阵量表') {
      const sels = $$(`select[data-topic="${t}"]`);
      const vals = sels.map((x) => parseInt(x.value, 10));
      if (vals.length && vals.every((v) => v > 0)) ans[t] = vals;
    } else if (q.type_name.startsWith('填空')) {
      const el = document.querySelector(`.fillin[data-topic="${t}"]`);
      const v = el ? el.value.trim() : '';
      if (v) ans[t] = v;
    }
  }
  return ans;
}

/** 把超限的勾选截断到上限。导入文件、后端回填这两条路径都可能带进超限值，
 *  它们不经过 change 事件，checkMax 管不到。 */
function trimOverLimit(quiet = false) {
  const fixed = [];
  $$('.opts[data-max]').forEach((group) => {
    const max = parseInt(group.dataset.max || '0', 10);
    if (!max) return;
    const checked = Array.from(group.querySelectorAll('input:checked'));
    if (checked.length <= max) return;
    checked.slice(max).forEach((x) => {
      x.checked = false;
      x.closest('.opt').classList.remove('on');
    });
    fixed.push(`Q${group.dataset.topic}`);
  });
  if (fixed.length && !quiet) {
    toast(`${fixed.join('、')} 超出限选项数，已自动截断到上限`, true);
  }
  return fixed;
}

function applyAnswers(ans) {
  const s = state.survey;
  if (!s || !ans) return;

  for (const q of s.questions) {
    const t = q.topic;
    const v = ans[t];
    if (v === undefined || v === null) continue;

    if (q.type_name === '单选') {
      const el = document.querySelector(`input[name="q${t}"][value="${v}"]`);
      if (el) { el.checked = true; el.closest('.opt').classList.add('on'); }
    } else if (q.type_name === '多选') {
      const wanted = (Array.isArray(v) ? v : [v]).map(String);
      $$(`input[name="q${t}"]`).forEach((x) => {
        x.checked = wanted.includes(x.value);
        x.closest('.opt').classList.toggle('on', x.checked);
      });
    } else if (q.type_name === '矩阵量表') {
      const vals = Array.isArray(v) ? v : [v];
      $$(`select[data-topic="${t}"]`).forEach((sel, i) => {
        if (i < vals.length) {
          const n = Math.max(1, Math.min(q.matrix.scale.length, parseInt(vals[i], 10) || 1));
          sel.value = String(n);
        }
      });
    } else if (q.type_name.startsWith('填空')) {
      const el = document.querySelector(`.fillin[data-topic="${t}"]`);
      if (el) el.value = String(v);
    }
  }
  trimOverLimit();
  updateCounts();
}

function updateCounts() {
  const ans = collectAnswers();
  const total = state.survey ? state.survey.questions.length : 0;
  $('#filled').textContent = Object.keys(ans).length;
  $('#total').textContent = total;
}

/* ---------------------------------------------------------------- 填充 */
async function fill(kind) {
  if (!state.survey) { toast('先加载问卷', true); return; }

  if (kind === 'clear') {
    $$('#questions input').forEach((x) => {
      if (x.type === 'radio' || x.type === 'checkbox') {
        x.checked = false;
        x.closest('.opt').classList.remove('on');
      }
    });
    $$('#questions select').forEach((s) => { s.selectedIndex = Math.floor(s.options.length / 2); });
    updateCounts();
    return;
  }

  if (kind === 'random') {
    // 必须走 /api/answers/random：/api/answers/template 是固定首项模板，不是随机
    const r = await api('/api/answers/random');
    if (!r.ok) { toast(r.error || '取随机答案失败', true); return; }
    applyAnswers(r.answers);
    const lens = state.survey.questions
      .filter((q) => q.type_name === '多选')
      .map((q) => (r.answers[q.topic] || []).length);
    const avg = lens.length ? (lens.reduce((a, b) => a + b, 0) / lens.length).toFixed(1) : '0';
    toast(`已随机填充 · ${lens.length} 道多选平均 ${avg} 项`);
    return;
  }

  const ans = {};
  for (const q of state.survey.questions) {
    const t = q.topic;
    if (q.type_name === '单选') {
      ans[t] = kind === 'first' ? q.options[0].value : q.options[q.options.length - 1].value;
    } else if (q.type_name === '多选') {
      const n = q.exact_select || q.min_select || 1;
      ans[t] = kind === 'first'
        ? q.options.slice(0, n).map((o) => o.value)
        : q.options.slice(-n).map((o) => o.value);
    } else if (q.type_name === '矩阵量表') {
      const len = q.matrix.scale.length;
      ans[t] = new Array(q.matrix.rows.length).fill(kind === 'first' ? len : 1);
    }
  }
  applyAnswers(ans);
  toast(kind === 'first' ? '已全部选首项（矩阵最高分）' : '已全部选末项（矩阵最低分）');
}

/* ---------------------------------------------------------------- 导入导出 */
function exportAnswers() {
  const ans = collectAnswers();
  if (!Object.keys(ans).length) { toast('还没有作答', true); return; }
  const blob = new Blob([JSON.stringify(ans, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `answers_${Date.now()}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
  toast('答案已导出');
}

function importAnswers(file) {
  const fr = new FileReader();
  fr.onload = () => {
    try {
      applyAnswers(JSON.parse(fr.result));
      toast('答案已导入');
    } catch (e) {
      toast('JSON 解析失败', true);
    }
  };
  fr.readAsText(file, 'utf-8');
}

/* ---------------------------------------------------------------- 题目文本复制 */
/* 后端按「题号 + 题型 + 选项」生成纯文本。打开时先弹预览再写剪贴板：
   浏览器如果拦截自动复制，textarea 已经全选，用户按 Ctrl+C 就能走。 */
let txtCount = 0;

async function writeClipboard(text) {
  if (!text) return false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (e) { /* 走下面的 execCommand 兜底 */ }
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.cssText = 'position:fixed;top:0;left:-9999px;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  } catch (e) {
    return false;
  }
}

async function exportSurveyText() {
  if (!state.survey) { toast('先加载问卷', true); return; }
  const btn = $('#btnExportText');
  if (btn) btn.disabled = true;
  try {
    const r = await api('/api/survey/export');
    if (!r.ok) { toast(r.error || '生成题目文本失败', true); return; }
    if (!r.count || !r.text) { toast('当前问卷没有可复制的题目', true); return; }

    txtCount = r.count || 0;
    const body = $('#txtBody');
    body.value = r.text || '';
    const warns = r.warnings || [];
    const stat = $('#txtStat');
    stat.textContent = `${txtCount} 题 · ${body.value.length} 字符`
      + (warns.length ? ` · ${warns.length} 条提示` : '');
    stat.title = warns.join('\n');
    const warnBox = $('#txtWarn');
    if (warnBox) {
      warnBox.hidden = warns.length === 0;
      warnBox.textContent = warns.length
        ? warns.slice(0, 4).join('；') + (warns.length > 4 ? `；…共 ${warns.length} 条` : '')
        : '';
    }
    $('#txtMask').hidden = false;

    const ok = await writeClipboard(body.value);
    if (ok) {
      toast(`已复制 ${txtCount} 题题目文本`);
    } else {
      body.focus();
      body.select();
      toast('自动复制被浏览器拦截，已全选，按 Ctrl+C 复制', true);
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function copySurveyText() {
  const body = $('#txtBody');
  const ok = await writeClipboard(body.value);
  if (ok) toast(`已复制 ${txtCount || ''} 题题目文本`);
  else {
    body.focus();
    body.select();
    toast('复制失败，已全选，请按 Ctrl+C', true);
  }
}

function closeSurveyText() {
  const mask = $('#txtMask');
  if (mask) mask.hidden = true;
}

/* ---------------------------------------------------------------- 填空题模板 */
/* 模板与题目的对应：按 topic（题号）绑定，不按题干文本 —— 题干可能微调，
   题号在一份问卷里唯一且稳定。数据存在后端 fill_template.json，刷新不丢。 */
const tpl = { items: {}, random: false, sig: '', stale: false };
let tplReady = false;

function fillQuestions() {
  return (state.survey ? state.survey.questions : [])
    .filter((q) => q.type_name && q.type_name.indexOf('填空') === 0);
}

async function loadTpl() {
  const r = await api('/api/fill/template');
  if (!r.ok) return;
  tpl.items = r.items || {};
  tpl.random = !!r.random;
  tpl.sig = r.sig || '';
  tpl.stale = !!r.stale;
  tplReady = true;
  // 模板属于别的问卷时给按钮加个警示色，别让人以为配好的还能直接用
  const btn = $('#btnFillTpl');
  if (btn) btn.classList.toggle('stale', tpl.stale);
  applyTplToAnswerArea();
}

function applyTplToAnswerArea() {
  // 模板属于另一份问卷时不往界面上套：后端运行时也会整份作废，
  // 两边表现要一致，否则界面上显示「模板 4 候选」但实际没用
  const items = tpl.stale ? {} : tpl.items;
  let n = 0;
  for (const q of fillQuestions()) {
    const vals = items[q.topic] || [];
    const inp = document.querySelector(`.fillin[data-topic="${q.topic}"]`);
    const tag = document.querySelector(`.fill-tag[data-tag="${q.topic}"]`);
    if (inp) {
      // 由模板接管时置灰：这个框里的值不会被直接提交，留着可编辑会误导
      inp.classList.toggle('by-tpl', vals.length > 0);
      inp.title = vals.length ? '由填空题模板提供候选，此处仅显示第一个' : '';
      if (vals.length) inp.value = vals[0];
    }
    if (tag) {
      tag.hidden = !vals.length;
      tag.textContent = vals.length ? `模板 ${vals.length} 候选` : '';
      tag.classList.toggle('on', vals.length > 0);
    }
    if (vals.length) n++;
  }
  const badge = $('#tplBadge');
  if (badge) { badge.hidden = n === 0; badge.textContent = n; }
  updateCounts();
}

function renderTplList() {
  const box = $('#tplList');
  const qs = fillQuestions();
  if (!qs.length) {
    box.innerHTML = '<div class="dr-empty">这份问卷没有填空题</div>';
    return;
  }
  box.innerHTML = qs.map((q) => {
    const vals = tpl.items[q.topic] || [];
    return `<div class="tpl-item${vals.length ? ' on' : ''}" data-topic="${q.topic}">
      <div class="tpl-q"><b>Q${q.topic}</b><span>${esc(q.prompt)}</span></div>
      <textarea data-topic="${q.topic}" placeholder="一行一个候选，例如：&#10;北京&#10;上海&#10;广州">${esc(vals.join('\n'))}</textarea>
      <div class="tpl-n${vals.length ? ' on' : ''}">${vals.length ? vals.length + ' 个候选' : '未配置，用答题区里填的值'}</div>
    </div>`;
  }).join('');

  box.querySelectorAll('textarea').forEach((t) => {
    t.addEventListener('input', () => {
      const n = t.value.split('\n').map((s) => s.trim()).filter(Boolean).length;
      const item = t.closest('.tpl-item');
      item.classList.toggle('on', n > 0);
      const lab = item.querySelector('.tpl-n');
      lab.textContent = n ? `${n} 个候选` : '未配置，用答题区里填的值';
      lab.classList.toggle('on', n > 0);
      updateTplStat();
    });
  });
  updateTplStat();
}

function collectTpl() {
  const out = {};
  $$('#tplList textarea').forEach((t) => {
    const vals = t.value.split('\n').map((s) => s.trim()).filter(Boolean);
    if (vals.length) out[t.dataset.topic] = vals;
  });
  return out;
}

function updateTplStat() {
  const items = collectTpl();
  const n = Object.keys(items).length;
  const total = Object.values(items).reduce((a, v) => a + v.length, 0);
  const el = $('#tplStat');
  if (el) el.textContent = n ? `${n} 题 / ${total} 个候选` : '尚未配置';
}

function setTplModeHint(on) {
  const h = $('#tplMode');
  if (!h) return;
  h.textContent = on
    ? '开启：每轮从候选里依次抽取，候选数够时各轮答案互不相同'
    : '关闭：每轮都填第一个候选，多轮答案完全一致';
  h.classList.toggle('on', on);
}

function openTpl() {
  if ($('#wDrawer') && !$('#wDrawer').hidden) closeWeights();
  renderTplList();
  $('#tplRandom').checked = tpl.random;
  setTplModeHint(tpl.random);
  const m = $('#tplMask');
  const d = $('#tplDrawer');
  m.hidden = false;
  d.hidden = false;
  requestAnimationFrame(() => { m.classList.add('open'); d.classList.add('open'); });
  if (tpl.stale) toast('模板是按上一份问卷配的，请核对题号', true);
}

function closeTpl() {
  const m = $('#tplMask');
  const d = $('#tplDrawer');
  m.classList.remove('open');
  d.classList.remove('open');
  setTimeout(() => { m.hidden = true; d.hidden = true; }, 230);
}

async function saveTpl() {
  const items = collectTpl();
  const random = $('#tplRandom').checked;
  const r = await post('/api/fill/template', { items, random });
  if (!r.ok) { toast(r.error || '保存失败', true); return; }
  tpl.items = r.items || {};
  tpl.random = r.random;
  tpl.sig = r.sig || '';
  tpl.stale = false;
  applyTplToAnswerArea();
  const n = Object.keys(tpl.items).length;
  const total = Object.values(tpl.items).reduce((a, v) => a + v.length, 0);
  toast(n ? `模板已保存 · ${n} 题 / ${total} 个候选 · ${random ? '每轮随机' : '固定首项'}`
          : '模板已清空，填空题改用答题区里填的值');
  closeTpl();
}

/** 必答填空题既没手填、也没配候选时拦住，别等提交才报错 */
function missingRequiredFill() {
  const out = [];
  const ans = collectAnswers();
  const items = tpl.stale ? {} : tpl.items;   // 失效的模板不算答案来源
  for (const q of fillQuestions()) {
    if (!q.required) continue;
    const fromArea = (ans[q.topic] || '').toString().trim();
    const fromTpl = (items[q.topic] || []).length;
    if (!fromArea && !fromTpl) out.push(q);
  }
  return out;
}

/* ---------------------------------------------------------------- 选项概率配置 */
/* 配置存在后端 choice_weights.json，键是「轮次 → 题号」，每轮是独立对象。
   前端只做展示与输入校验；归一化、可行性检查、抽样都以服务端返回为准。 */
const WT = {
  rounds: {},          // {"1": {"3": {mode, weights, count, sig}}}
  sig: '',
  currentSig: '',
  stale: false,
  round: 1,
  topic: null,
  draft: null,         // 当前编辑中的副本，未保存不影响 WT.rounds
  dirty: false,
  preview: null,
  timer: null,
  seq: 0,
  backendOld: false,   // 后端没有 /api/weights：旧进程还在跑，重启程序即可
  stats: { pairs: 0, configured_rounds: [], questions: [] },
};

function weightQuestions() {
  const ok = ['单选', '多选', '矩阵量表', '下拉'];
  return (state.survey ? state.survey.questions : []).filter((q) => ok.includes(q.type_name));
}

function weightQ(topic) {
  return weightQuestions().find((q) => String(q.topic) === String(topic)) || null;
}

function wtValues(q) {
  if (q.type_name === '矩阵量表') {
    return q.matrix.scale.map((s, i) => ({ value: String(i + 1), text: `${i + 1} · ${s}` }));
  }
  return q.options.map((o) => ({ value: String(o.value), text: o.text }));
}

function roundCfg(round) {
  const v = WT.rounds[String(round)];
  return (v && typeof v === 'object' && !Array.isArray(v)) ? v : {};
}

function maxRound() {
  const nums = Object.keys(WT.rounds).map((x) => parseInt(x, 10)).filter((x) => x > 0);
  const count = parseInt(($('#bcount') && $('#bcount').value) || '1', 10) || 1;
  return Math.min(999, Math.max(1, count, WT.round, ...(nums.length ? nums : [1])));
}

function updateWBadge() {
  const badge = $('#wBadge');
  const btn = $('#btnWeights');
  if (!badge || !btn) return;
  const n = WT.stats.pairs || 0;
  badge.hidden = n === 0;
  badge.textContent = n;
  btn.classList.toggle('stale', !!WT.stale);
  btn.title = WT.stale
    ? '概率配置属于上一份问卷，保存新配置后会整体重建'
    : '按轮次给每道选择题设置选项出现概率';
}

async function loadWeights() {
  try {
    const r = await api('/api/weights');
    if (!r.ok) {
      // 旧版 server.py 没有这个路由：GET 返回 HTML 404，解析失败；
      // 旧版 do_POST 返回 {"error":"not found"}。两种情况都归到「后端过旧」。
      const msg = String(r.error || '');
      if (msg.includes('not found') || msg.includes('响应解析失败')) {
        WT.backendOld = true;
      }
      return;
    }
    WT.backendOld = false;
    WT.rounds = r.rounds || {};
    WT.sig = r.sig || '';
    WT.currentSig = r.current_sig || '';
    WT.stale = !!r.stale;
    WT.stats = {
      pairs: r.pairs || 0,
      configured_rounds: r.configured_rounds || [],
      questions: r.questions || [],
    };
    updateWBadge();
    if (!$('#wDrawer') || $('#wDrawer').hidden) return;
    renderWQList();
    renderWStat();
  } catch (e) { /* 服务重启时忽略 */ }
}

function openWeights() {
  if (!state.survey) { toast('先加载问卷', true); return; }
  if (WT.backendOld) {
    toast('后端还是旧版（没有 /api/weights）。关闭程序后重新启动，再刷新本页', true);
    return;
  }
  if (!weightQuestions().length) { toast('这份问卷没有可配置的选择题', true); return; }
  if (!$('#wDrawer').hidden) return;
  // 两个抽屉共用同一层遮罩，同时开会互相盖住，先收起另一个
  if ($('#tplDrawer') && !$('#tplDrawer').hidden) closeTpl();

  WT.round = Math.max(1, Math.min(999, WT.round || 1));
  $('#wRound').value = WT.round;
  const conf = roundCfg(WT.round);
  const configured = Object.keys(conf).filter((t) => weightQ(t));
  WT.topic = configured.length ? configured[0] : String(weightQuestions()[0].topic);
  WT.draft = null;
  WT.dirty = false;
  WT.preview = null;

  renderWQList();
  renderWEditor();
  renderWStat();

  const m = $('#wMask');
  const d = $('#wDrawer');
  m.hidden = false;
  d.hidden = false;
  requestAnimationFrame(() => { m.classList.add('open'); d.classList.add('open'); });
  if (WT.stale) toast('概率配置是按上一份问卷保存的，保存时会重建', true);
  schedulePreview();
}

function closeWeights() {
  const m = $('#wMask');
  const d = $('#wDrawer');
  if (!m || d.hidden) return;
  m.classList.remove('open');
  d.classList.remove('open');
  setTimeout(() => { m.hidden = true; d.hidden = true; }, 230);
}

function renderWQList() {
  const box = $('#wQList');
  if (!box) return;
  const conf = roundCfg(WT.round);
  box.innerHTML = weightQuestions().map((q) => {
    const on = conf[String(q.topic)] ? ' on' : '';
    const sel = String(q.topic) === String(WT.topic) ? ' on' : '';
    const prompt = q.prompt.replace(/\s+/g, ' ').slice(0, 18);
    return `<button class="w-q${sel}" data-topic="${q.topic}">
      <b>Q${q.topic}</b><span>${esc(prompt)}</span>
      <i class="w-dot${on}"></i>
    </button>`;
  }).join('');
  box.querySelectorAll('.w-q').forEach((el) => {
    el.addEventListener('click', () => selectWQ(el.dataset.topic));
  });
}

function renderWStat() {
  const stat = $('#wStat');
  const hint = $('#wRoundHint');
  if (!stat || !hint) return;
  const conf = roundCfg(WT.round);
  const n = Object.keys(conf).filter((t) => weightQ(t)).length;
  const total = weightQuestions().length;
  stat.textContent = `第 ${WT.round} 轮 · 已配置 ${n}/${total} 题`;
  hint.textContent = `共 ${maxRound()} 轮 · 本轮 ${n} 题`;
}

function selectWQ(topic) {
  topic = String(topic);
  if (topic === String(WT.topic)) return;
  if (WT.dirty && !window.confirm('当前题目有未保存的修改，切换会丢弃。继续？')) return;
  WT.topic = topic;
  WT.draft = null;
  WT.dirty = false;
  WT.preview = null;
  renderWQList();
  renderWEditor();
  renderWStat();
  schedulePreview();
}

/** 从已存配置或默认值生成编辑副本。界面里的数字统一按「百分数/相对权重」显示。 */
function currentDraft() {
  const q = weightQ(WT.topic);
  if (!q) return null;
  if (WT.draft) return WT.draft;

  const stored = roundCfg(WT.round)[String(q.topic)];
  const vals = wtValues(q);
  if (stored) {
    const weights = {};
    for (const v of vals) {
      const raw = (stored.weights || {})[v.value];
      weights[v.value] = raw === undefined ? 0 : +(raw * 100).toFixed(2);
    }
    WT.draft = {
      mode: stored.mode || defaultWMode(q),
      weights,
      count: stored.count == null ? null : Number(stored.count),
    };
  } else {
    const n = vals.length || 1;
    let each;
    if (q.type_name === '多选') {
      each = uniformMultiPct(q, n);
    } else {
      each = +(100 / n).toFixed(2);
    }
    const weights = {};
    vals.forEach((v) => { weights[v.value] = each; });
    WT.draft = { mode: defaultWMode(q), weights, count: null };
  }
  return WT.draft;
}

/** 多选默认值：沿用题干项数的三角分布均值（峰值在 1/3 处），
 *  再把期望项数平均摊到每个选项上。这样「不配置」和原来的随机作答
 *  在平均选中项数上是同一个量级，不会一打开就变成只选 1 项。 */
function uniformMultiPct(q, n) {
  if (q.exact_select) return +(100 * q.exact_select / n).toFixed(2);
  const lo = Math.max(1, q.min_select || 1);
  const hi = Math.min(q.max_select || n, n);
  const bound = Math.max(1, Math.min(lo, hi));
  const k = bound + (Math.max(bound, hi) - bound) / 3;
  return +(100 * k / n).toFixed(2);
}

function defaultWMode(q) {
  if (q.type_name === '多选') return 'multi_target';
  if (q.type_name === '矩阵量表') return 'matrix';
  return 'single';
}

function modeHint(q, cfg) {
  if (cfg.mode === 'multi_target') {
    return '目标出现概率：每个选项在每一轮被选中的比例。所有概率之和 = 平均选中项数，'
      + '限项约束下列选项之间会互相牵连，实际分布以下方「实测」为准。';
  }
  if (cfg.mode === 'multi_weight') {
    return '相对权重：先按题干确定选几项，再按权重不放回抽。权重只决定倾向，不保证精确比例。';
  }
  if (cfg.mode === 'matrix') {
    const rows = q.matrix ? q.matrix.rows.length : 0;
    return `矩阵 ${rows} 个子项共用这一组分值分布，每行独立抽一次。`;
  }
  return '权重会自动归一化成出现概率，未配置的轮次仍走默认均匀随机。';
}

function renderWEditor() {
  const box = $('#wEditor');
  const q = weightQ(WT.topic);
  if (!q) {
    box.innerHTML = '<div class="dr-empty">左侧选一道选择题开始配置</div>';
    return;
  }
  const cfg = currentDraft();
  const vals = wtValues(q);
  const tag = q.type_name === '多选'
    ? `多选 · ${q.max_select ? '最多' + q.max_select + '项' : (q.exact_select ? '限选' + q.exact_select + '项' : '不限项')}`
    : q.type_name;

  let html = `<div class="w-qhead"><b>Q${q.topic}</b><span class="w-type">${esc(tag)}</span></div>
    <div class="w-prompt">${esc(q.prompt)}</div>`;

  const used = Object.keys(WT.rounds)
    .filter((r) => roundCfg(r)[String(q.topic)])
    .map((r) => parseInt(r, 10))
    .filter((r) => r > 0)
    .sort((a, b) => a - b);
  html += `<div class="w-used">
    <span>已配置轮次：${used.length
      ? used.map((r) => `第 ${r} 轮`).join('、')
      : '无（当前使用默认均匀）'}</span>
    ${used.length ? '<button class="w-linkbtn" id="wCopyQ">本题套用到全部轮次</button>' : ''}
  </div>`;

  if (q.type_name === '多选') {
    html += `<div class="w-modes">
      <button data-mode="multi_target" class="${cfg.mode === 'multi_target' ? 'on' : ''}">目标出现概率</button>
      <button data-mode="multi_weight" class="${cfg.mode === 'multi_weight' ? 'on' : ''}">相对权重</button>
    </div>
    <label class="w-count">固定选
      <input id="wCount" type="number" min="1" max="${vals.length}"
             value="${q.exact_select ? q.exact_select : (cfg.count == null ? '' : cfg.count)}"
             ${q.exact_select ? 'disabled' : ''}
             placeholder="${q.exact_select ? '限选' + q.exact_select : '按题干'}"> 项
      <span>${q.exact_select ? '（题干限选，不可改）' : '（留空=按题干约束）'}</span>
    </label>`;
  }

  html += `<p class="w-hint" id="wModeHint">${esc(modeHint(q, cfg))}</p>
    <div id="wRows">`;

  for (const v of vals) {
    const num = cfg.weights[v.value] == null ? 0 : cfg.weights[v.value];
    const pct = displayPct(cfg, v.value);
    html += `<div class="w-row${q.type_name === '矩阵量表' ? ' matrix' : ''}" data-val="${esc(v.value)}">
      <span class="w-opt">${esc(v.text)}</span>
      <input class="w-num" type="number" min="0" step="1" value="${num}">
      <span class="w-pct">${pct}</span>
      <input class="w-range" type="range" min="0" max="100" step="1" value="${Math.min(100, num)}">
      <div class="w-bars"><i class="wt"></i><i class="wa"></i></div>
    </div>`;
  }
  html += `</div>
    <p class="w-hint" id="wPreview">改动后自动实测 600 次，用于核对实际分布</p>`;

  box.innerHTML = html;

  box.querySelectorAll('.w-row').forEach((row) => {
    const val = row.dataset.val;
    const num = row.querySelector('.w-num');
    const range = row.querySelector('.w-range');

    const commit = (v) => {
      const n = Number(v);
      cfg.weights[val] = Number.isFinite(n) ? n : NaN;
      WT.dirty = true;
      WT.preview = null;
      paintRow(row, cfg, val);
      schedulePreview();
    };
    num.addEventListener('input', () => {
      range.value = String(Math.max(0, Math.min(100, Number(num.value) || 0)));
      commit(num.value);
    });
    range.addEventListener('input', () => {
      num.value = range.value;
      commit(range.value);
    });
  });

  if (q.type_name === '多选') {
    box.querySelectorAll('.w-modes button').forEach((b) => {
      b.addEventListener('click', () => {
        if (cfg.mode === b.dataset.mode) return;
        cfg.mode = b.dataset.mode;
        WT.dirty = true;
        WT.preview = null;
        renderWEditor();
        schedulePreview();
      });
    });
    const countEl = $('#wCount');
    if (countEl) {
      countEl.addEventListener('input', () => {
        const n = parseInt(countEl.value, 10);
        cfg.count = Number.isFinite(n) && n > 0 ? n : null;
        WT.dirty = true;
        WT.preview = null;
        updateWStatPreview();
        schedulePreview();
      });
    }
  }
  const copyQ = $('#wCopyQ');
  if (copyQ) copyQ.addEventListener('click', copyWQuestionAll);
  paintPreviewBars();
}

function displayPct(cfg, val) {
  if (cfg.mode === 'multi_target') {
    const v = Number(cfg.weights[val]) || 0;
    return `${v.toFixed(1)}%`;
  }
  const sum = Object.values(cfg.weights).reduce((a, b) => a + (Number(b) || 0), 0);
  const v = Number(cfg.weights[val]) || 0;
  return sum > 0 ? `${(v / sum * 100).toFixed(1)}%` : '0%';
}

function targetShare(cfg, val) {
  const v = Number(cfg.weights[val]) || 0;
  if (cfg.mode === 'multi_target') return Math.max(0, Math.min(1, v / 100));
  const sum = Object.values(cfg.weights).reduce((a, b) => a + (Number(b) || 0), 0);
  return sum > 0 ? Math.max(0, Math.min(1, v / sum)) : 0;
}

function paintRow(row, cfg, val) {
  const pct = row.querySelector('.w-pct');
  if (pct) pct.textContent = displayPct(cfg, val);
  const wt = row.querySelector('.wt');
  if (wt) wt.style.setProperty('--w', `${(targetShare(cfg, val) * 100).toFixed(1)}%`);
}

function paintPreviewBars() {
  const q = weightQ(WT.topic);
  const cfg = currentDraft();
  if (!q || !cfg) return;
  const pv = WT.preview;
  $$('#wRows .w-row').forEach((row) => {
    const val = row.dataset.val;
    const wt = row.querySelector('.wt');
    const wa = row.querySelector('.wa');
    // 目标条优先用服务端归一化后的目标值：固定选 2 项时用户填的 80+70
    // 会被缩放成 53+47，画原值会和实际抽样对不上
    const target = pv && pv.target ? (pv.target[val] || 0) : targetShare(cfg, val);
    if (wt) wt.style.setProperty('--w', `${(target * 100).toFixed(1)}%`);
    if (wa) {
      const a = pv && pv.actual ? (pv.actual[val] || 0) : 0;
      wa.style.setProperty('--w', `${(a * 100).toFixed(1)}%`);
    }
  });
}

async function runWPreview() {
  const q = weightQ(WT.topic);
  const cfg = currentDraft();
  if (!q || !cfg) return;
  const bad = localWCheck(true);
  if (bad) { showWMsg(bad, 'err'); return; }

  const seq = ++WT.seq;
  try {
    const r = await post('/api/weights/preview', {
      topic: q.topic,
      config: payloadW(cfg),
      samples: 600,
    });
    if (seq !== WT.seq) return;
    if (!r.ok) {
      const msg = String(r.error || '');
      if (msg.includes('not found')) {
        WT.backendOld = true;
        showWMsg('后端没有 /api/weights/preview，当前进程还是旧版。关闭程序重启后再试。', 'err');
      } else {
        showWMsg(msg || '实测失败', 'err');
      }
      return;
    }

    WT.preview = r;
    paintPreviewBars();
    const line = $('#wPreview');
    if (line) {
      let txt = `实测 ${r.samples} 次`;
      if (q.type_name === '多选') txt += ` · 平均选 ${r.mean_count} 项`;
      if (r.target) {
        let maxErr = 0;
        for (const v of r.values) {
          maxErr = Math.max(maxErr, Math.abs((r.actual[v] || 0) - (r.target[v] || 0)));
        }
        txt += ` · 与目标最大偏差 ${(maxErr * 100).toFixed(1)}%`;
      }
      line.textContent = txt;
      line.className = 'w-hint';
    }
    const warns = r.warnings || [];
    if (warns.length) showWMsg(warns.join('；'), 'warn');
    else showWMsg('');
  } catch (e) {
    if (seq === WT.seq) showWMsg('实测请求失败', 'err');
  }
}

function schedulePreview() {
  WT.seq++;                 // 让在途请求的结果作废，避免旧配置的画覆盖新输入
  clearTimeout(WT.timer);
  WT.timer = setTimeout(runWPreview, 380);
}

function payloadW(cfg) {
  const q = weightQ(WT.topic);
  const out = {};
  for (const [k, v] of Object.entries(cfg.weights)) {
    // 目标概率在接口里用 0-1；其余模式传原始权重，由后端归一化
    out[k] = cfg.mode === 'multi_target' ? (Number(v) || 0) / 100 : (Number(v) || 0);
  }
  return {
    mode: cfg.mode,
    weights: out,
    count: cfg.count == null ? null : cfg.count,
  };
}

function localWCheck(silent) {
  const q = weightQ(WT.topic);
  const cfg = currentDraft();
  if (!q || !cfg) return '没有选中的题目';
  const vals = Object.values(cfg.weights);
  if (vals.some((v) => !Number.isFinite(Number(v)) || Number(v) < 0)) {
    return '权重必须是不小于 0 的数字';
  }
  const sum = vals.reduce((a, b) => a + (Number(b) || 0), 0);
  if (sum <= 0) return '至少有一个选项的权重要大于 0';

  if (cfg.mode === 'multi_target') {
    if (vals.some((v) => Number(v) > 100)) return '目标出现概率不能超过 100%';
    const n = wtValues(q).length;
    const fixed = cfg.count || q.exact_select || null;
    if (fixed) {
      const target = Number(fixed) * 100;
      if (Math.abs(sum - target) > 0.5) {
        if (!silent) {
          showWMsg(`固定选 ${fixed} 项时，目标和应等于 ${target}%（当前 ${sum.toFixed(0)}%）。`
            + '保存时会自动归一化到该值。', 'warn');
        }
      }
    } else {
      const lo = Math.max(1, q.min_select || 1);
      const hi = Math.min(q.max_select || n, n);
      if (sum < lo * 100 - 0.5 || sum > hi * 100 + 0.5) {
        if (!silent) {
          showWMsg(`题干允许选 ${lo}-${hi} 项，目标和应在 ${lo * 100}%-${hi * 100}% 之间。`
            + '保存时会自动缩放到边界。', 'warn');
        }
      }
    }
  }
  return '';
}

function showWMsg(msg, level = 'err') {
  const el = $('#wMsg');
  if (!el) return;
  el.hidden = !msg;
  el.textContent = msg || '';
  el.className = 'w-msg' + (level === 'warn' ? ' warn' : '');
}

async function saveWQ() {
  const q = weightQ(WT.topic);
  const cfg = currentDraft();
  if (!q || !cfg) return;
  const bad = localWCheck(true);
  if (bad) { showWMsg(bad, 'err'); return; }

  const btn = $('#wSave');
  btn.disabled = true;
  try {
    const r = await post('/api/weights', {
      action: 'set',
      round: WT.round,
      topic: q.topic,
      config: payloadW(cfg),
    });
    if (!r.ok) {
      const msg = String(r.error || '');
      if (msg.includes('not found')) {
        WT.backendOld = true;
        showWMsg('后端没有 /api/weights，当前进程还是旧版。关闭程序重启后再试。', 'err');
        return;
      }
      showWMsg((r.errors && r.errors.length ? r.errors.join('；') : msg) || '保存失败', 'err');
      return;
    }
    WT.rounds = r.rounds || {};
    WT.sig = r.sig || '';
    WT.stats = {
      pairs: r.pairs || 0,
      configured_rounds: r.configured_rounds || [],
      questions: r.questions || [],
    };
    WT.stale = false;
    WT.draft = null;
    WT.dirty = false;
    WT.preview = null;
    updateWBadge();
    renderWQList();
    renderWEditor();
    renderWStat();
    schedulePreview();
    const warns = r.warnings || [];
    toast(warns.length ? `已保存 · ${warns[0]}` : `第 ${WT.round} 轮 Q${q.topic} 概率已保存`);
  } finally {
    btn.disabled = false;
  }
}

async function clearWQ() {
  const q = weightQ(WT.topic);
  if (!q) return;
  if (!window.confirm(`删除第 ${WT.round} 轮 Q${q.topic} 的概率配置？`)) return;
  const r = await post('/api/weights', {
    action: 'clear', round: WT.round, topic: q.topic,
  });
  if (!r.ok) { showWMsg(r.error || '清除失败', 'err'); return; }
  applyWStore(r);
  toast(`第 ${WT.round} 轮 Q${q.topic} 已恢复默认均匀`);
}

async function clearWRound() {
  if (!window.confirm(`清空第 ${WT.round} 轮的全部概率配置？`)) return;
  const r = await post('/api/weights', { action: 'clear', round: WT.round });
  if (!r.ok) { toast(r.error || '清除失败', true); return; }
  applyWStore(r);
  toast(`第 ${WT.round} 轮的概率配置已清空`);
}

async function copyWNext() {
  const target = Math.min(999, WT.round + 1);
  const r = await post('/api/weights', {
    action: 'copy', from: WT.round, to: [target],
  });
  if (!r.ok) { toast(r.error || '复制失败', true); return; }
  applyWStore(r);
  WT.round = target;
  $('#wRound').value = target;
  WT.draft = null;
  WT.dirty = false;
  renderWQList(); renderWEditor(); renderWStat();
  toast(`已复制到第 ${target} 轮`);
}

async function copyWAll() {
  const n = maxRound();
  if (!window.confirm(`用第 ${WT.round} 轮的配置覆盖第 1-${n} 轮？各轮之后仍是独立配置。`)) return;
  const r = await post('/api/weights', {
    action: 'copy', from: WT.round, to: 'all', count: n,
  });
  if (!r.ok) { toast(r.error || '复制失败', true); return; }
  applyWStore(r);
  toast(`已套用到第 1-${n} 轮（各轮仍是独立配置）`);
}

async function copyWQuestionAll() {
  const q = weightQ(WT.topic);
  if (!q) return;
  if (WT.dirty) { toast('先保存本题，再复制到其他轮次', true); return; }
  const n = maxRound();
  if (!window.confirm(`把第 ${WT.round} 轮 Q${q.topic} 的配置复制到第 1-${n} 轮？`
    + '其他题目不受影响。')) return;
  const r = await post('/api/weights', {
    action: 'copy', from: WT.round, topic: q.topic, to: 'all', count: n,
  });
  if (!r.ok) { toast(r.error || '复制失败', true); return; }
  applyWStore(r);
  toast(`Q${q.topic} 已复制到第 1-${n} 轮`);
}

function applyWStore(r) {
  WT.rounds = r.rounds || {};
  WT.sig = r.sig || '';
  WT.stale = false;
  WT.stats = {
    pairs: r.pairs || 0,
    configured_rounds: r.configured_rounds || [],
    questions: r.questions || [],
  };
  WT.draft = null;
  WT.dirty = false;
  WT.preview = null;
  updateWBadge();
  renderWQList();
  renderWEditor();
  renderWStat();
  schedulePreview();
}

function setWUniform() {
  const q = weightQ(WT.topic);
  const cfg = currentDraft();
  if (!q || !cfg) return;
  const vals = wtValues(q);
  let each;
  if (cfg.mode === 'multi_target') {
    each = uniformMultiPct(q, vals.length);
  } else {
    each = +(100 / vals.length).toFixed(2);
  }
  vals.forEach((v) => { cfg.weights[v.value] = each; });
  WT.dirty = true;
  WT.preview = null;
  renderWEditor();
  schedulePreview();
}

function updateWStatPreview() {
  const line = $('#wPreview');
  if (line) line.textContent = '改动后自动实测 600 次，用于核对实际分布';
}

function changeWRound() {
  if (WT.dirty && !window.confirm('当前题目有未保存的修改，切换轮次会丢弃。继续？')) {
    $('#wRound').value = WT.round;
    return;
  }
  let n = parseInt($('#wRound').value, 10);
  if (!Number.isFinite(n) || n < 1) n = 1;
  n = Math.min(999, n);
  WT.round = n;
  $('#wRound').value = n;
  WT.draft = null;
  WT.dirty = false;
  WT.preview = null;
  // 切轮次时保持当前题：用户通常是在同一道题上比较不同轮的配置，
  // 每切一次就跳到「本轮第一道已配置题」会让人找不到刚才在改什么
  if (!weightQ(WT.topic)) {
    const conf = roundCfg(n);
    const first = Object.keys(conf).filter((t) => weightQ(t))[0];
    WT.topic = first || (weightQuestions()[0] ? String(weightQuestions()[0].topic) : null);
  }
  renderWQList();
  renderWEditor();
  renderWStat();
  schedulePreview();
}

function initWeights() {
  if (!$('#btnWeights')) return;
  $('#btnWeights').addEventListener('click', openWeights);
  $('#wClose').addEventListener('click', closeWeights);
  $('#wMask').addEventListener('click', closeWeights);
  $('#wSave').addEventListener('click', saveWQ);
  $('#wUniform').addEventListener('click', setWUniform);
  $('#wRestore').addEventListener('click', clearWQ);
  $('#wClearRound').addEventListener('click', clearWRound);
  $('#wCopyNext').addEventListener('click', copyWNext);
  $('#wCopyAll').addEventListener('click', copyWAll);
  $('#wRound').addEventListener('change', changeWRound);
  document.addEventListener('keydown', (e) => {
    const d = $('#wDrawer');
    if (e.key === 'Escape' && d && !d.hidden) closeWeights();
  });
}

/* ---------------------------------------------------------------- 开始 / 停止 */
let qrBase64 = '';

// 选择题是否每轮重抽。存 localStorage，刷新后保持上次选择。
let choiceRandom = localStorage.getItem('wjx.choiceRandom') === '1';

function initRandSwitch() {
  const box = $('#randEach');
  if (!box) return;
  box.checked = choiceRandom;
  syncRandTag();
  box.addEventListener('change', () => {
    choiceRandom = box.checked;
    localStorage.setItem('wjx.choiceRandom', choiceRandom ? '1' : '0');
    syncRandTag();
    if (choiceRandom) {
      toast('选择题每轮重新随机，答题区的勾选不再作为提交内容');
    } else {
      toast('已关闭：每轮都用答题区当前的答案');
    }
  });
}

function syncRandTag() {
  const tag = $('#randTag');
  if (tag) tag.hidden = !choiceRandom;
}

async function startRun() {
  const shortid = $('#shortid').value.trim();
  if (!shortid && !qrBase64) {
    toast('先填短链，或上传一张二维码图片', true);
    return;
  }

  // 界面上的答案就是提交用的答案，后端只在缺题时补齐
  const answers = collectAnswers();
  const total = state.survey ? state.survey.questions.length : 0;

  // 必答填空没有来源时先拦住：提交后再报「Q5 必答但未提供」太晚了
  const miss = missingRequiredFill();
  if (miss.length) {
    const q = miss[0];
    toast(`Q${q.topic}「${q.prompt}」是必答填空，请直接填写或配到填空题模板里`, true);
    const card = document.querySelector(`.q[data-topic="${q.topic}"]`);
    if (card) card.scrollIntoView({ block: 'center', behavior: 'smooth' });
    return;
  }

  const body = {
    shortid: qrBase64 ? '' : shortid,
    qr_base64: qrBase64,
    answers,
    survey_sig: state.sig,   // 后端据此判断答案是否属于这份问卷
    count: parseInt($('#bcount').value, 10) || 1,
    gap: parseInt($('#bgap').value, 10) || 0,
    proxy: ($('#proxy').value || '').trim(),
    engine: 'browser',
    headless: false,        // 无头模式验证码几乎必弹，且人工无法接管
    choice_random: choiceRandom,   // 选择题每轮重抽
  };

  const r = await post('/api/quick/start', body);
  if (!r.ok) {
    toast(r.error || '启动失败', true);
    return;
  }
  if (total && Object.keys(answers).length < total) {
    toast(`已启动 · 缺 ${total - Object.keys(answers).length} 题将由脚本补齐`);
  } else {
    toast('已启动，浏览器窗口稍后弹出');
  }
  renderDock(buildView({ running: true }, { running: true, phase: 'opening', current: 0 }));
}

async function stopRun() {
  const r = await post('/api/quick/stop');
  if (!r.ok) {
    // 后端说没在跑，说明本地状态比后端旧（任务已异常结束），
    // 先把两个状态源重拉一遍并重画，别让按钮一直卡在「运行中…」
    toast(r.error || '停止失败', true);
    await pollQuick();
    await pollBrowser();
    tick();
    return;
  }
  if (r.cleaned) toast('已清理残留状态，可以重新开始');
  await pollQuick();
  await pollBrowser();
  tick();
}

/* ---------------------------------------------------------------- 状态合成 */
const PHASE_TEXT = {
  idle: '未开始',
  opening: '正在打开浏览器',
  filling: '逐题填写中',
  submitting: '提交中',
  captcha: '等待你点验证',
  waiting: '轮间隔等待',
  done: '本轮完成',
};

// quick 阶段的文案：浏览器还没开始动的时候，这两个阶段的进展靠它体现
const STEP_TEXT = {
  env: '检查运行环境',
  parse: '解析问卷',
  answer: '生成答案',
  submit: '提交答卷',
};

/** 把 quick 与 browser 两路状态合成单一视图，前端只消费这个结果 */
function buildView(q, b) {
  const surveyTotal = state.survey ? state.survey.questions.length : 0;
  const total = b.total || surveyTotal || 0;
  const busy = !!(q.running || b.running);

  let phase = 'idle';
  let text = '未开始';
  let now = '勾选答案后点「开始作答」';
  let pct = 0;
  let round = '—';

  if (b.captcha) {
    phase = 'captcha';
    text = b.captcha_mode === 'auto' ? '验证码拦截' : '等待你点验证';
    now = b.captcha_mode === 'auto'
      ? '脚本正在尝试自动通过，若停在弹窗请到窗口点一下'
      : '在浏览器窗口里点一下「智能验证」，脚本会自动继续';
    pct = 100;
  } else if (['opening', 'filling', 'submitting', 'waiting', 'done'].includes(b.phase)) {
    phase = b.phase;
    text = PHASE_TEXT[b.phase];
    if (b.phase === 'filling') {
      pct = total ? Math.min(100, (b.current / total) * 100) : 0;
      now = b.current ? `第 ${b.current}/${total} 题` : '准备开始填写';
      if (state.survey && b.current) {
        const item = state.survey.questions.find((x) => x.topic === b.current);
        if (item) now = `Q${b.current} · ${item.prompt.replace(/\s+/g, ' ').slice(0, 36)}`;
      }
    } else if (b.phase === 'submitting') {
      pct = 100;
      now = '已填完，正在提交';
    } else if (b.phase === 'opening') {
      now = '正在加载页面…';
    } else if (b.phase === 'waiting') {
      pct = 100;
      now = '等待下一轮开始';
    } else if (b.phase === 'done') {
      pct = 100;
      const res = q.result;
      now = res
        ? `共 ${total} 题 · 成功 ${res.success || 0} · 失败 ${res.fail || 0}`
        : `共 ${total} 题已提交`;
    }
  } else if (q.running) {
    // 浏览器尚未启动：显示当前正在做的准备步骤
    const cur = (q.steps || []).find((s) => s.state === 'running');
    const failed = (q.steps || []).find((s) => s.state === 'fail');
    if (failed) {
      phase = 'fail';
      text = '失败';
      now = `${STEP_TEXT[failed.key] || failed.label}：${failed.detail || '未通过'}`;
    } else if (cur) {
      phase = 'prep';
      text = STEP_TEXT[cur.key] || cur.label || '准备中';
      now = cur.detail || '…';
    }
  } else if (q.result && q.result.ok === false) {
    if (q.result.stopped) {
      phase = 'stopped';
      text = '已停止';
      now = '上一轮在提交前被中断，没有产生答卷';
    } else {
      phase = 'fail';
      text = '失败';
      now = q.result.error || '运行未成功';
    }
  }

  if (b.rounds) round = `${b.round || 0}/${b.rounds}`;

  return {
    phase, text, now, pct, round, busy,
    captchaMode: b.captcha_mode || '',
    cls: ['captcha', 'fail'].includes(phase) ? 'warn'
      : (phase === 'done' ? 'done'
        : (busy ? 'live' : '')),
    liveTopic: (phase === 'filling' && b.current) ? b.current : 0,
  };
}

/* ---------------------------------------------------------------- 渲染控制坞 */
function renderDock(v) {
  const dock = $('#dock');
  dock.classList.remove('live', 'warn', 'done');
  if (v.cls) dock.classList.add(v.cls);

  $('#bphase').textContent = v.text;
  const nowEl = $('#bnow');
  nowEl.textContent = v.now;
  nowEl.title = v.now;      // 错误信息较长时靠 hover 看全文
  $('#bround').textContent = v.round;
  $('#bfill').style.width = (v.pct || 0) + '%';

  const cap = $('#bcap');
  cap.hidden = v.phase !== 'captcha';
  if (v.phase === 'captcha') {
    cap.textContent = v.captchaMode === 'auto'
      ? '脚本正在自动尝试通过验证码，若弹窗一直不走，请到浏览器窗口里点一下'
      : '需要你在浏览器窗口里点一下「智能验证」，点完脚本自动继续';
  }

  // 按钮与参数：运行中唯一一套启停，不再散落在别处
  const bs = $('#btnBStart');
  const bp = $('#btnBStop');
  bs.disabled = v.busy;
  bs.textContent = v.busy ? '运行中…' : '开始作答';
  bp.disabled = !v.busy;
  ['#bcount', '#bgap', '#proxy', '#randEach'].forEach((sel) => {
    const el = $(sel);
    if (el) el.disabled = v.busy;
  });

  // 工具按钮同样锁住，避免「改了却不生效」的错觉
  $$('[data-fill], #btnImport, #btnExport, #btnFetch, #btnFillTpl, #btnWeights').forEach((b) => {
    b.disabled = v.busy;
  });

  // 答题区锁定 + 高亮正在填的那道题
  $('#questions').classList.toggle('locked', v.busy);
  highlightTopic(v.liveTopic);

  if (v.busy !== state.busy) {
    state.busy = v.busy;
    if (!v.busy) $('#bcap').hidden = true;
  }
  state.phase = v.phase;
}

/** 高亮 = 联动核心：控制坞的「当前题」和答题区的题干是同一道，两边同时指向 */
function highlightTopic(topic) {
  if (topic === state.liveTopic) return;
  state.liveTopic = topic;

  $$('.q.live').forEach((el) => el.classList.remove('live'));
  if (!topic) return;

  const el = document.querySelector(`.q[data-topic="${topic}"]`);
  if (!el) return;
  el.classList.add('live');

  // 只在题目不在视口里时才滚，避免每一步都抢走用户的滚动位置
  const box = $('#questions');
  if (getComputedStyle(box).overflowY !== 'auto') return;
  const r = el.getBoundingClientRect();
  const br = box.getBoundingClientRect();
  if (r.top < br.top + 8 || r.bottom > br.bottom - 8) {
    el.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }
}

/* ---------------------------------------------------------------- 轮询 */
let lastQ = { running: false, steps: [], result: null };
let lastB = { running: false, phase: 'idle', current: 0, total: 0, round: 0, rounds: 0, captcha: false };
let quickWasRunning = false;

async function pollQuick() {
  try {
    const d = await api('/api/quick/status');
    if (!d.ok) return;

    // 只在「运行 → 结束」那一刻处理一次，否则轮间隔期间会反复覆盖
    const ended = quickWasRunning && !d.running;
    quickWasRunning = !!d.running;
    lastQ = d;

    if (ended && d.result) {
      const res = d.result;
      if (res.sig && state.sig && res.sig !== state.sig) {
        // 后端解析出的问卷和界面不是同一份：旧答案已经作废，重建界面
        await loadSurvey();
        await loadTpl();
        await loadWeights();
        toast('问卷已更新为后端解析的版本，请重新确认答案');
      } else if (res.answers) {
        // 同一份问卷：把后端补齐过的完整答案写回界面，
        // 保证「界面显示的就是刚提交的」
        if (!state.survey) await loadSurvey();
        if (state.survey) applyAnswers(res.answers);
      }
    }
  } catch (e) { /* 服务重启时忽略 */ }
}

async function pollBrowser() {
  try {
    const d = await api('/api/browser/status');
    if (!d.ok) return;
    lastB = d;
  } catch (e) { /* 忽略 */ }
}

async function tick() {
  const prevPhase = state.phase;
  const wasCaptcha = prevPhase === 'captcha';
  const v = buildView(lastQ, lastB);
  renderDock(v);
  if (v.phase === 'captcha' && !wasCaptcha) {
    toast(v.captchaMode === 'auto'
      ? '提交被验证码拦下，脚本正在尝试自动通过'
      : '需要你在浏览器窗口里点一下验证');
  }
  // 从运行态掉到失败：主动弹一次，否则错误只躺在状态栏里没人注意
  if (v.phase === 'fail' && !['fail', 'stopped'].includes(prevPhase)) {
    toast(v.now, true);
  }
}

/* 日志：唯一还依赖 /api/batch/status 的东西 */
async function pollLog() {
  try {
    const r = await api(`/api/batch/status?since=${state.logSeq}`);
    if (!r.ok) return;
    if (r.logs && r.logs.length) {
      const box = $('#log');
      const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
      const frag = document.createDocumentFragment();
      for (const item of r.logs) {
        const div = document.createElement('div');
        div.innerHTML = `<span class="t">${item.t}</span><span class="${item.level}">${esc(item.msg)}</span>`;
        frag.appendChild(div);
        state.logSeq = item.id;
      }
      box.appendChild(frag);
      while (box.childNodes.length > 400) box.removeChild(box.firstChild);
      if (atBottom) box.scrollTop = box.scrollHeight;
    }
  } catch (e) { /* 忽略 */ }
}

/* ---------------------------------------------------------------- 加载问卷 */
async function loadSurvey() {
  const r = await api('/api/survey');
  if (!r.ok) return;
  if (r.shortid) $('#shortid').value = r.shortid;
  if (r.survey) {
    state.survey = r.survey;
    state.sig = r.sig || '';
    renderSurvey(r.survey);
    $('#surveyMeta').textContent =
      `${r.survey.title} · ${r.survey.question_count} 题 · 必答 ${r.survey.required_count}`;
  }
}

async function fetchSurvey() {
  const shortid = $('#shortid').value.trim();
  if (!shortid) { toast('先填短链', true); return; }
  const btn = $('#btnFetch');
  btn.disabled = true;
  btn.textContent = '抓取中…';
  try {
    const r = await post('/api/survey', { shortid, proxy: ($('#proxy').value || '').trim() });
    if (!r.ok) { toast(r.error || '抓取失败', true); return; }
    state.survey = r.survey;
    renderSurvey(r.survey);
    $('#surveyMeta').textContent =
      `${r.survey.title} · ${r.survey.question_count} 题 · 必答 ${r.survey.required_count}`;
    await loadSurvey();   // 补上 sig
    await loadTpl();      // 模板跟着问卷刷新，换了问卷会标成 stale
    await loadWeights();  // 概率配置同理
    toast('解析完成，可以开始作答');
  } finally {
    btn.disabled = false;
    btn.textContent = '抓取并解析';
  }
}

/* ---------------------------------------------------------------- 绑定 */
$('#btnFetch').addEventListener('click', fetchSurvey);
$('#shortid').addEventListener('keydown', (e) => { if (e.key === 'Enter') fetchSurvey(); });
$('#btnBStart').addEventListener('click', startRun);
$('#btnBStop').addEventListener('click', stopRun);
$('#btnExport').addEventListener('click', exportAnswers);
$('#btnExportText').addEventListener('click', exportSurveyText);
$('#txtClose').addEventListener('click', closeSurveyText);
$('#txtCopy').addEventListener('click', copySurveyText);
$('#txtRefresh').addEventListener('click', exportSurveyText);
$('#txtMask').addEventListener('click', (e) => {
  if (e.target.id === 'txtMask') closeSurveyText();
});
$('#btnImport').addEventListener('click', () => $('#fileImport').click());
$('#fileImport').addEventListener('change', (e) => {
  if (e.target.files[0]) importAnswers(e.target.files[0]);
  e.target.value = '';
});
$('#btnClearLog').addEventListener('click', () => { $('#log').innerHTML = ''; });
$$('[data-fill]').forEach((b) => {
  b.addEventListener('click', () => fill(b.dataset.fill));
});

// 填空题模板抽屉
$('#btnFillTpl').addEventListener('click', openTpl);
$('#tplClose').addEventListener('click', closeTpl);
$('#tplMask').addEventListener('click', closeTpl);
$('#tplSave').addEventListener('click', saveTpl);
$('#tplRandom').addEventListener('change', (e) => setTplModeHint(e.target.checked));
document.addEventListener('keydown', (e) => {
  const d = $('#tplDrawer');
  if (e.key === 'Escape' && d && !d.hidden) closeTpl();
});

// 二维码：上传后直接顶掉短链，等价于「用图里的链接」
$('#qrFile').addEventListener('change', (e) => {
  const f = e.target.files[0];
  if (!f) return;
  if (f.size > 6 * 1024 * 1024) { toast('图片不要超过 6MB', true); return; }
  const fr = new FileReader();
  fr.onload = () => {
    qrBase64 = fr.result;
    $('#qrName').textContent = f.name.length > 8 ? f.name.slice(0, 7) + '…' : f.name;
    $('#qrLabel').classList.add('has');
    $('#qrIcon').textContent = '✓';
    toast('二维码就绪，点「开始作答」会自动识别');
  };
  fr.readAsDataURL(f);
});

/* ---------------------------------------------------------------- 启动弹窗 */
/* 每次打开页面都弹：不写 localStorage / sessionStorage，刷新即重现。
   作者与授权文案以 /api/about 为准，接口取不到时回落到页面里的静态文本。 */
function showAbout() {
  const mask = $('#aboutMask');
  if (!mask) return;
  mask.classList.add('show');
  const btn = $('#aboutOk');
  if (btn) { try { btn.focus(); } catch (e) { /* 无焦点不致命 */ } }
}

function closeAbout() {
  const mask = $('#aboutMask');
  if (!mask || !mask.classList.contains('show')) return;
  mask.classList.remove('show');
}

async function fillAbout() {
  try {
    const a = await api('/api/about');
    if (!a || !a.ok) return;
    if (a.author) $('#aboutAuthor').textContent = a.author;
    const sub = $('#aboutSub');
    if (sub && a.version) sub.textContent = `本地运行 · 浏览器作答 · v${a.version}`;
    // notice 已经在上面三行里分成「授权 / 限制」展示，这里不再重复拼进去
    if (a.notice) $('#aboutMask').dataset.notice = a.notice;
  } catch (e) { /* 静态文案已够用 */ }
}

if ($('#aboutOk')) $('#aboutOk').addEventListener('click', closeAbout);
if ($('#aboutMask')) {
  $('#aboutMask').addEventListener('click', (e) => {
    if (e.target.id === 'aboutMask') closeAbout();   // 点遮罩空白处也能关
  });
}
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeAbout();
  if (e.key === 'Escape') closeSurveyText();
});

initRandSwitch();
initWeights();
showAbout();
fillAbout();
loadSurvey().then(() => { tick(); loadTpl(); loadWeights(); });
setInterval(pollQuick, 700);
setInterval(pollBrowser, 500);
setInterval(tick, 500);
setInterval(pollLog, 900);
