// 登录页：用户名 + 口令；登录后回到 next（仅当 next 属于本角色可访问的视图，否则回首页）。

import { api, ApiError, session } from '../api.js';
import { clear, h } from '../dom.js';

const ROLE_PREFIX = { CUSTOMER: ['#/chat', '#/orders', '#/tickets'], SUPPORT: ['#/support/'] };
const HOME = { CUSTOMER: '#/chat', SUPPORT: '#/support/tickets' };

function destination(role, next) {
  if (next && (ROLE_PREFIX[role] || []).some((p) => next.startsWith(p))) return next;
  return HOME[role] || '#/login';
}

export function renderLogin(main, query) {
  const next = query.get('next') || '';
  const error = h('div', { class: 'form-error', role: 'alert', 'data-testid': 'login-error' });
  const username = h('input', {
    id: 'login-username', name: 'username', autocomplete: 'username', required: true, spellcheck: 'false',
  });
  const password = h('input', {
    id: 'login-password', name: 'password', type: 'password', autocomplete: 'current-password', required: true,
  });
  const submit = h('button', { class: 'btn btn-primary btn-block', type: 'submit' }, '登录');

  const form = h('form', {
    class: 'login-card',
    novalidate: true,
    onsubmit: async (event) => {
      event.preventDefault();
      error.textContent = '';
      const u = username.value.trim();
      if (!u || !password.value) {
        error.textContent = '请输入用户名和口令';
        (u ? password : username).focus();
        return;
      }
      submit.disabled = true;
      try {
        const data = await api('/api/auth/login', { method: 'POST', body: { username: u, password: password.value } });
        session.set({ token: data.access_token, role: data.role, userId: data.user_id, username: u });
        location.hash = destination(data.role, next);
      } catch (err) {
        error.textContent = err instanceof ApiError && err.status === 401 ? '用户名或口令错误' : err.message;
        password.select();
      } finally {
        submit.disabled = false;
      }
    },
  },
  h('h1', {}, '登录'),
  h('p', { class: 'muted' }, next ? '登录后将返回刚才的页面。' : '客户与客服使用同一入口，登录后按角色进入。'),
  h('label', { for: 'login-username' }, '用户名'),
  username,
  h('label', { for: 'login-password' }, '口令'),
  password,
  error,
  submit);

  clear(main, h('div', { class: 'login-wrap' }, form));
  username.focus();
}
