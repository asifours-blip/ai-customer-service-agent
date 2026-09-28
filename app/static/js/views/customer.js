// 客户端视图：咨询（会话 + 聊天 + 依据 + 确认卡片）、我的订单、我的工单与详情（时间线 / 回复 / 反馈）。

import { api, ApiError } from '../api.js';
import {
  CATEGORY_LABEL,
  ORDER_STATUS_LABEL,
  clear,
  fmtTime,
  h,
  linkifyTickets,
  notice,
  statusPill,
} from '../dom.js';
import { KB_STATUS_LABEL } from './kb.js';
import { feedbackView, ticketHeader, timeline } from './timeline.js';

const EXAMPLES = [
  '这个耳机支持多久保修？',
  '帮我查一下订单 A10001',
  '订单 A10001 到哪了？',
  'A10001 买的耳机用了三天坏了，可以退款吗',
];

const ticketHref = (id) => `#/tickets/${encodeURIComponent(id)}`;

function newSessionId() {
  return `web-${crypto.getRandomValues(new Uint32Array(2)).join('').slice(0, 12)}`;
}

function loading(main) {
  clear(main, h('div', { class: 'page' }, h('p', { class: 'muted' }, '加载中…')));
}

function errorPage(main, err, what) {
  const text = err instanceof ApiError && err.status === 403
    ? `无权查看该${what}：只能查看本人的${what}。`
    : err instanceof ApiError && err.status === 404
      ? `${what}不存在。`
      : `加载失败：${err.message}`;
  clear(main, h('div', { class: 'page' },
    h('div', { 'data-testid': err.status === 403 ? 'access-denied' : 'load-error' }, notice('error', text)),
    h('p', {}, h('a', { href: '#/tickets' }, '返回我的工单'))));
}

// ---------------- 咨询 ----------------

// 引用来源所属的知识库版本（去重、按出现顺序）；版本化之前的旧回答没有 version_id
function sourceVersions(sources) {
  return [...new Set((sources || []).map((s) => s.version_id).filter(Boolean))];
}

function citationsEl(items) {
  return h('ol', { class: 'citations', 'data-testid': 'citations' }, items.map((c) => h('li', {
    'data-testid': 'citation', dataset: { versionId: c.version_id ?? '', found: String(c.found) },
  },
  h('div', { class: 'citation-head' },
    `《${c.document}》${c.section ? ` · ${c.section}` : ''}`,
    c.version_id ? h('span', { class: 'muted' },
      ` · v${c.version_id}${c.version_status ? `（${KB_STATUS_LABEL[c.version_status] || c.version_status}）` : ''}`) : null),
  c.found ? h('blockquote', {}, c.content) : h('p', { class: 'muted' }, c.reason))));
}

function sourcesEl(sources, traceId) {
  if (!sources || !sources.length) return null;
  const versions = sourceVersions(sources);
  const detail = h('div', { class: 'citations-slot', hidden: true });
  const toggle = traceId ? h('button', {
    class: 'btn btn-quiet btn-sm', type: 'button', 'data-testid': 'show-citations',
    onclick: async () => {
      if (!detail.hidden) {
        detail.hidden = true;
        return;
      }
      clear(detail, h('span', { class: 'muted' }, '加载中…'));
      detail.hidden = false;
      try {
        // 按 (version_id, chunk_id) 取回回答当时的原文：该版本被替换后仍能查到
        clear(detail, citationsEl(await api(`/api/traces/${encodeURIComponent(traceId)}/citations`)));
      } catch (err) {
        clear(detail, notice('error', `查询失败：${err.message}`));
      }
    },
  }, '查看引用原文') : null;
  return h('div', { class: 'sources', 'data-testid': 'sources' },
    h('span', { class: 'sources-label' }, '引用来源'),
    versions.length ? h('span', { 'data-testid': 'kb-version-note' },
      ` · 引用自知识库版本 ${versions.map((v) => `v${v}`).join('、')}`) : null,
    h('ul', {}, sources.map((s) => h('li', {}, `《${s.document}》${s.section ? ` · ${s.section}` : ''}`))),
    toggle,
    detail);
}

