// 客服工作台：工单队列（范围 / 状态筛选写在 URL）、领取、详情时间线、回复、状态推进。
// 按钮是否出现只是体验层判断；后端对领取、迁移、回复都独立校验领取人与状态。

import { api, session } from '../api.js';
import {
  CATEGORY_LABEL,
  NEXT_STATUS,
  PRIORITY_LABEL,
  STATUS_LABEL,
  clear,
  fmtTime,
  h,
  notice,
  statusPill,
} from '../dom.js';
import { replyForm } from './customer.js';
import { feedbackView, ticketHeader, timeline } from './timeline.js';

const SCOPES = [['unassigned', '未指派'], ['mine', '我的'], ['all', '全部']];
const detailHref = (id) => `#/support/tickets/${encodeURIComponent(id)}`;

function queueHref(scope, status) {
  const q = new URLSearchParams({ scope });
  if (status) q.set('status', status);
  return `#/support/tickets?${q}`;
}

export async function renderQueue(main, { query, isStale }) {
  const scope = SCOPES.some(([k]) => k === query.get('scope')) ? query.get('scope') : 'unassigned';
  const status = STATUS_LABEL[query.get('status')] ? query.get('status') : '';
  const me = session.get().userId;
  clear(main, h('div', { class: 'page' }, h('p', { class: 'muted' }, '加载中…')));

  let tickets;
  try {
    const q = new URLSearchParams({ scope });
    if (status) q.set('status', status);
    tickets = await api(`/api/support/tickets?${q}`);
  } catch (err) {
    if (!isStale()) clear(main, h('div', { class: 'page' }, notice('error', `加载失败：${err.message}`)));
    return;
  }
  if (isStale()) return;

  const flash = h('div', { class: 'flash' });
  const statusSelect = h('select', {
    'aria-label': '状态筛选',
    'data-testid': 'status-filter',
    onchange: (e) => { location.hash = queueHref(scope, e.target.value); },
  },
  h('option', { value: '' }, '全部状态'),
  Object.entries(STATUS_LABEL).map(([k, v]) => h('option', { value: k, selected: k === status }, v)));

  const rows = tickets.map((t) => {
    let action = null;
    if (!t.assignee_id && t.status !== 'CLOSED') {
      action = h('button', {
        class: 'btn btn-primary btn-sm',
        type: 'button',
        'data-testid': 'claim',
        onclick: async (e) => {
          e.target.disabled = true;
          try {
            await api(`/api/support/tickets/${encodeURIComponent(t.id)}/claim`, { method: 'POST' });
            location.hash = detailHref(t.id);
          } catch (err) {
            clear(flash, notice('error', `领取 ${t.id} 失败：${err.message}`));
            e.target.disabled = false;
          }
        },
      }, '领取');
    }
    return h('tr', { 'data-testid': 'queue-row', dataset: { ticketId: t.id } },
      h('td', { class: 'mono' }, h('a', { href: detailHref(t.id) }, t.id)),
      h('td', { class: 'truncate' }, h('a', { href: detailHref(t.id) }, t.title)),
      h('td', {}, CATEGORY_LABEL[t.category] || t.category),
      h('td', {}, PRIORITY_LABEL[t.priority] || t.priority),
      h('td', {}, statusPill(t.status)),
      h('td', {}, t.assignee_id ? (t.assignee_id === me ? '我' : t.assignee_id) : h('span', { class: 'muted' }, '未指派')),
      h('td', { class: 'num' }, fmtTime(t.updated_at)),
      h('td', { class: 'row-actions' }, action));
  });

  clear(main, h('div', { class: 'page' },
    h('div', { class: 'page-head' },
      h('h1', {}, '工单队列'),
      h('div', { class: 'filters' },
        h('div', { class: 'segmented', role: 'tablist' }, SCOPES.map(([k, label]) =>
          h('a', { href: queueHref(k, status), role: 'tab', 'aria-selected': String(k === scope), 'data-testid': `scope-${k}` }, label))),
        statusSelect)),
    flash,
    tickets.length
      ? h('table', { class: 'table', 'data-testid': 'queue-table' },
        h('thead', {}, h('tr', {}, ['工单号', '标题', '类别', '优先级', '状态', '处理人', '更新时间', ''].map((x, i) =>
          h('th', { class: i === 6 ? 'num' : '' }, x)))),
        h('tbody', {}, rows))
      : h('p', { class: 'empty' }, '当前筛选下没有工单。')));
}

