/* Shared approval prompt for the voice-first surfaces (/ and /face).
 *
 * An approval request arrives on the SSE stream from /api/chat and blocks the
 * request until answered. Both of these interfaces used to ignore it: the
 * server asked, nothing was rendered, and after APPROVAL_TIMEOUT the call was
 * auto-denied. Observed in production — "create a PDF about Coimbra and email
 * it" produced the PDF, asked to send, and sat on "thinking" for five minutes
 * before denying itself. Every gated tool was unusable there: send_email,
 * write_file, delete_file, run_shell, claude_task, db_execute, calendar,
 * schedules.
 *
 * chat.html keeps its own richer card (history, always-allow). This is the
 * minimal shared version for the two surfaces that had none, in one file
 * rather than two copies that drift.
 *
 * The host page supplies its own idle/busy callbacks, because "stop looking
 * busy" means something different on a WebGL face than in a message list.
 */
(function (g) {
  'use strict';

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // t() may not exist if i18n failed to load; fall back to English rather than
  // rendering a raw key, which is what a missing key returns here.
  function label(key, fallback) {
    try {
      var s = g.t ? g.t(key) : key;
      return (!s || s === key) ? fallback : s;
    } catch (e) { return fallback; }
  }

  /**
   * @param ev    the approval_request event {id, tool, summary}
   * @param opts  {onWaiting, onAnswered, fetcher}
   */
  g.showApprovalPrompt = function (ev, opts) {
    opts = opts || {};
    var fetcher = opts.fetcher || g._apiFetch || window.fetch.bind(window);

    // One prompt at a time: a second request replaces the first rather than
    // stacking dialogs the user has to dismiss in order.
    var old = document.querySelectorAll('.suni-approval');
    for (var i = 0; i < old.length; i++) old[i].remove();

    if (opts.onWaiting) { try { opts.onWaiting(); } catch (e) {} }

    var wrap = document.createElement('div');
    wrap.className = 'suni-approval';
    wrap.style.cssText =
      'position:fixed;left:50%;bottom:96px;transform:translateX(-50%);z-index:99999;' +
      'max-width:min(560px,92vw);padding:14px 16px;border-radius:10px;' +
      'background:rgba(10,14,24,.94);border:1px solid rgba(0,212,255,.45);' +
      'box-shadow:0 8px 30px rgba(0,0,0,.5);color:#dfe8f5;' +
      'font-size:13px;line-height:1.5;backdrop-filter:blur(6px)';
    wrap.innerHTML =
      '<div style="font-size:10px;letter-spacing:.14em;text-transform:uppercase;' +
      'color:#00d4ff;margin-bottom:6px">' +
        esc(label('chat.approval_required', 'Permission required')) + '</div>' +
      '<div style="margin-bottom:12px;word-break:break-word">' +
        esc(ev.summary || ev.tool || '') + '</div>' +
      '<div style="display:flex;gap:8px;flex-wrap:wrap">' +
        '<button data-d="allow" style="flex:1;min-width:120px;padding:9px 12px;' +
        'border-radius:6px;border:1px solid rgba(0,212,255,.5);' +
        'background:rgba(0,212,255,.14);color:#dfe8f5;font:inherit;cursor:pointer">' +
        esc(label('common.allow', 'Allow')) + '</button>' +
        '<button data-d="deny" style="flex:1;min-width:120px;padding:9px 12px;' +
        'border-radius:6px;border:1px solid rgba(255,120,140,.45);' +
        'background:rgba(255,120,140,.12);color:#dfe8f5;font:inherit;cursor:pointer">' +
        esc(label('common.deny', 'Deny')) + '</button>' +
      '</div>' +
      // Standing permission for this tool. The gate deliberately fires even
      // when the judge rules the call on-intent — an explicitly requested
      // email still has a model-composed body and attachment — so asking every
      // time is the safe default. This is the way out of it, and chat.html was
      // the only surface that offered it.
      '<label style="display:flex;align-items:center;gap:7px;margin-top:10px;' +
      'font-size:11px;color:#93a4bb;cursor:pointer">' +
        '<input type="checkbox" data-always style="width:auto;margin:0">' +
        esc(label('chat.always_allow', 'Always allow this tool')) +
      '</label>';
    document.body.appendChild(wrap);

    var btns = wrap.querySelectorAll('button');
    for (var j = 0; j < btns.length; j++) {
      btns[j].onclick = function () {
        var decision = this.getAttribute('data-d');
        var alwaysEl = wrap.querySelector('[data-always]');
        // Only meaningful on allow: "always deny" is not a thing the server
        // stores, and sending it would be a silent no-op the user thinks took.
        var always = !!(alwaysEl && alwaysEl.checked) && decision === 'allow';
        for (var k = 0; k < btns.length; k++) btns[k].disabled = true;
        // Remove the prompt before awaiting: the answer releases the blocked
        // request, and leaving a dead dialog on screen reads as "ignored".
        wrap.remove();
        if (opts.onAnswered) { try { opts.onAnswered(decision); } catch (e) {} }
        Promise.resolve(
          fetcher('/api/approval/' + ev.id, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
              decision: decision,
              tool: ev.tool,
              summary: ev.summary || ev.tool,
              always_allow: always
            })
          })
        ).catch(function (e) { console.error('[APPROVAL] decision failed', e); });
      };
    }
  };

  /* Standing permissions, with a way to take them back.
   *
   * Ticking "always allow" writes a rule that now survives a restart, which is
   * what the label always claimed. That makes revocation the other half of the
   * feature rather than a nicety: a permission you cannot find is one you
   * cannot withdraw, and GET/DELETE /api/approval/trust existed for months
   * with no interface on top of them.
   *
   * Renders nothing when there are no rules — an empty "Standing permissions"
   * box in every settings dialog is noise, and its absence is accurate.
   *
   * @param el  container to fill (already in the DOM)
   */
  g.renderTrustRules = function (el) {
    if (!el) return;
    var fetcher = g._apiFetch || window.fetch.bind(window);
    el.innerHTML = '';
    return Promise.resolve(fetcher('/api/approval/trust'))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var rules = (data && data.rules) || {};
        var tools = Object.keys(rules);
        if (!tools.length) return;

        var html =
          '<label>' + esc(label('settings.trust_title', 'Standing permissions')) + '</label>' +
          '<small style="display:block;color:#93a4bb;font-size:11px;margin-bottom:6px">' +
            esc(label('settings.trust_hint',
                      'Tools you chose to always allow. They run without asking until revoked.')) +
          '</small>';
        for (var i = 0; i < tools.length; i++) {
          html +=
            '<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;' +
            'padding:5px 0;border-bottom:1px solid rgba(255,255,255,.06)">' +
              '<code style="font-size:12px">' + esc(tools[i]) + '</code>' +
              '<button type="button" data-revoke="' + esc(tools[i]) + '" ' +
              'style="padding:3px 10px;border-radius:5px;font-size:11px;cursor:pointer;' +
              'border:1px solid rgba(255,120,140,.45);background:rgba(255,120,140,.12);' +
              'color:inherit;font-family:inherit">' +
                esc(label('common.revoke', 'Revoke')) +
              '</button>' +
            '</div>';
        }
        el.innerHTML = html;

        var btns = el.querySelectorAll('[data-revoke]');
        for (var j = 0; j < btns.length; j++) {
          btns[j].onclick = function () {
            var tool = this.getAttribute('data-revoke');
            this.disabled = true;
            Promise.resolve(
              fetcher('/api/approval/trust/' + encodeURIComponent(tool), {method: 'DELETE'})
            ).then(function () {
              g.renderTrustRules(el);       // re-read rather than guess the new state
            }).catch(function (e) {
              console.error('[APPROVAL] revoke failed', e);
            });
          };
        }
      })
      .catch(function (e) { console.error('[APPROVAL] trust list failed', e); });
  };
})(window);