const TYPE_LABEL = { REFUND: '退款', EXCHANGE: '换货', REPAIR: '维修' };

function eligibilityEl(e) {
  if (!e) return null;
  const d = e.details || {};
  const rows = [
    ['订单', e.order_id],
    ['申请类型', TYPE_LABEL[e.request_type] || e.request_type],
    ['判定规则', e.policy_rule],
    ['判定结果', e.reason_code],
  ];
  if (d.delivered_days !== undefined) rows.push(['签收天数', `${d.delivered_days} 天（期限 ${d.allowed_days} 天）`]);
  if (d.order_status) rows.push(['订单状态', ORDER_STATUS_LABEL[d.order_status] || d.order_status]);
  return h('div', { class: `eligibility ${e.eligible ? 'is-ok' : 'is-no'}`, 'data-testid': 'eligibility' },
    h('div', { class: 'eligibility-head' },
      h('span', {}, '售后资格判定'),
      h('span', { class: `pill ${e.eligible ? 'pill-resolved' : 'pill-closed'}` }, e.eligible ? '符合' : '不符合')),
    h('dl', {}, rows.map(([k, v]) => h('div', {}, h('dt', {}, k), h('dd', {}, v ?? '—')))),
    h('p', { class: 'muted small' }, '由业务规则确定性计算，非模型生成。'));
}

// 回答方式：离线回显与模板回复必须明示，不能让用户以为是模型生成的
const ANSWER_MODE_LABEL = {
  MODEL: '模型生成',
  OFFLINE_ECHO: '离线演示：未调用模型，内容为知识库原文回显',
  TEMPLATE: '系统模板回复（非模型生成）',
  ERROR: '本轮未能生成回答',
};

function answerModeEl(mode) {
  if (!mode) return null;
  return h('div', { class: `answer-mode answer-mode-${mode.toLowerCase()}`, 'data-testid': 'answer-mode', dataset: { mode } },
    ANSWER_MODE_LABEL[mode] || mode);
}

function messageEl(role, content, extra = {}) {
  const body = role === 'assistant' ? linkifyTickets(content, ticketHref) : [content];
  const isError = role === 'assistant' && extra.answer_mode === 'ERROR';
  return h('div', { class: `msg msg-${role}${isError ? ' msg-failed' : ''}`, 'data-testid': `msg-${role}` },
    h('div', { class: 'msg-body' }, body),
    role === 'assistant' ? answerModeEl(extra.answer_mode) : null,
    role === 'assistant' ? sourcesEl(extra.sources, extra.trace_id) : null,
    role === 'assistant' ? eligibilityEl(extra.eligibility) : null);
}

// 聊天接口 503：后端给的是用户可读提示（模型未配置 / 限流 / 超时 / 知识库维护…），内部细节只在 Trace
function chatErrorEl(err) {
  const d = err instanceof ApiError ? err.detail : null;
  const known = d && d.category && d.message;
  const text = known ? d.message : err instanceof ApiError && err.status >= 500
    ? '服务暂时不可用，请稍后再试。'
    : `请求失败：${err.message}`;
  return h('div', {
    class: 'msg msg-error', 'data-testid': 'chat-error', dataset: { category: known ? d.category : '' },
  }, notice('error', text), known && d.trace_id ? h('p', { class: 'muted small' }, `Trace：${d.trace_id}`) : null);
}

