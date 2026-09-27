// DOM 小工具：所有文本都经 textContent 写入，不拼接 innerHTML，避免 XSS。

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'text') el.textContent = value;
    else if (key === 'dataset') Object.assign(el.dataset, value);
    else if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2), value);
    else if (value === true) el.setAttribute(key, '');
    else el.setAttribute(key, String(value));
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === undefined || child === null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function clear(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

const pad = (n) => String(n).padStart(2, '0');

export function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export const STATUS_LABEL = { OPEN: '待处理', PROCESSING: '处理中', RESOLVED: '已解决', CLOSED: '已关闭' };
export const NEXT_STATUS = { OPEN: 'PROCESSING', PROCESSING: 'RESOLVED', RESOLVED: 'CLOSED' };
export const CATEGORY_LABEL = { REFUND: '退款', EXCHANGE: '换货', REPAIR: '维修', OTHER: '其他' };
export const PRIORITY_LABEL = { LOW: '低', MEDIUM: '中', HIGH: '高' };
export const ORDER_STATUS_LABEL = {
  PENDING: '待支付', PAID: '已支付', SHIPPED: '已发货', DELIVERED: '已签收', CANCELLED: '已取消',
};

export function statusPill(status) {
  return h('span', { class: `pill pill-${status.toLowerCase()}`, 'data-testid': 'status-pill', dataset: { status } },
    STATUS_LABEL[status] || status);
}

export function notice(kind, text) {
  return h('div', { class: `notice notice-${kind}`, role: kind === 'error' ? 'alert' : 'status' }, text);
}

// 把文本中的工单号渲染为可点击链接（仍逐段 textContent 写入）
export function linkifyTickets(text, hrefFor) {
  const parts = [];
  let last = 0;
  for (const m of text.matchAll(/T\d{4,}/g)) {
    parts.push(text.slice(last, m.index));
    parts.push(h('a', { href: hrefFor(m[0]), 'data-testid': 'ticket-link' }, m[0]));
    last = m.index + m[0].length;
  }
  parts.push(text.slice(last));
  return parts;
}
