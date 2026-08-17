(() => {
  const toolbars = [...document.querySelectorAll('[data-article-batch]')];
  if (!toolbars.length) return;

  const selectors = [...document.querySelectorAll('.article-topic-select')];
  const articleButtons = toolbars.flatMap(toolbar => [...toolbar.querySelectorAll('[data-batch-article-type]')]);
  const videoButtons = toolbars.flatMap(toolbar => [...toolbar.querySelectorAll('[data-batch-video]')]);
  const buttons = [...articleButtons, ...videoButtons];
  const selected = () => selectors.filter(input => input.checked);
  const typeName = type => type === 'long' ? '深度长文' : '头条文章';
  const setStatus = message => toolbars.forEach(toolbar => {
    toolbar.querySelector('[data-batch-status]').textContent = message;
  });

  const update = (anchor = null) => {
    const amount = selected().length;
    const anchorTop = anchor?.getBoundingClientRect().top;
    toolbars.forEach(toolbar => {
      toolbar.hidden = amount === 0;
      toolbar.querySelector('[data-batch-count]').textContent = amount;
    });
    if (anchor && anchorTop !== undefined) {
      const restoreAnchor = () => {
        const layoutShift = anchor.getBoundingClientRect().top - anchorTop;
        if (Math.abs(layoutShift) > 0.5) window.scrollBy(0, layoutShift);
      };
      restoreAnchor();
      window.requestAnimationFrame(() => {
        restoreAnchor();
        window.requestAnimationFrame(() => {
          restoreAnchor();
          window.setTimeout(restoreAnchor, 0);
        });
      });
    }
    buttons.forEach(button => { button.disabled = amount === 0; });
  };

  selectors.forEach(input => input.addEventListener('change', () => update(input)));
  update();

  buttons.forEach(button => button.addEventListener('click', async () => {
    const items = selected();
    if (!items.length || button.disabled) return;
    const isVideoBatch = button.hasAttribute('data-batch-video');
    const articleType = isVideoBatch ? 'standard' : button.dataset.batchArticleType;
    const actionName = isVideoBatch ? '文章并生成视频' : typeName(articleType);
    buttons.forEach(item => { item.disabled = true; });
    selectors.forEach(input => { input.disabled = true; });
    setStatus(`正在逐个提交${actionName}…`);
    let submitted = 0;
    try {
      for (const input of items) {
        const title = input.dataset.topicTitle || '话题';
        setStatus(`正在提交第 ${submitted + 1}/${items.length} 个${actionName}：${title}`);
        const response = await fetch(
          `/api/topics/${encodeURIComponent(input.dataset.topicId)}/article?article_type=${encodeURIComponent(articleType)}`,
          {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({background: true, ...(isVideoBatch ? {follow_up_video: true} : {})}),
          },
        );
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `${title}提交失败`);
        submitted += 1;
      }
      setStatus(
        isVideoBatch
          ? `已提交 ${submitted} 个文章任务，文章完成后会自动按队列生成视频。`
          : `已提交 ${submitted} 个${typeName(articleType)}，可在铃铛查看逐个进度。`,
      );
      items.forEach(input => { input.checked = false; });
    } catch (error) {
      setStatus(`已提交 ${submitted} 个，${error.message}`);
      items.slice(submitted).forEach(input => { input.checked = true; });
    } finally {
      selectors.forEach(input => { input.disabled = false; });
      update();
    }
  }));
})();