function basisPanel(resp) {
  if (!resp) return [h('h2', {}, '本轮依据'), h('p', { class: 'muted' }, '发送消息后，这里显示本轮的路由、工具调用与 Trace。')];
  const traceOut = h('pre', { class: 'trace-json', hidden: true });
  return [
    h('h2', {}, '本轮依据'),
    h('dl', { class: 'kv' },
      h('div', {}, h('dt', {}, '路由'), h('dd', {}, resp.route || '—')),
      h('div', {}, h('dt', {}, '意图'), h('dd', {}, resp.intent || '—')),
      h('div', {}, h('dt', {}, '回答方式'), h('dd', { 'data-testid': 'basis-answer-mode' },
        ANSWER_MODE_LABEL[resp.answer_mode] || resp.answer_mode || '—')),
      h('div', {}, h('dt', {}, '耗时'), h('dd', { class: 'num' }, `${resp.latency_ms} ms`)),
      h('div', {}, h('dt', {}, '工具'), h('dd', {},
        resp.tool_calls.length ? resp.tool_calls.map((t) => `${t.tool}${t.ok ? '' : '（失败）'}`).join('、') : '无')),
      h('div', {}, h('dt', {}, '拒答'), h('dd', {}, resp.abstained ? '是' : '否')),
      h('div', {}, h('dt', {}, '知识库版本'), h('dd', { 'data-testid': 'basis-kb-version' },
        sourceVersions(resp.sources).map((v) => `v${v}`).join('、') || '—')),
      h('div', {}, h('dt', {}, 'Trace'), h('dd', { class: 'mono' }, resp.trace_id))),
    h('button', {
      class: 'btn btn-quiet',
      type: 'button',
      onclick: async () => {
        try {
          traceOut.textContent = JSON.stringify(await api(`/api/traces/${resp.trace_id}`), null, 2);
        } catch (err) {
          traceOut.textContent = `查询失败：${err.message}`;
        }
        traceOut.hidden = false;
      },
    }, '查看完整 Trace'),
    traceOut,
  ];
}

