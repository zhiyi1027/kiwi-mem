// 📖 完整聊天归档 — 使用 sessionStorage 中的 memory API Bearer key 只读浏览。
import { jfetch, escHtml, fmtDateTime } from '../api.js';
import { emptyState, loadingBlock, toast, delegate } from '../ui.js';

const TOKEN_KEY = 'kiwi-archive-session-token';
// 归档页是知知与凛共同翻阅的外部界面，不是助手内部的记忆叙述。
// 因此界面显示双方名字；“我 / 知知”的第一人称契约仍只约束语义记忆正文。
const roleLabel = role => role === 'user' ? '知知/Lyra' : role === 'assistant' ? '凛/Grey' : '系统记录';

export default {
  title: '聊天归档',
  state: {
    token: sessionStorage.getItem(TOKEN_KEY) || '',
    conversations: [],
    conversationCursor: null,
    selectedId: '',
    eventCursor: null,
    events: [],
  },

  async mount(root) {
    this.root = root;
    root.innerHTML = `
      <div class="banner banner-danger"><span>🔐</span><div>
        <b>这里是逐字聊天原文。</b>只在私网 / Tailscale 中打开。
        Bearer 密钥只保存在当前标签页，关闭标签后消失。
      </div></div>
      <div class="archive-auth">
        <input id="archive-token" type="password" autocomplete="off" placeholder="粘贴任意一扇记忆入口的 Bearer 密钥">
        <button class="btn btn-primary" data-act="connect">进入归档</button>
        <button class="btn btn-secondary" data-act="forget">忘记本页密钥</button>
      </div>
      <div id="archive-status" class="text-xs muted mb16"></div>
      <div class="archive-search toolbar">
        <input id="archive-query" class="grow" type="search" placeholder="在我们说过的原话中搜索…">
        <button class="btn btn-secondary" data-act="search">搜索原话</button>
        <button class="btn btn-ghost" data-act="clear-search">返回会话目录</button>
      </div>
      <div class="archive-layout">
        <section class="archive-list-pane">
          <div class="section-title">会话目录</div>
          <div id="archive-conversations">${this.state.token ? loadingBlock() : emptyState({ msg: '请先输入密钥' })}</div>
          <div id="archive-conv-more" class="pagination"></div>
        </section>
        <section class="archive-thread-pane">
          <div class="section-title" id="archive-thread-title">原文</div>
          <div id="archive-thread">${emptyState({ msg: '选择一段会话' })}</div>
          <div id="archive-event-more" class="pagination"></div>
        </section>
      </div>`;

    this.root.querySelector('#archive-token').value = this.state.token;
    delegate(root, {
      connect: () => this.connect(),
      forget: () => this.forget(),
      conversation: el => this.openConversation(el.dataset.id),
      'more-conversations': () => this.loadConversations(true),
      'more-events': () => this.loadOlderEvents(),
      search: () => this.search(),
      'clear-search': () => this.clearSearch(),
    });
    root.querySelector('#archive-token').addEventListener('keydown', e => { if (e.key === 'Enter') this.connect(); });
    root.querySelector('#archive-query').addEventListener('keydown', e => { if (e.key === 'Enter') this.search(); });
    if (this.state.token) await this.connect(false);
  },

  headers() { return { Authorization: `Bearer ${this.state.token}` }; },
  async api(path, opts = {}) { return jfetch(path, { ...opts, headers: { ...this.headers(), ...(opts.headers || {}) } }); },

  async connect(readInput = true) {
    const input = this.root.querySelector('#archive-token');
    if (readInput) this.state.token = input.value.trim();
    if (!this.state.token) { toast('请先输入 Bearer 密钥', 'err'); return; }
    try {
      const who = await this.api('/memory/v1/whoami');
      sessionStorage.setItem(TOKEN_KEY, this.state.token);
      this.root.querySelector('#archive-status').textContent = `已进入同一记忆空间 · ${who.memory_space_id}`;
      this.state.conversations = [];
      this.state.conversationCursor = null;
      await this.loadConversations(false);
    } catch (e) {
      sessionStorage.removeItem(TOKEN_KEY);
      this.root.querySelector('#archive-status').textContent = '';
      toast('无法进入归档：' + e.message, 'err');
    }
  },

  forget() {
    sessionStorage.removeItem(TOKEN_KEY);
    this.state = { ...this.state, token: '', conversations: [], conversationCursor: null, selectedId: '', eventCursor: null, events: [] };
    this.root.querySelector('#archive-token').value = '';
    this.root.querySelector('#archive-status').textContent = '';
    this.root.querySelector('#archive-conversations').innerHTML = emptyState({ msg: '已从当前标签页清除密钥' });
    this.root.querySelector('#archive-thread').innerHTML = emptyState({ msg: '选择一段会话' });
  },

  async loadConversations(append = false) {
    const box = this.root.querySelector('#archive-conversations');
    if (!append) box.innerHTML = loadingBlock();
    try {
      const q = new URLSearchParams({ limit: '30' });
      if (append && this.state.conversationCursor) q.set('cursor', this.state.conversationCursor);
      const data = await this.api('/memory/v1/archive/conversations?' + q);
      this.state.conversations = append ? this.state.conversations.concat(data.conversations || []) : (data.conversations || []);
      this.state.conversationCursor = data.next_cursor || null;
      this.renderConversations();
    } catch (e) {
      box.innerHTML = `<div class="banner banner-warn"><span>⚠️</span><div>${escHtml(e.message)}</div></div>`;
    }
  },

  renderConversations() {
    const box = this.root.querySelector('#archive-conversations');
    if (!this.state.conversations.length) box.innerHTML = emptyState({ msg: '还没有归档对话' });
    else box.innerHTML = this.state.conversations.map(c => `
      <button class="archive-conversation ${c.conversation_id === this.state.selectedId ? 'active' : ''}"
              data-act="conversation" data-id="${escHtml(c.conversation_id)}">
        <span class="archive-conv-main">
          <b>${escHtml((c.preview || '（空对话）').slice(0, 90))}</b>
          <small>${fmtDateTime(c.last_event_at)} · ${Number(c.message_count || 0)} 条</small>
        </span>
      </button>`).join('');
    this.root.querySelector('#archive-conv-more').innerHTML = this.state.conversationCursor
      ? '<button class="btn btn-sm btn-secondary" data-act="more-conversations">更早的会话</button>' : '';
  },

  async openConversation(id) {
    this.state.selectedId = id;
    this.state.events = [];
    this.state.eventCursor = null;
    this.renderConversations();
    this.root.querySelector('#archive-thread-title').textContent = '原文';
    this.root.querySelector('#archive-thread').innerHTML = loadingBlock();
    await this.loadEventPage(false);
  },

  async loadEventPage(prepend = false) {
    try {
      const q = new URLSearchParams({ limit: '80' });
      if (prepend && this.state.eventCursor) q.set('cursor', this.state.eventCursor);
      const data = await this.api(`/memory/v1/archive/conversations/${encodeURIComponent(this.state.selectedId)}?${q}`);
      this.state.events = prepend ? (data.events || []).concat(this.state.events) : (data.events || []);
      this.state.eventCursor = data.next_cursor || null;
      this.renderThread();
    } catch (e) {
      this.root.querySelector('#archive-thread').innerHTML = `<div class="banner banner-warn"><span>⚠️</span><div>${escHtml(e.message)}</div></div>`;
    }
  },

  loadOlderEvents() { return this.loadEventPage(true); },

  renderThread() {
    const box = this.root.querySelector('#archive-thread');
    if (!this.state.events.length) box.innerHTML = emptyState({ msg: '这段会话没有可见原文' });
    else box.innerHTML = `<div class="archive-messages">${this.state.events.map(e => `
      <article class="archive-message role-${escHtml(e.role)}">
        <header><b>${roleLabel(e.role)}</b><time>${fmtDateTime(e.occurred_at)}</time></header>
        <div>${escHtml(e.content || '')}</div>
      </article>`).join('')}</div>`;
    this.root.querySelector('#archive-event-more').innerHTML = this.state.eventCursor
      ? '<button class="btn btn-sm btn-secondary" data-act="more-events">载入更早原文</button>' : '';
  },

  async search() {
    const query = this.root.querySelector('#archive-query').value.trim();
    if (!query) { this.clearSearch(); return; }
    const box = this.root.querySelector('#archive-conversations');
    box.innerHTML = loadingBlock();
    try {
      const data = await this.api('/memory/v1/archive/search', { method: 'POST', body: { query, limit: 100 } });
      const matches = data.matches || [];
      box.innerHTML = matches.length ? matches.map(e => `
        <button class="archive-conversation" data-act="conversation" data-id="${escHtml(e.conversation_id)}">
          <span class="archive-conv-main"><b>${escHtml((e.content || '').slice(0, 120))}</b>
          <small>${roleLabel(e.role)} · ${fmtDateTime(e.occurred_at)}</small></span>
        </button>`).join('') : emptyState({ msg: '没有找到这句原话' });
      this.root.querySelector('#archive-conv-more').innerHTML = '';
    } catch (e) { box.innerHTML = `<div class="banner banner-warn"><span>⚠️</span><div>${escHtml(e.message)}</div></div>`; }
  },

  clearSearch() {
    this.root.querySelector('#archive-query').value = '';
    this.state.conversations = [];
    this.state.conversationCursor = null;
    this.loadConversations(false);
  },
};
