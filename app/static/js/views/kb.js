// 知识库管理（KB_ADMIN）：版本列表与状态、上传、导入进度与失败原因、发布与回滚、版本详情（校验结果 / 文档 / 审计）。
// 发布与回滚带上页面上看到的生效版本号（比较交换）：若别人已先改了生效版本，后端返回 409，这里提示并刷新，不会静默覆盖。
// 按钮是否出现只是体验层；后端每个接口都独立校验 KB_ADMIN 角色与版本状态。

import { api, ApiError } from '../api.js';
import { clear, fmtTime, h, notice } from '../dom.js';

export const KB_STATUS_LABEL = {
  DRAFT: '草稿', INGESTING: '导入中', READY: '待发布', FAILED: '失败', ACTIVE: '生效中', RETIRED: '已退役',
};
const SOURCE_LABEL = { MIGRATION: '迁移归档', BOOTSTRAP: '启动初始化', UPLOAD: '管理员上传', EVAL: '评测导入' };
const AUDIT_LABEL = {
  CREATED: '创建草稿',
  INGEST_STARTED: '开始导入',
  INGEST_READY: '导入完成，校验通过',
  INGEST_FAILED: '导入失败',
  ACTIVATED: '发布生效',
  RETIRED: '被替换，退役',
  ROLLED_BACK: '回滚生效',
  RECOVERED_AFTER_RESTART: '服务重启时导入未完成，标记失败',
  MIGRATED: '迁移归档为初始版本',
};
const IN_PROGRESS = new Set(['DRAFT', 'INGESTING']);
const POLL_MS = 1000;

const versionHref = (id) => `#/kb/versions/${id}`;
const actorLabel = (actor) => (actor === 'system' ? '系统' : actor);

export function kbStatusPill(status) {
  return h('span', { class: `pill pill-kb-${status.toLowerCase()}`, 'data-testid': 'kb-status', dataset: { status } },
    KB_STATUS_LABEL[status] || status);
}

function contentText(v) {
  if (IN_PROGRESS.has(v.status)) return `已处理 ${v.progress_done}/${v.progress_total} 篇`;
  return v.chunk_count == null ? `${v.doc_count} 篇` : `${v.doc_count} 篇 / ${v.chunk_count} 段`;
}

function uploadErrors(err) {
  const errors = err instanceof ApiError && err.detail && Array.isArray(err.detail.errors) ? err.detail.errors : [];
  if (!errors.length) return notice('error', `上传失败：${err.message}`);
  return h('div', { class: 'notice notice-error', role: 'alert' },
    h('p', { class: 'kb-errors-title' }, '上传未通过校验，没有创建任何版本：'),
    h('ul', { class: 'kb-errors' }, errors.map((e) => h('li', {
      'data-testid': 'kb-upload-error', dataset: { file: e.file, check: e.check },
    }, h('span', { class: 'mono' }, e.file === '*' ? '整体' : e.file), `：${e.message}`))));
}

function uploadPanel(onCreated) {
  const input = h('input', {
    type: 'file', multiple: true, accept: '.md,text/markdown', 'data-testid': 'kb-files', 'aria-label': '选择 .md 文件',
  });
  const mode = h('select', { 'data-testid': 'kb-mode', 'aria-label': '上传方式' },
    h('option', { value: 'merge' }, '合并到当前生效版本（按 document_id 覆盖或新增）'),
    h('option', { value: 'replace' }, '完整替换（新版本只包含本次上传的文件）'));
  const result = h('div', { class: 'kb-upload-result' });
  const submit = h('button', { class: 'btn btn-primary', type: 'submit', 'data-testid': 'kb-upload' }, '上传并导入');
  return h('form', {
    class: 'panel',
    onsubmit: async (e) => {
      e.preventDefault();
      clear(result);
      if (!input.files.length) {
        clear(result, notice('error', '请先选择至少一个 .md 文件'));
        return;
      }
      const form = new FormData();
      for (const f of input.files) form.append('files', f);
      form.append('mode', mode.value);
      submit.disabled = true;
      try {
        const v = await api('/api/kb/versions', { method: 'POST', body: form });
        input.value = '';
        clear(result, h('div', { class: 'notice', role: 'status' }, `已创建 v${v.id}，正在后台导入与校验…`));
        onCreated();
      } catch (err) {
        clear(result, uploadErrors(err));
      } finally {
        submit.disabled = false;
      }
    },
  },
  h('h2', {}, '上传新版本'),
  h('p', { class: 'muted small' },
    '只接受 UTF-8 编码、带 front matter（document_id、document_name、category、policy_version）的 .md 文件，'
    + '单个不超过 200 KB，一次最多 50 个。导入在后台进行，并自动检查 chunk 数量、向量维度和冒烟查询；'
    + '全部通过才会变成「待发布」，发布前不会被客户检索到。'),
  h('div', { class: 'kb-upload-row' }, input, mode, submit),
  result);
}