export async function renderChat(main, { params, isStale }) {
  let sid = params[0];
  loading(main);
  let conversations;
  try {
    conversations = await api('/api/conversations');
  } catch (err) {
    if (!isStale()) clear(main, h('div', { class: 'page' }, notice('error', `加载会话失败：${err.message}`)));
    return;
  }
  if (isStale()) return;
  if (!sid) {
    location.replace(`#/chat/${encodeURIComponent(conversations[0]?.session_id || newSessionId())}`);
    return;
  }
  const existing = conversations.find((c) => c.session_id === sid);
  let detail = null;
  if (existing) {
    try {
      detail = await api(`/api/conversations/${existing.id}`);
    } catch (err) {
      if (!isStale()) clear(main, h('div', { class: 'page' }, notice('error', `加载会话失败：${err.message}`)));
      return;
    }
    if (isStale()) return;
  }

  const list = h('div', { class: 'messages', 'data-testid': 'messages' });
  const cardSlot = h('div', { class: 'card-slot' });
  const scroller = h('div', { class: 'chat-scroll' }, list, cardSlot);
  const basis = h('aside', { class: 'basis' }, basisPanel(null));
  const input = h('textarea', {
    id: 'chat-input', rows: 2, placeholder: '描述您的问题，例如：订单 A10001 到哪了…', 'aria-label': '消息',
  });
  const sendBtn = h('button', { class: 'btn btn-primary', type: 'submit', 'data-testid': 'send' }, '发送');
  let busy = false;

  const scrollDown = () => { scroller.scrollTop = scroller.scrollHeight; };
  const setBusy = (value) => {
    busy = value;
    sendBtn.disabled = value;
    cardSlot.querySelectorAll('button').forEach((b) => { b.disabled = value; });
  };

  function renderCard(pending) {
    if (!pending) {
      clear(cardSlot);
      return;
    }
    clear(cardSlot, h('div', { class: 'confirm-card', 'data-testid': 'confirm-card', dataset: { pendingId: pending.id } },
      h('h3', {}, '待您确认：创建售后工单'),
      h('dl', { class: 'kv' },
        h('div', {}, h('dt', {}, '订单'), h('dd', {}, pending.order_id || '—')),
        h('div', {}, h('dt', {}, '类型'), h('dd', {}, CATEGORY_LABEL[pending.category] || pending.category || '—')),
        h('div', {}, h('dt', {}, '标题'), h('dd', {}, pending.title || '—')),
        h('div', {}, h('dt', {}, '确认时限'), h('dd', {}, fmtTime(pending.expires_at)))),
      h('p', { class: 'muted small' }, '确认后系统会重新核验订单与售后资格再创建工单；重复确认不会重复开单。'),
      h('div', { class: 'actions' },
        h('button', { class: 'btn btn-primary', type: 'button', 'data-testid': 'confirm-yes',
          onclick: () => decide(pending.id, 'CONFIRM') }, '确认创建'),
        h('button', { class: 'btn', type: 'button', 'data-testid': 'confirm-no',
          onclick: () => decide(pending.id, 'CANCEL') }, '取消'))));
  }

  function showResponse(resp) {
    list.append(messageEl('assistant', resp.answer, resp));
    renderCard(resp.pending_action);
    clear(basis, basisPanel(resp));
    scrollDown();
  }

  async function run(userText, request) {
    if (busy) return;
    setBusy(true);
    list.append(messageEl('user', userText));
    scrollDown();
    try {
      showResponse(await request());
      if (!existing) refreshList();
    } catch (err) {
      list.append(chatErrorEl(err));
      scrollDown();
    } finally {
      setBusy(false);
    }
  }

  function send(text) {
    const message = text.trim();
    if (!message) return;
    input.value = '';
    run(message, () => api('/api/chat', { method: 'POST', body: { session_id: sid, message } }));
  }

  function decide(pendingId, decision) {
    run(decision === 'CONFIRM' ? '确认' : '取消', () => api('/api/chat/confirm', {
      method: 'POST', body: { session_id: sid, pending_action_id: pendingId, decision },
    }));
  }

  const convList = h('nav', { class: 'conv-items' });
  function fillList(items) {
    const all = items.some((c) => c.session_id === sid) ? items : [{ session_id: sid, title: '新会话', updated_at: null }, ...items];
    clear(convList, all.map((c) => h('a', {
      href: `#/chat/${encodeURIComponent(c.session_id)}`,
      class: c.session_id === sid ? 'conv active' : 'conv',
    }, h('span', { class: 'conv-title' }, c.title), h('span', { class: 'muted small' }, c.updated_at ? fmtTime(c.updated_at) : '未开始'))));
  }
  async function refreshList() {
    try {
      fillList(await api('/api/conversations'));
    } catch {
      /* 列表刷新失败不影响当前对话 */
    }
  }
  fillList(conversations);

  if (detail) {
    for (const m of detail.messages) {
      list.append(messageEl(m.role, m.content, { sources: m.sources, trace_id: m.trace_id, answer_mode: m.answer_mode }));
    }
    renderCard(detail.pending_action);
  }
  if (!detail || !detail.messages.length) {
    list.append(messageEl('assistant', '您好，我可以查询订单、物流与工单，解答产品与售后政策问题。请问有什么可以帮您？'));
  }

  const form = h('form', {
    class: 'composer',
    onsubmit: (e) => { e.preventDefault(); send(input.value); },
  }, input, sendBtn);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      send(input.value);
    }
  });

  clear(main, h('div', { class: 'chat-layout' },
    h('aside', { class: 'conv-list' },
      h('a', { class: 'btn btn-block', href: `#/chat/${newSessionId()}` }, '新会话'),
      convList),
    h('section', { class: 'chat' },
      scroller,
      h('div', { class: 'examples' }, EXAMPLES.map((q) =>
        h('button', { class: 'chip', type: 'button', onclick: () => send(q) }, q))),
      form),
    basis));
  scrollDown();
  input.focus();
}

// ---------------- 我的订单 ----------------

