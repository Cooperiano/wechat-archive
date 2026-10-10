'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const token = location.hash.slice(1) || sessionStorage.getItem('archive-token') || '';
  if (token) sessionStorage.setItem('archive-token', token);
  history.replaceState(null, '', location.pathname);
  let room = '', rp = 0, mp = 0, roomRequest = 0, messageRequest = 0;
  const fail = error => { $('error').hidden = false; $('error').textContent = error.message; };
  const clearError = () => { $('error').hidden = true; };
  async function api(path, params = {}) {
    const response = await fetch('/api/' + path + '?' + new URLSearchParams(params), {
      headers: {Authorization: 'Bearer ' + token}, cache: 'no-store'
    });
    if (!response.ok) throw new Error(response.status === 401 ? '此启动地址已失效，请重新打开终端中的完整地址。' : '读取失败，请检查归档或筛选条件。');
    return response.json();
  }
  function pager(prefix, data) {
    $(prefix + 'prev').disabled = data.page === 0;
    $(prefix + 'next').disabled = (data.page + 1) * 100 >= data.total;
    $(prefix + 'page').textContent = `${data.total} 条 · ${data.total ? data.page + 1 : 0}/${Math.ceil(data.total / 100)} 页`;
  }
  async function rooms() {
    const request = ++roomRequest;
    $('rooms-prev').disabled = $('rooms-next').disabled = true;
    try {
    const data = await api('rooms', {page: rp, q: $('rooms-search').value, kind: $('kind').value});
    if (request !== roomRequest) return;
    rp = data.page;
    $('rooms').replaceChildren();
    for (const item of data.rows) {
      const button = document.createElement('button');
      button.className = 'room' + (room === item.room_id ? ' active' : '');
      button.textContent = `${item.name} · ${item.source_rows}`;
      button.onclick = () => {
        room = item.room_id; mp = 0; $('title').textContent = item.name;
        $('messages').replaceChildren(); $('page').textContent = '正在读取…';
        $('prev').disabled = $('next').disabled = true;
        document.querySelectorAll('.room').forEach(b => b.classList.toggle('active', b === button));
        messages().catch(fail);
      };
      $('rooms').append(button);
    }
    pager('rooms-', data);
    } catch (error) { if (request === roomRequest) throw error; }
  }
  async function messages() {
    if (!room) return;
    const request = ++messageRequest;
    $('messages').replaceChildren(); $('page').textContent = '正在读取…';
    $('prev').disabled = $('next').disabled = true;
    const params = {room, page: mp, q: $('q').value, type: $('type').value};
    try {
    if ($('from').value) params.from = String(Date.parse($('from').value + 'T00:00:00+08:00') / 1000);
    if ($('to').value) params.before = String(Date.parse($('to').value + 'T00:00:00+08:00') / 1000 + 86400);
    const data = await api('messages', params);
    if (request !== messageRequest) return;
    mp = data.page;
    $('messages').replaceChildren();
    for (const item of data.rows) {
      const article = document.createElement('article'); article.className = 'message';
      const meta = document.createElement('div'); meta.className = 'meta';
      const timestamp = Number(item.create_time);
      const date = new Date(timestamp * 1000);
      const valid = item.create_time !== null && item.create_time !== undefined && typeof item.create_time !== 'boolean' && !(typeof item.create_time === 'string' && !item.create_time.trim());
      const time = valid && Number.isFinite(timestamp) && Number.isFinite(date.getTime())
        ? date.toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai'}) : '时间未知';
      meta.textContent = `${item.sender_name || '发送者未映射'} · ${time} · 类型 ${item.local_type}`;
      const body = document.createElement('p'); body.className = 'body'; body.textContent = item.content;
      article.append(meta, body); $('messages').append(article);
    }
    pager('', data); clearError();
    } catch (error) { if (request === messageRequest) throw error; }
  }
  $('filters').onsubmit = event => { event.preventDefault(); mp = 0; messages().catch(fail); };
  $('kind').onchange = () => { rp = 0; rooms().catch(fail); };
  let debounce;
  $('rooms-search').oninput = () => { clearTimeout(debounce); debounce = setTimeout(() => { rp = 0; rooms().catch(fail); }, 250); };
  $('rooms-prev').onclick = () => { rp = Math.max(0, rp - 1); rooms().catch(fail); };
  $('rooms-next').onclick = () => { rp++; rooms().catch(fail); };
  $('prev').onclick = () => { mp = Math.max(0, mp - 1); messages().catch(fail); };
  $('next').onclick = () => { mp++; messages().catch(fail); };
  $('prev').disabled = $('next').disabled = true;
  api('status').then(data => { $('status').textContent = `${data.conversations} 个会话 · ${data.archived_messages} 条消息`; }).catch(fail);
  rooms().catch(fail);
})();
