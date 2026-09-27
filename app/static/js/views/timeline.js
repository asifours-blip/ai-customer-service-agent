// 工单详情的公共部分：处理记录时间线（回复内容按 reply_id 关联）与反馈展示。
// viewer = 'CUSTOMER' | 'SUPPORT'：客户视图里后端已隐去客服账号，这里只按角色显示「客服」。

import { CATEGORY_LABEL, PRIORITY_LABEL, STATUS_LABEL, fmtTime, h, statusPill } from '../dom.js';

function actorLabel(event, viewer) {
  if (event.actor_role === 'SUPPORT') return viewer === 'SUPPORT' ? `客服 ${event.actor_id}` : '客服';
  return viewer === 'CUSTOMER' ? '我' : `客户 ${event.actor_id}`;
}

function describe(event) {
  switch (event.event_type) {
    case 'CREATED':
      return '创建了工单';
    case 'CLAIMED':
      return '领取了工单';
    case 'STATUS_CHANGED':
      return `将状态从「${STATUS_LABEL[event.from_status] || event.from_status}」改为「${
        STATUS_LABEL[event.to_status] || event.to_status}」`;
    case 'REPLIED':
      return '回复';
    case 'FEEDBACK_SUBMITTED':
      return '提交了评价';
    default:
      return event.event_type;
  }
}

export function ticketHeader(ticket, extra = []) {
  return h('header', { class: 'detail-head' },
    h('div', { class: 'detail-title' },
      h('h1', { 'data-testid': 'ticket-title' }, h('span', { class: 'ticket-id' }, ticket.id), ticket.title),
      statusPill(ticket.status)),
    h('dl', { class: 'meta' },
      h('div', {}, h('dt', {}, '类别'), h('dd', {}, CATEGORY_LABEL[ticket.category] || ticket.category)),
      h('div', {}, h('dt', {}, '优先级'), h('dd', {}, PRIORITY_LABEL[ticket.priority] || ticket.priority)),
      h('div', {}, h('dt', {}, '关联订单'), h('dd', {}, ticket.order_id || '—')),
      h('div', {}, h('dt', {}, '创建时间'), h('dd', {}, fmtTime(ticket.created_at))),
      extra),
    ticket.description ? h('p', { class: 'description' }, ticket.description) : null);
}

export function timeline(ticket, viewer) {
  const replies = new Map(ticket.replies.map((r) => [r.id, r]));
  const items = ticket.events.map((event) => {
    const reply = event.reply_id ? replies.get(event.reply_id) : null;
    return h('li', {
      class: `tl-item tl-${event.event_type.toLowerCase()} tl-${event.actor_role.toLowerCase()}`,
      'data-testid': 'timeline-item',
      dataset: { event: event.event_type },
    },
    h('div', { class: 'tl-line' },
      h('strong', {}, actorLabel(event, viewer)),
      h('span', {}, describe(event)),
      h('time', { datetime: event.created_at }, fmtTime(event.created_at))),
    reply ? h('div', { class: 'tl-reply', 'data-testid': 'reply-content' }, reply.content) : null);
  });
  return h('section', { class: 'panel' },
    h('h2', {}, '处理记录'),
    items.length ? h('ol', { class: 'timeline' }, items) : h('p', { class: 'muted' }, '暂无记录'));
}

export function feedbackView(feedback) {
  return h('div', { class: 'feedback-done', 'data-testid': 'feedback-done' },
    h('span', { class: 'stars', 'aria-label': `${feedback.rating} 分` }, '★'.repeat(feedback.rating) + '☆'.repeat(5 - feedback.rating)),
    h('span', { class: 'muted' }, `${feedback.rating} / 5 · ${fmtTime(feedback.created_at)}`),
    feedback.comment ? h('p', {}, feedback.comment) : null);
}