export async function renderOrders(main, { isStale }) {
  loading(main);
  let orders;
  try {
    orders = await api('/api/orders');
  } catch (err) {
    if (!isStale()) clear(main, h('div', { class: 'page' }, notice('error', `加载失败：${err.message}`)));
    return;
  }
  if (isStale()) return;

  const rows = orders.map((o) => {
    const detailRow = h('tr', { class: 'detail-row', hidden: true }, h('td', { colspan: 5 }));
    const toggle = h('button', {
      class: 'btn btn-quiet',
      type: 'button',
      onclick: async () => {
        if (!detailRow.hidden) {
          detailRow.hidden = true;
          return;
        }
        const cell = detailRow.firstChild;
        clear(cell, h('span', { class: 'muted' }, '加载中…'));
        detailRow.hidden = false;
        try {
          const d = await api(`/api/orders/${encodeURIComponent(o.id)}`);
          const l = d.logistics;
          clear(cell, h('dl', { class: 'kv inline' },
            h('div', {}, h('dt', {}, '支付'), h('dd', {}, fmtTime(d.paid_at))),
            h('div', {}, h('dt', {}, '发货'), h('dd', {}, fmtTime(d.shipped_at))),
            h('div', {}, h('dt', {}, '签收'), h('dd', {}, fmtTime(d.delivered_at))),
            h('div', {}, h('dt', {}, '物流'), h('dd', {},
              l ? `${l.carrier} ${l.tracking_number} · ${l.status} · ${l.current_location}` : '暂无物流信息'))));
        } catch (err) {
          clear(cell, notice('error', err.message));
        }
      },
    }, '详情');
    return [
      h('tr', { 'data-testid': 'order-row' },
        h('td', { class: 'mono' }, o.id),
        h('td', {}, o.product_id),
        h('td', {}, ORDER_STATUS_LABEL[o.status] || o.status),
        h('td', { class: 'num' }, `¥${Number(o.amount).toFixed(2)}`),
        h('td', { class: 'row-actions' }, toggle)),
      detailRow,
    ];
  });

  clear(main, h('div', { class: 'page' },
    h('div', { class: 'page-head' }, h('h1', {}, '我的订单')),
    orders.length
      ? h('table', { class: 'table' },
        h('thead', {}, h('tr', {}, ['订单号', '商品', '状态', '金额', ''].map((t, i) =>
          h('th', { class: i === 3 ? 'num' : '' }, t)))),
        h('tbody', {}, rows))
      : h('p', { class: 'empty' }, '暂无订单。')));
}

// ---------------- 我的工单 ----------------

export async function renderTickets(main, { isStale }) {
  loading(main);
  let tickets;
  try {
    tickets = await api('/api/tickets');
  } catch (err) {
    if (!isStale()) clear(main, h('div', { class: 'page' }, notice('error', `加载失败：${err.message}`)));
    return;
  }
  if (isStale()) return;
  clear(main, h('div', { class: 'page' },
    h('div', { class: 'page-head' }, h('h1', {}, '我的工单')),
    tickets.length
      ? h('table', { class: 'table', 'data-testid': 'ticket-table' },
        h('thead', {}, h('tr', {}, ['工单号', '标题', '类别', '状态', '更新时间'].map((t, i) =>
          h('th', { class: i === 4 ? 'num' : '' }, t)))),
        h('tbody', {}, tickets.map((t) => h('tr', { 'data-testid': 'ticket-row', dataset: { ticketId: t.id } },
          h('td', { class: 'mono' }, h('a', { href: ticketHref(t.id) }, t.id)),
          h('td', { class: 'truncate' }, h('a', { href: ticketHref(t.id) }, t.title)),
          h('td', {}, CATEGORY_LABEL[t.category] || t.category),
          h('td', {}, statusPill(t.status)),
          h('td', { class: 'num' }, fmtTime(t.updated_at))))))
      : h('p', { class: 'empty' }, '暂无工单。需要售后时可在', h('a', { href: '#/chat' }, '咨询'), '中申请。')));
}

