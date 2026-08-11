// 🔐 认证与安全 — 管理密钥只按 SHA-256 摘要校验；供应商 key 只读脱敏。
import { get, escHtml } from '../api.js';
import { card, badge, emptyState, loadingBlock, toast } from '../ui.js';

export default {
  title: '认证与安全',
  async mount(root) {
    this.root = root;
    root.innerHTML = `
      <p class="page-intro">这一页讲清 kiwi-mem 公开版的认证现状与密钥保护方式。本页仅作信息展示与只读核对，无任何破坏性操作。</p>

      <div class="banner banner-warn"><span>🔓</span><div>
        <b>管理控制面已启用 Bearer 认证。</b>服务端只配置 <code>KIWI_ADMIN_TOKEN_SHA256</code> 摘要，
        原始密钥只保存在当前浏览器标签页。仍应部署在<b>私有网络</b>中，不要把完整聊天归档公开到公网。
      </div></div>

      ${card({
        title: '① 管理密钥',
        body: `<p class="muted" style="line-height:1.7;margin:0">
          <code>/auth/verify</code> 会验证当前标签页提交的 Bearer 密钥。服务端保存的是 SHA-256 摘要，
          不保存原始密钥，也没有公开的密钥轮换接口。
        </p>`,
      })}

      ${card({
        title: '② 网络边界',
        cls: 'mt16',
        body: `<p class="muted" style="line-height:1.7;margin:0">
          浏览器的所有管理请求都会携带认证头；MCP 的内部回环请求使用进程启动时随机生成的内部能力值。
          应继续使用 <b>127.0.0.1 / Tailscale / 私网反向代理</b>，形成第二层边界。
        </p>`,
      })}

      ${card({
        title: '③ API Key 脱敏',
        cls: 'mt16',
        body: `<p class="muted" style="line-height:1.7;margin:0">
          面板里所有供应商 / 搜索引擎的密钥<b>从不回显明文</b>。供应商接口只返回
          <code>api_key_preview</code>（如 <code>sk-…abc</code>）。编辑时<b>留空表示保持原值不变</b>，
          填新值才会覆盖。下方为当前已配置供应商的脱敏预览，供你核对而不泄露密钥。
        </p>`,
      })}

      <div class="section-title mt24">已配置供应商（只读·脱敏）</div>
      <div id="sec-provs">${loadingBlock()}</div>
    `;

    this.loadProviders();
  },

  async loadProviders() {
    const el = this.root.querySelector('#sec-provs');
    try {
      const d = await get('/admin/providers');
      const list = d.providers || [];
      if (!list.length) {
        el.innerHTML = emptyState({ icon: '🔌', msg: '还没有配置供应商', hint: '前往「供应商与模型」页接入 API' });
        return;
      }
      el.innerHTML = list.map(p => {
        const hasKey = !!p.api_key_preview;
        return `
        <div class="item">
          <div class="item-row">
            <div style="flex:1;min-width:0">
              <div class="item-title">${escHtml(p.name || '未命名')} ${badge(p.api_format || 'openai', p.api_format === 'anthropic' ? 'purple' : 'info')}</div>
              <div class="item-sub mono truncate">${escHtml(p.api_base_url || '')}</div>
              <div class="item-sub">Key 预览：<span class="mono">${escHtml(p.api_key_preview || '（未设置）')}</span></div>
            </div>
            <div class="item-actions">
              ${hasKey ? badge('🔒 已脱敏', 'accent') : badge('未设置密钥', 'muted')}
            </div>
          </div>
        </div>`;
      }).join('');
    } catch (e) {
      el.innerHTML = `<div class="banner banner-warn"><span>⚠️</span><div>加载供应商失败：${escHtml(e.message)}</div></div>`;
      toast(e.message, 'err');
    }
  },
};