export async function renderSupportDetail(main, { params, isStale }) {
  const id = params[0];
  const me = session.get().userId;
  clear(main, h('div', { class: 'page' }, h('p', { class: 'muted' }, '加载中…')));
  let ticket;
  try {
    ticket = await api(`/api/support/tickets/${encodeURIComponent(id)}`);
  } catch (err) {
    if (!isStale()) {
      clear(main, h('div', { class: 'page' }, notice('error', err.status === 404 ? '工单不存在。' : `加载失败：${err.message}`),
        h('p', {}, h('a', { href: '#/support/tickets' }, '返回队列'))));
    }
    return;
  }
  if (isStale()) return;
  const reload = () => renderSupportDetail(main, { params, isStale });
  const flash = h('div', { class: 'flash' });
  const mine = ticket.assignee_id === me;

  async function act(button, request) {
    button.disabled = true;
    try {
      await request();
      await reload();
    } catch (err) {
      clear(flash, notice('error', err.message));
      button.disabled = false;
    }
  }

  const actions = [];
  if (!ticket.assignee_id && ticket.status !== 'CLOSED') {
    actions.push(h('button', {
      class: 'btn btn-primary', type: 'button', 'data-testid': 'claim',
      onclick: (e) => act(e.target, () => api(`/api/support/tickets/${encodeURIComponent(ticket.id)}/claim`, { method: 'POST' })),
    }, '领取'));
  }
  const next = NEXT_STATUS[ticket.status];
  if (mine && next) {
    actions.push(h('button', {
      class: 'btn', type: 'button', 'data-testid': 'advance', dataset: { target: next },
      onclick: (e) => act(e.target, () => api(`/api/support/tickets/${encodeURIComponent(ticket.id)}/status`, {
        method: 'PATCH', body: { status: next },
      })),
    }, `标记为「${STATUS_LABEL[next]}」`));
  }

  let replySection;
  if (ticket.status === 'CLOSED') replySection = h('p', { class: 'muted' }, '工单已关闭，不能再回复。');
  else if (!mine) replySection = h('p', { class: 'muted' }, ticket.assignee_id ? '该工单由其他客服处理，仅领取人可以回复。' : '领取后才能回复。');
  else {
    replySection = replyForm(async (content) => {
      await api(`/api/support/tickets/${encodeURIComponent(ticket.id)}/replies`, { method: 'POST', body: { content } });
      await reload();
    }, 'support-reply');
  }

  const assignee = ticket.assignee_id ? (mine ? `我（${me}）` : ticket.assignee_id) : '未指派';
  clear(main, h('div', { class: 'page narrow', 'data-testid': 'ticket-detail', dataset: { ticketId: ticket.id } },
    h('p', {}, h('a', { href: '#/support/tickets' }, '← 工单队列')),
    ticketHeader(ticket, [
      h('div', {}, h('dt', {}, '客户'), h('dd', {}, ticket.user_id)),
      h('div', {}, h('dt', {}, '处理人'), h('dd', { 'data-testid': 'assignee' }, assignee)),
    ]),
    actions.length ? h('div', { class: 'toolbar' }, actions) : null,
    flash,
    timeline(ticket, 'SUPPORT'),
    h('section', { class: 'panel' }, h('h2', {}, '回复客户'), replySection),
    h('section', { class: 'panel' }, h('h2', {}, '客户评价'),
      ticket.feedback ? feedbackView(ticket.feedback) : h('p', { class: 'muted' }, '暂无评价。'))));
}
