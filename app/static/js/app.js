// 入口：hash 路由。视图状态全部体现在 URL（#/tickets/T10001、#/support/tickets?scope=mine），
// 刷新、直接打开链接、重新登录后都能回到同一视图。角色不符只做前端跳转，鉴权以后端为准。

import { loginHash, session } from './api.js';
import { clear, h } from './dom.js';
import { renderLogin } from './views/login.js';
import { renderChat, renderOrders, renderTicketDetail, renderTickets } from './views/customer.js';
import { renderQueue, renderSupportDetail } from './views/support.js';

const HOME = { CUSTOMER: '#/chat', SUPPORT: '#/support/tickets' };

const ROUTES = [
  { pattern: /^\/chat(?:\/([^/]+))?$/, role: 'CUSTOMER', nav: 'chat', view: renderChat },
  { pattern: /^\/orders$/, role: 'CUSTOMER', nav: 'orders', view: renderOrders },
  { pattern: /^\/tickets$/, role: 'CUSTOMER', nav: 'tickets', view: renderTickets },
  { pattern: /^\/tickets\/([^/]+)$/, role: 'CUSTOMER', nav: 'tickets', view: renderTicketDetail },
  { pattern: /^\/support\/tickets$/, role: 'SUPPORT', nav: 'queue', view: renderQueue },
  { pattern: /^\/support\/tickets\/([^/]+)$/, role: 'SUPPORT', nav: 'queue', view: renderSupportDetail },
];

const NAV = {
  CUSTOMER: [['chat', '#/chat', '咨询'], ['orders', '#/orders', '我的订单'], ['tickets', '#/tickets', '我的工单']],
  SUPPORT: [['queue', '#/support/tickets', '工单队列']],
};

export function homeFor(role) {
  return HOME[role] || '#/login';
}

function parseHash() {
  const raw = location.hash.replace(/^#/, '') || '/';
  const [path, qs = ''] = raw.split('?');
  return { path, query: new URLSearchParams(qs) };
}

function renderTopbar(current, activeNav) {
  const bar = document.getElementById('topbar');
  if (!current) {
    clear(bar, h('span', { class: 'brand' }, '智能客服'));
    return;
  }
  const links = (NAV[current.role] || []).map(([key, href, label]) =>
    h('a', { href, class: key === activeNav ? 'nav-link active' : 'nav-link' }, label));
  clear(
    bar,
    h('span', { class: 'brand' }, current.role === 'SUPPORT' ? '智能客服 · 客服工作台' : '智能客服'),
    h('nav', { class: 'nav' }, links),
    h('div', { class: 'who' },
      h('span', { 'data-testid': 'current-user' }, `${current.username}（${current.role === 'SUPPORT' ? '客服' : '客户'}）`),
      h('button', {
        class: 'btn btn-quiet',
        type: 'button',
        onclick: () => {
          session.clear();
          location.hash = '#/login';
        },
      }, '退出')),
  );
}

let renderSeq = 0;

async function route() {
  const seq = ++renderSeq;
  const { path, query } = parseHash();
  const main = document.getElementById('app');
  const current = session.get();

  if (path === '/login') {
    renderTopbar(null);
    renderLogin(main, query);
    return;
  }
  if (!current) {
    location.replace(loginHash(location.hash));
    return;
  }
  const match = ROUTES.map((r) => ({ r, m: path.match(r.pattern) })).find((x) => x.m);
  if (!match) {
    location.replace(homeFor(current.role));
    return;
  }
  if (match.r.role !== current.role) {
    // 体验层：跳回本角色首页；即使绕过这里直接调接口，后端也会返回 403
    location.replace(homeFor(current.role));
    return;
  }
  renderTopbar(current, match.r.nav);
  const params = match.m.slice(1).map((p) => (p === undefined ? undefined : decodeURIComponent(p)));
  // isStale：异步请求返回时若用户已切到别的视图，则丢弃结果，避免旧数据覆盖新页面
  await match.r.view(main, { params, query, isStale: () => seq !== renderSeq });
}

window.addEventListener('hashchange', route);
route();
