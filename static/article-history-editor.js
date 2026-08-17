(() => {
  const VDITOR_CDN = '/static/vendor/vditor';
  const SAVE_DELAY_MS = 800;
  let activeSession = null;
  let formatClipboard = null;

  const plainSelection = (markdown) => markdown
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/^\s*(?:>|[-+*]|\d+\.)\s+/gm, '')
    .replace(/(!?\[)([^\]]+)(\]\([^)]*\))/g, '$2')
    .replace(/(?:\*\*|__|~~|`)(.*?)(?:\*\*|__|~~|`)/gs, '$1');

  const customToolbar = [
    'undo', 'redo',
    {
      name: 'clear-format',
      tip: '清除格式',
      icon: '<span class="history-toolbar-glyph">Tx</span>',
      click(_event, vditor) {
        const selection = vditor.getSelection();
        if (selection) vditor.insertValue(plainSelection(selection));
      },
    },
    {
      name: 'format-painter',
      tip: '格式刷',
      icon: '<span class="history-toolbar-glyph">刷</span>',
      click(_event, vditor) {
        const selection = vditor.getSelection();
        if (!selection) {
          if (activeSession) setState(activeSession, '请先选中带格式的文字', 'is-dirty');
          return;
        }
        if (!formatClipboard) {
          const formats = [
            [/^(#{1,6}\s+)([\s\S]*)$/, '$1', ''],
            [/^(>\s+)([\s\S]*)$/, '$1', ''],
            [/^(\*\*)([\s\S]*)(\*\*)$/, '$1', '$3'],
            [/^(~~)([\s\S]*)(~~)$/, '$1', '$3'],
            [/^(`)([\s\S]*)(`)$/, '$1', '$3'],
          ];
          const match = formats.map(([regex, prefixRef, suffixRef]) => {
            const found = selection.match(regex);
            return found ? {prefix: found[Number(prefixRef.slice(1))] || '', suffix: suffixRef ? found[Number(suffixRef.slice(1))] || '' : ''} : null;
          }).find(Boolean);
          formatClipboard = match || {prefix: '**', suffix: '**'};
          if (activeSession) setState(activeSession, '格式已取样，请选中文字后再次点格式刷', 'is-dirty');
          return;
        }
        vditor.insertValue(`${formatClipboard.prefix}${plainSelection(selection)}${formatClipboard.suffix}`);
        formatClipboard = null;
      },
    },
    '|', 'headings', 'bold', 'quote', 'list', 'ordered-list', 'strike',
    'inline-code', 'code', 'upload', 'link', 'emoji', 'table',
    {
      name: 'alignment',
      tip: '对齐（HTML 扩展）',
      icon: '<span class="history-toolbar-glyph">≡</span>',
      toolbar: [
        {name: 'align-left', tip: '左对齐', prefix: '<div align="left">\n', suffix: '\n</div>'},
        {name: 'align-center', tip: '居中', prefix: '<div align="center">\n', suffix: '\n</div>'},
        {name: 'align-right', tip: '右对齐', prefix: '<div align="right">\n', suffix: '\n</div>'},
      ],
    },
    {
      name: 'more',
      tip: '更多',
      icon: '<span class="history-toolbar-glyph">•••</span>',
      toolbar: ['italic', 'check', 'line'],
    },
    '|', 'export',
  ];

  function setState(session, text, kind = '') {
    session.state.textContent = text;
    session.state.className = `history-save-state ${kind}`.trim();
  }

  function displayTitle(article, content) {
    const firstLine = content.split(/\r?\n/).find((line) => line.trim()) || '未命名文章';
    article.querySelector('[data-article-title]').textContent = firstLine.replace(/^#{1,6}\s+/, '').trim();
  }

  function attachToolbar(session, attempts = 0) {
    if (session.closed) return;
    const toolbar = session.mount.querySelector('.vditor-toolbar');
    if (toolbar) {
      toolbar.style.paddingLeft = '5px';
      toolbar.style.paddingRight = '5px';
      session.toolbarHost.hidden = false;
      session.toolbarHost.append(toolbar);
      return;
    }
    if (attempts < 100) setTimeout(() => attachToolbar(session, attempts + 1), 20);
  }

  async function save(session) {
    if (!session || session.closed) return;
    clearTimeout(session.timer);
    session.timer = null;
    const content = session.editor.getValue();
    session.latestContent = content;
    if (content === session.savedContent) {
      if (!session.saving) setState(session, '已保存', 'is-saved');
      return;
    }
    if (session.saving) {
      session.saveQueued = true;
      return session.saving;
    }
    setState(session, '正在保存…', 'is-saving');
    session.saving = fetch(`/api/articles/${session.id}`, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({content, updated_at: session.updatedAt}),
      keepalive: content.length < 60_000,
    }).then(async (response) => {
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `保存失败（${response.status}）`);
      session.updatedAt = data.updated_at;
      session.article.dataset.updatedAt = data.updated_at;
      session.savedContent = content;
      session.pre.textContent = content;
      displayTitle(session.article, content);
      document.dispatchEvent(new CustomEvent('article-content-saved', {
        detail: {articleId: session.id, content},
      }));
      setState(session, '已保存', 'is-saved');
    }).catch((error) => {
      setState(session, error.message, 'is-error');
    }).finally(() => {
      session.saving = null;
      if (session.saveQueued) {
        session.saveQueued = false;
        save(session);
      }
    });
    return session.saving;
  }

  function scheduleSave(session) {
    clearTimeout(session.timer);
    setState(session, '有修改', 'is-dirty');
    session.timer = setTimeout(() => save(session), SAVE_DELAY_MS);
  }

  async function finish(session) {
    if (!session || session.closed) return;
    await save(session);
    if (session.saving) await session.saving;
    if (session.editor.getValue() !== session.savedContent) return;
    session.closed = true;
    session.editor.destroy();
    session.toolbarHost.replaceChildren();
    session.toolbarHost.hidden = true;
    session.mount.hidden = true;
    session.pre.hidden = false;
    session.article.classList.remove('is-editing');
    session.button.textContent = '编辑';
    setState(session, '已保存', 'is-saved');
    if (activeSession === session) activeSession = null;
  }

  async function start(article) {
    if (activeSession?.article === article) return finish(activeSession);
    if (activeSession) {
      await finish(activeSession);
      if (activeSession) return;
    }
    const id = article.dataset.articleId;
    const pre = article.querySelector('.history-article-content');
    const mount = article.querySelector('.history-editor');
    const toolbarHost = article.querySelector('.history-editor-toolbar');
    const button = article.querySelector('.history-edit-button');
    const state = article.querySelector('.history-save-state');
    article.querySelector('details').open = true;
    pre.hidden = true;
    mount.hidden = false;
    article.classList.add('is-editing');
    button.textContent = '完成编辑';

    const session = {
      id, article, pre, mount, toolbarHost, button, state,
      updatedAt: article.dataset.updatedAt,
      savedContent: pre.textContent,
      latestContent: pre.textContent,
      editor: null,
      timer: null,
      saving: null,
      saveQueued: false,
      closed: false,
    };
    activeSession = session;
    session.editor = new Vditor(mount.id, {
      cdn: VDITOR_CDN,
      mode: 'ir',
      value: session.savedContent,
      minHeight: 520,
      cache: {enable: false},
      counter: {enable: true, type: 'text'},
      toolbar: customToolbar,
      toolbarConfig: {pin: false},
      upload: {
        url: '/api/article-images',
        fieldName: 'file[]',
        accept: 'image/jpeg,image/png,image/gif,image/webp',
        max: 10 * 1024 * 1024,
        multiple: false,
      },
      input: () => scheduleSave(session),
      blur: () => save(session),
      after: () => {
        attachToolbar(session);
        setState(session, '已开启实时保存', 'is-saved');
        session.editor.focus();
      },
    });
  }

  document.querySelectorAll('.history-article').forEach((article) => {
    article.querySelector('.history-edit-button').addEventListener('click', () => start(article));
    const copyButton = article.querySelector('.history-copy-button');
    copyButton.addEventListener('click', async () => {
      const content = activeSession?.article === article
        ? activeSession.editor.getValue()
        : article.querySelector('.history-article-content').textContent;
      await navigator.clipboard.writeText(content);
      copyButton.textContent = '已复制';
      setTimeout(() => { copyButton.textContent = '复制全文'; }, 1500);
    });
  });

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden' && activeSession) save(activeSession);
  });
})();
