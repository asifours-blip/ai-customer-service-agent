// API 客户端与会话：token 存 sessionStorage（关闭标签页即失效）；任何接口 401 → 清会话并跳登录页。
// 注意：这里的角色信息只用于前端导航，真正的鉴权在后端每个接口独立完成。

const KEY = 'csagent.session';

export const session = {
  get() {
    try {
      return JSON.parse(sessionStorage.getItem(KEY) || 'null');
    } catch {
      return null;
    }
  },
  set(value) {
    sessionStorage.setItem(KEY, JSON.stringify(value));
  },
  clear() {
    sessionStorage.removeItem(KEY);
  },
};

export class ApiError extends Error {
  constructor(status, type, message) {
    super(message);
    this.status = status;
    this.type = type;
  }
}

export function loginHash(next) {
  const target = next && !next.startsWith('#/login') ? next : '';
  return target ? `#/login?next=${encodeURIComponent(target)}` : '#/login';
}

export function redirectToLogin() {
  if (!location.hash.startsWith('#/login')) location.hash = loginHash(location.hash);
}

function errorMessage(data, status) {
  const detail = data && data.detail;
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) return [detail.type, detail.message];
  if (Array.isArray(detail)) return ['VALIDATION_FAILED', detail.map((d) => d.msg).join('；')];
  return [undefined, `请求失败（HTTP ${status}）`];
}

export async function api(path, { method = 'GET', body } = {}) {
  const headers = {};
  const current = session.get();
  if (current) headers.Authorization = `Bearer ${current.token}`;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const resp = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  let data = null;
  try {
    data = await resp.json();
  } catch {
    data = null;
  }
  if (resp.status === 401 && path !== '/api/auth/login') {
    session.clear();
    redirectToLogin();
  }
  if (!resp.ok) {
    const [type, message] = errorMessage(data, resp.status);
    throw new ApiError(resp.status, type, message);
  }
  return data;
}
