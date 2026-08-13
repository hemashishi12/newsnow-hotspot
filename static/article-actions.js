(() => {
  const dialog = document.querySelector('#article-dialog');
  const dialogTitle = document.querySelector('#article-dialog-title');
  const dialogStatus = document.querySelector('#article-dialog-status');
  const promptInput = document.querySelector('#article-custom-prompt');
  const promptTags = document.querySelector('#article-prompt-presets');
  const presetName = document.querySelector('#article-preset-name');
  const presetSave = document.querySelector('#article-preset-save');
  const presetStatus = document.querySelector('#article-preset-status');
  const selectedType = document.querySelector('#article-selected-type');
  const customGenerate = document.querySelector('#article-generate');
  const articleButtons = [...document.querySelectorAll('.article-generate-button')];
  const customizeButtons = [...document.querySelectorAll('.article-customize-button')];
  let currentTopicId = null;
  let currentTopicTitle = '';
  let currentArticleType = 'standard';
  let promptData = null;

  const typeName = articleType => articleType === 'long' ? '深度长文' : '爆款文章';
  const articleUrl = (topicId, articleType) =>
    `/api/topics/${topicId}/article?article_type=${articleType}`;

  const setChoice = choice => {
    currentArticleType = choice.article_type;
    promptInput.value = choice.prompt;
    selectedType.textContent = `当前：${choice.name}`;
    dialogStatus.textContent = `已选择「${choice.name}」，本次将生成${typeName(choice.article_type)}。`;
    promptTags.querySelectorAll('.article-prompt-tag').forEach(tag => {
      tag.classList.toggle('is-active', tag.dataset.choiceKey === choice.key);
    });
  };

  const addPromptTag = (choice, fixed = false) => {
    const tag = document.createElement('div');
    tag.className = `article-prompt-tag${fixed ? ' fixed-prompt-tag' : ''}`;
    tag.dataset.choiceKey = choice.key;
    const applyButton = document.createElement('button');
    applyButton.type = 'button';
    applyButton.className = 'article-prompt-tag-apply';
    applyButton.textContent = choice.name;
    applyButton.addEventListener('click', () => setChoice(choice));
    tag.append(applyButton);
    if (!fixed) {
      const deleteButton = document.createElement('button');
      deleteButton.type = 'button';
      deleteButton.className = 'article-prompt-tag-delete';
      deleteButton.textContent = '×';
      deleteButton.setAttribute('aria-label', `删除提示词标签「${choice.name}」`);
      deleteButton.title = `删除「${choice.name}」`;
      deleteButton.addEventListener('click', async () => {
        if (!window.confirm(`确定删除提示词标签「${choice.name}」吗？`)) return;
        const response = await fetch(`/api/article-prompt-presets/${choice.id}`, {method: 'DELETE'});
        if (!response.ok) {
          const error = await response.json();
          presetStatus.textContent = error.error || '删除标签失败';
          return;
        }
        presetStatus.textContent = `已删除「${choice.name}」`;
        await loadPrompts();
      });
      tag.append(deleteButton);
    }
    promptTags.append(tag);
  };

  const loadPrompts = async () => {
    const response = await fetch('/api/article-prompts?article_type=all');
    promptData = await response.json();
    if (!response.ok) throw new Error(promptData.error || '无法读取文章提示词');
    promptTags.replaceChildren();
    promptData.defaults.forEach((item, index) => addPromptTag({...item, key: `fixed-${index}`}, true));
    promptData.presets.forEach(item => addPromptTag({...item, key: `preset-${item.id}`}));
    const standard = promptData.defaults.find(item => item.article_type === 'standard');
    setChoice({...standard, key: 'fixed-0'});
  };

  const watchJob = async (button, jobId, originalLabel) => {
    for (let attempt = 0; attempt < 900; attempt += 1) {
      await new Promise(resolve => setTimeout(resolve, 2000));
      const response = await fetch(`/api/article-jobs/${jobId}`);
      const job = await response.json();
      if (!response.ok) throw new Error(job.error || '无法读取写作任务');
      if (job.status === 'success') {
        button.textContent = '✓ 写作完成';
        button.classList.add('article-task-success');
        return;
      }
      if (job.status === 'failed') throw new Error(job.error || '后台写作失败');
    }
    button.textContent = originalLabel;
    button.disabled = false;
  };

  const startBackgroundArticle = async (button, topicId, articleType, prompt = '') => {
    const originalLabel = button.dataset.label || button.textContent;
    button.disabled = true;
    button.textContent = '已提交后台…';
    button.classList.remove('article-task-success', 'article-task-error');
    try {
      const response = await fetch(articleUrl(topicId, articleType), {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({background: true, prompt}),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '无法创建后台写作任务');
      button.textContent = '后台写作中…';
      await watchJob(button, data.job_id, originalLabel);
    } catch (error) {
      button.textContent = '写作失败，点击重试';
      button.title = error.message;
      button.classList.add('article-task-error');
      button.disabled = false;
    }
  };

  articleButtons.forEach(button => {
    button.addEventListener('click', () => {
      startBackgroundArticle(button, button.dataset.topicId, button.dataset.articleType);
    });
  });

  customizeButtons.forEach(button => {
    button.addEventListener('click', async () => {
      currentTopicId = button.dataset.topicId;
      currentTopicTitle = button.dataset.topicTitle;
      dialogTitle.textContent = `${currentTopicTitle} · 定制提示词`;
      presetStatus.textContent = '';
      customGenerate.disabled = true;
      dialog.showModal();
      try {
        await loadPrompts();
        customGenerate.disabled = false;
      } catch (error) {
        dialogStatus.textContent = error.message;
        dialogStatus.classList.add('error');
      }
    });
  });

  presetSave.addEventListener('click', async () => {
    presetStatus.textContent = '';
    try {
      const response = await fetch('/api/article-prompt-presets', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          name: presetName.value,
          prompt: promptInput.value,
          article_type: currentArticleType,
        }),
      });
      const saved = await response.json();
      if (!response.ok) throw new Error(saved.error || '保存标签失败');
      presetName.value = '';
      await loadPrompts();
      const savedChoice = {...saved, key: `preset-${saved.id}`};
      setChoice(savedChoice);
      presetStatus.textContent = `已保存「${saved.name}」`;
    } catch (error) {
      presetStatus.textContent = error.message;
    }
  });

  customGenerate.addEventListener('click', () => {
    const topicButton = customizeButtons.find(button => button.dataset.topicId === currentTopicId);
    topicButton.dataset.label = '定制提示词';
    dialog.close();
    startBackgroundArticle(topicButton, currentTopicId, currentArticleType, promptInput.value);
  });

  document.querySelector('#article-dialog-close').addEventListener('click', () => dialog.close());
})();