export async function renderKbVersions(main, { isStale }) {
  const flash = h('div', { class: 'flash' });
  const banner = h('div', { class: 'kb-banner', 'data-testid': 'kb-active' });
  const tableSlot = h('div', {}, h('p', { class: 'muted' }, '加载中…'));
  let timer = null;
  let activeId = null;

  async function act(button, v, kind) {
    button.disabled = true;
    const path = `/api/kb/versions/${v.id}/${kind === 'rollback' ? 'rollback' : 'activate'}`;
    try {
      await api(path, { method: 'POST', body: { expected_active_version_id: activeId } });
      clear(flash, h('div', { class: 'notice notice-success', role: 'status' },
        kind === 'rollback' ? `已回滚：v${v.id} 重新生效` : `v${v.id} 已发布生效`));
    } catch (err) {
      clear(flash, notice('error', `${kind === 'rollback' ? '回滚' : '发布'}失败：${err.message}`));
    }
    await refresh();
  }

  function actionFor(v) {
    if (v.status === 'READY') {
      return h('button', { class: 'btn btn-primary btn-sm', type: 'button', 'data-testid': 'kb-activate',
        onclick: (e) => act(e.target, v, 'activate') }, '发布');
    }
    if (v.status === 'RETIRED') {
      return h('button', { class: 'btn btn-sm', type: 'button', 'data-testid': 'kb-rollback',
        onclick: (e) => act(e.target, v, 'rollback') }, '回滚到此版本');
    }
    return v.status === 'ACTIVE' ? h('span', { class: 'muted small' }, '当前生效') : null;
  }

  function row(v) {
    const remark = v.status === 'FAILED'
      ? h('span', { class: 'kb-failure', 'data-testid': 'kb-failure', title: v.failure_reason || '' }, v.failure_reason || '失败')
      : h('span', { class: 'muted' }, v.note || '');
    return h('tr', { 'data-testid': 'kb-row', dataset: { versionId: String(v.id), status: v.status } },
      h('td', { class: 'mono' }, h('a', { href: versionHref(v.id) }, `v${v.id}`)),
      h('td', {}, kbStatusPill(v.status)),
      h('td', {}, SOURCE_LABEL[v.source] || v.source),
      h('td', { 'data-testid': 'kb-progress' }, contentText(v)),
      h('td', {}, actorLabel(v.created_by)),
      h('td', { class: 'num' }, fmtTime(v.created_at)),
      h('td', { class: 'kb-remark' }, remark),
      h('td', { class: 'row-actions' }, actionFor(v)));
  }

  async function refresh() {
    if (timer) clearTimeout(timer);
    let data;
    try {
      data = await api('/api/kb/versions');
    } catch (err) {
      if (!isStale()) clear(tableSlot, notice('error', `加载失败：${err.message}`));
      return;
    }
    if (isStale()) return;
    activeId = data.active_version_id;
    const active = data.versions.find((v) => v.id === activeId);
    clear(banner, active
      ? [h('span', {}, '当前生效：'), h('a', { href: versionHref(active.id), class: 'mono' }, `v${active.id}`),
        h('span', { class: 'muted' }, ` · ${SOURCE_LABEL[active.source] || active.source} · ${contentText(active)} · 生效于 ${fmtTime(active.activated_at)}`)]
      : h('span', { class: 'kb-failure' }, '当前没有生效版本：客户提问将全部拒答'));
    clear(tableSlot, data.versions.length
      ? h('table', { class: 'table', 'data-testid': 'kb-table' },
        h('thead', {}, h('tr', {}, ['版本', '状态', '来源', '内容', '创建人', '创建时间', '说明 / 失败原因', ''].map((t, i) =>
          h('th', { class: i === 5 ? 'num' : '' }, t)))),
        h('tbody', {}, data.versions.map(row)))
      : h('p', { class: 'empty' }, '还没有任何知识库版本。'));
    // 有版本在导入中：定时刷新进度；离开本页（isStale）即停止
    if (data.versions.some((v) => IN_PROGRESS.has(v.status))) timer = setTimeout(() => { if (!isStale()) refresh(); }, POLL_MS);
  }

  clear(main, h('div', { class: 'page' },
    h('div', { class: 'page-head' }, h('h1', {}, '知识库版本')),
    banner,
    uploadPanel(() => refresh()),
    h('section', { class: 'kb-list' }, flash, tableSlot)));
  await refresh();
}