function feedbackForm(ticket, onDone) {
  let rating = 0;
  const error = h('div', { class: 'form-error', role: 'alert', 'data-testid': 'feedback-error' });
  const buttons = [1, 2, 3, 4, 5].map((n) => h('button', {
    class: 'rate', type: 'button', 'aria-pressed': 'false', 'data-testid': `rate-${n}`,
    onclick: () => {
      rating = n;
      buttons.forEach((b, i) => b.setAttribute('aria-pressed', String(i + 1 === n)));
    },
  }, String(n)));
  const comment = h('textarea', { rows: 3, maxlength: 2000, placeholder: '说说这次处理的体验（可选）…', 'aria-label': '评价内容' });
  const submit = h('button', { class: 'btn btn-primary', type: 'submit', 'data-testid': 'feedback-submit' }, '提交评价');
  return h('form', {
    class: 'feedback-form',
    'data-testid': 'feedback-form',
    onsubmit: async (e) => {
      e.preventDefault();
      if (!rating) {
        error.textContent = '请选择 1–5 分';
        return;
      }
      submit.disabled = true;
      error.textContent = '';
      try {
        await api(`/api/tickets/${encodeURIComponent(ticket.id)}/feedback`, {
          method: 'POST', body: { rating, comment: comment.value.trim() },
        });
        onDone();
      } catch (err) {
        error.textContent = err.status === 409 && err.type === 'DUPLICATE' ? '该工单已评价过，不能重复提交。' : err.message;
        submit.disabled = false;
      }
    },
  },
  h('div', { class: 'rate-group', role: 'group', 'aria-label': '评分' }, buttons, h('span', { class: 'muted small' }, '1 = 很不满意，5 = 非常满意')),
  comment, error, h('div', { class: 'actions' }, submit));
}

function replyForm(onSubmit, testid) {
  const box = h('textarea', { rows: 3, maxlength: 4000, placeholder: '输入回复内容…', 'aria-label': '回复内容' });
  const error = h('div', { class: 'form-error', role: 'alert' });
  const submit = h('button', { class: 'btn btn-primary', type: 'submit', 'data-testid': `${testid}-submit` }, '发送回复');
  return h('form', {
    class: 'reply-form',
    'data-testid': testid,
    onsubmit: async (e) => {
      e.preventDefault();
      const content = box.value.trim();
      if (!content) {
        error.textContent = '回复内容不能为空';
        box.focus();
        return;
      }
      submit.disabled = true;
      error.textContent = '';
      try {
        await onSubmit(content);
      } catch (err) {
        error.textContent = err.message;
        submit.disabled = false;
      }
    },
  }, box, error, h('div', { class: 'actions' }, submit));
}

export { replyForm };

export async function renderTicketDetail(main, { params, isStale }) {
  const id = params[0];
  loading(main);
  let ticket;
  try {
    ticket = await api(`/api/tickets/${encodeURIComponent(id)}`);
  } catch (err) {
    if (!isStale()) errorPage(main, err, '工单');
    return;
  }
  if (isStale()) return;
  const reload = () => renderTicketDetail(main, { params, isStale });

  let feedbackSection;
  if (ticket.feedback) feedbackSection = feedbackView(ticket.feedback);
  else if (ticket.status === 'RESOLVED' || ticket.status === 'CLOSED') feedbackSection = feedbackForm(ticket, reload);
  else feedbackSection = h('p', { class: 'muted' }, '工单解决后可以在这里评价处理结果。');

  clear(main, h('div', { class: 'page narrow', 'data-testid': 'ticket-detail', dataset: { ticketId: ticket.id } },
    h('p', {}, h('a', { href: '#/tickets' }, '← 我的工单')),
    ticketHeader(ticket),
    timeline(ticket, 'CUSTOMER'),
    ticket.status === 'CLOSED'
      ? h('p', { class: 'muted' }, '工单已关闭，不能再回复。')
      : h('section', { class: 'panel' }, h('h2', {}, '补充说明'), replyForm(async (content) => {
        await api(`/api/tickets/${encodeURIComponent(ticket.id)}/replies`, { method: 'POST', body: { content } });
        await reload();
      }, 'customer-reply')),
    h('section', { class: 'panel' }, h('h2', {}, '评价'), feedbackSection)));
}