function checksPanel(checks) {
  if (!checks) return h('p', { class: 'muted' }, '尚无校验结果。');
  const items = [];
  if (checks.embedding_dim) {
    const d = checks.embedding_dim;
    items.push(h('li', {}, `${d.passed ? '通过' : '未通过'} · 向量维度：要求 ${d.expected}，实际 ${d.actual}`));
  }
  if (checks.chunk_count) {
    const c = checks.chunk_count;
    items.push(h('li', {}, `${c.passed ? '通过' : '未通过'} · chunk 数量：${c.value}（至少 ${c.min}）`));
  }
  const smoke = checks.smoke;
  return h('div', {},
    h('ul', { class: 'kb-checks' }, items),
    smoke ? h('table', { class: 'table', 'data-testid': 'kb-smoke' },
      h('thead', {}, h('tr', {}, ['冒烟查询', '期望命中', '结果', `top${smoke.top_k}（阈值 ${smoke.threshold}）`].map((t) => h('th', {}, t)))),
      h('tbody', {}, smoke.results.map((r) => h('tr', { dataset: { passed: String(r.passed) } },
        h('td', {}, r.query),
        h('td', { class: 'mono' }, r.expect_document_id),
        h('td', {}, r.passed ? '命中' : h('span', { class: 'kb-failure' }, '未命中')),
        h('td', { class: 'small mono' }, r.top.map((t) => `${t.document_id} ${t.score.toFixed(3)}`).join('、') || '无结果'))))) : null);
}

export async function renderKbVersionDetail(main, { params, isStale }) {
  const id = params[0];
  clear(main, h('div', { class: 'page' }, h('p', { class: 'muted' }, '加载中…')));
  let v;
  try {
    v = await api(`/api/kb/versions/${encodeURIComponent(id)}`);
  } catch (err) {
    if (!isStale()) {
      clear(main, h('div', { class: 'page' }, notice('error', err.status === 404 ? '版本不存在。' : `加载失败：${err.message}`),
        h('p', {}, h('a', { href: '#/kb' }, '返回版本列表'))));
    }
    return;
  }
  if (isStale()) return;
  const meta = [
    ['来源', SOURCE_LABEL[v.source] || v.source],
    ['创建人', actorLabel(v.created_by)],
    ['创建时间', fmtTime(v.created_at)],
    ['导入完成', fmtTime(v.finished_at)],
    ['最近生效', fmtTime(v.activated_at)],
    ['内容', contentText(v)],
    ['向量后端', v.embedding_backend || '未知'],
    ['来源内容哈希', v.source_hash.slice(0, 16)],
  ];
  clear(main, h('div', { class: 'page narrow', 'data-testid': 'kb-detail', dataset: { versionId: String(v.id), status: v.status } },
    h('p', {}, h('a', { href: '#/kb' }, '← 知识库版本')),
    h('header', { class: 'detail-head' },
      h('div', { class: 'detail-title' }, h('h1', {}, `知识库版本 v${v.id}`), kbStatusPill(v.status)),
      v.note ? h('p', { class: 'description muted' }, v.note) : null,
      h('dl', { class: 'meta' }, meta.map(([k, val]) => h('div', {}, h('dt', {}, k), h('dd', {}, val))))),
    v.failure_reason ? h('div', { class: 'panel' }, h('h2', {}, '失败原因'),
      h('p', { class: 'kb-failure', 'data-testid': 'kb-failure' }, v.failure_reason)) : null,
    h('section', { class: 'panel' }, h('h2', {}, '发布前校验'), checksPanel(v.checks)),
    h('section', { class: 'panel' }, h('h2', {}, `文档（${v.documents.length}）`),
      v.documents.length
        ? h('table', { class: 'table' },
          h('thead', {}, h('tr', {}, ['路径', 'document_id', '名称', '内容哈希', '大小'].map((t, i) => h('th', { class: i === 4 ? 'num' : '' }, t)))),
          h('tbody', {}, v.documents.map((d) => h('tr', {},
            h('td', { class: 'mono' }, d.path), h('td', { class: 'mono' }, d.document_id), h('td', {}, d.document_name),
            h('td', { class: 'mono small' }, d.content_hash.slice(0, 12)), h('td', { class: 'num' }, `${d.size_bytes} B`)))))
        : h('p', { class: 'muted' }, '该版本没有保存文档原文（版本化之前迁移归档的数据）。')),
    h('section', { class: 'panel' }, h('h2', {}, '操作记录'),
      h('ol', { class: 'timeline' }, v.audit.map((a) => h('li', { class: 'tl-item', 'data-testid': 'kb-audit', dataset: { action: a.action } },
        h('div', { class: 'tl-line' },
          h('strong', {}, actorLabel(a.actor)),
          h('span', {}, AUDIT_LABEL[a.action] || a.action),
          a.from_status || a.to_status ? h('span', { class: 'muted small' },
            `${KB_STATUS_LABEL[a.from_status] || '—'} → ${KB_STATUS_LABEL[a.to_status] || '—'}`) : null,
          h('time', {}, fmtTime(a.created_at)))))))));
  if (IN_PROGRESS.has(v.status)) setTimeout(() => { if (!isStale()) renderKbVersionDetail(main, { params, isStale }); }, POLL_MS);
}
