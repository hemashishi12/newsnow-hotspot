(() => {
  const ACTIVE_STATES = new Set(['queued', 'starting', 'processing']);
  const POLL_MS = 2000;

  const narrationFromMarkdown = (content) => content
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/^#{1,6}\s*/gm, '')
    .replace(/^\s*(?:>|[-+*]|\d+\.)\s+/gm, '')
    .replace(/(?:\*\*|__|~~|`)(.*?)(?:\*\*|__|~~|`)/gs, '$1')
    .replace(/<[^>]+>/g, '')
    .replace(/["'“”‘’＂＇„‟‚‛«»‹›「」『』﹁﹂﹃﹄]/g, '')
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .join('\n\n');

  const safeWebUrl = (value) => {
    try {
      const url = new URL(value);
      return ['http:', 'https:'].includes(url.protocol) ? url.href : '';
    } catch (_error) {
      return '';
    }
  };

  const videoStatusText = (job) => job?.error || job?.message || (
    job?.status === 'success' ? '口播视频已生成' : '准备生成'
  );

  const renderCreditItems = (list, items) => {
    list.replaceChildren();
    (items || []).forEach((item) => {
      const sourceUrl = safeWebUrl(item.source_page);
      const creatorUrl = safeWebUrl(item.creator_page);
      const li = document.createElement('li');
      const provider = item.provider ? item.provider[0].toUpperCase() + item.provider.slice(1) : '素材平台';
      if (creatorUrl && item.creator_name) {
        const creator = document.createElement('a');
        creator.href = creatorUrl;
        creator.target = '_blank';
        creator.rel = 'noreferrer';
        creator.textContent = item.creator_name;
        li.append(creator, document.createTextNode(` · ${provider}`));
      } else {
        li.textContent = item.creator_name ? `${item.creator_name} · ${provider}` : provider;
      }
      if (sourceUrl) {
        const source = document.createElement('a');
        source.href = sourceUrl;
        source.target = '_blank';
        source.rel = 'noreferrer';
        source.textContent = '查看素材';
        li.append(document.createTextNode(' · '), source);
      }
      list.append(li);
    });
  };

  const setHistoryCardCollapsed = (card, collapsed) => {
    card.classList.toggle('is-history-collapsed', collapsed);
    const toggle = card.querySelector('[data-history-video-toggle]');
    if (!toggle) return;
    toggle.setAttribute('aria-expanded', String(!collapsed));
    const label = toggle.querySelector('.history-video-job-toggle-label');
    if (label) label.textContent = collapsed ? '展开历史视频' : '收起视频';
  };

  const createHistoryJobCard = (job) => {
    const card = document.createElement('article');
    card.className = 'history-video-job';
    card.dataset.videoJobId = String(job.id);

    const head = document.createElement('div');
    head.className = 'history-video-job-head';
    const meta = document.createElement('div');
    meta.className = 'history-article-meta';
    const time = document.createElement('span');
    time.textContent = String(job.created_at || '').replace('T', ' ');
    const kind = document.createElement('span');
    kind.textContent = '口播视频';
    const status = document.createElement('span');
    status.className = `history-video-status history-video-job-status is-${job.status || 'idle'}`;
    status.dataset.videoJobStatus = 'true';
    status.textContent = videoStatusText(job);
    meta.append(time, kind, status);
    head.append(meta);

    const title = document.createElement('h4');
    title.className = 'history-video-job-title';
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'history-video-job-toggle';
    toggle.dataset.historyVideoToggle = 'true';
    toggle.setAttribute('aria-controls', `history-video-job-body-${job.id}`);
    const titleText = document.createElement('span');
    titleText.textContent = job.params?.title || '口播视频';
    const toggleLabel = document.createElement('span');
    toggleLabel.className = 'history-video-job-toggle-label';
    toggle.append(titleText, toggleLabel);
    title.append(toggle);
    head.append(title);
    card.append(head);

    const body = document.createElement('div');
    body.className = 'history-video-job-body';
    body.id = `history-video-job-body-${job.id}`;
    const scriptDetails = document.createElement('details');
    scriptDetails.className = 'history-video-script-details';
    const scriptSummary = document.createElement('summary');
    scriptSummary.textContent = '查看口播稿';
    const script = document.createElement('pre');
    script.textContent = job.script || '';
    scriptDetails.append(scriptSummary, script);
    body.append(scriptDetails);

    if (ACTIVE_STATES.has(job.status)) {
      const progress = document.createElement('progress');
      progress.max = 100;
      progress.value = Number(job.progress || 0);
      progress.dataset.videoJobProgress = 'true';
      body.append(progress);
    }

    if (job.status === 'success' && job.video_url) {
      const result = document.createElement('div');
      result.className = 'history-video-result';
      const video = document.createElement('video');
      video.controls = true;
      video.preload = 'metadata';
      video.src = `${job.video_url}?v=${encodeURIComponent(job.updated_at || '')}`;
      result.append(video);

      const actions = document.createElement('div');
      actions.className = 'history-video-result-actions';
      const download = document.createElement('a');
      download.href = `${job.video_url}?download=1`;
      download.textContent = '下载 MP4';
      actions.append(download);
      result.append(actions);

      const credits = document.createElement('div');
      credits.className = 'history-video-credits';
      const creditsTitle = document.createElement('b');
      creditsTitle.textContent = '素材来源';
      const creditsList = document.createElement('ul');
      renderCreditItems(creditsList, job.credits);
      credits.append(creditsTitle, creditsList);
      credits.hidden = creditsList.children.length === 0;
      result.append(credits);
      body.append(result);
    }

    card.append(body);
    toggle.addEventListener('click', () => {
      const collapsed = !card.classList.contains('is-history-collapsed');
      setHistoryCardCollapsed(card, collapsed);
      card.dispatchEvent(new CustomEvent('history-video-card-toggle', {
        detail: { collapsed },
        bubbles: true,
      }));
    });
    setHistoryCardCollapsed(card, false);
    return card;
  };

  const initArticleVideoHistory = (article, panel) => {
    const button = article.querySelector('.history-video-button');
    const form = panel.querySelector('.history-video-form');
    const script = form.elements.script;
    const count = panel.querySelector('[data-script-count]');
    const status = panel.querySelector('.history-video-status');
    const submit = form.querySelector('button[type="submit"]');
    const list = panel.querySelector('[data-video-history-list]');
    const ttsProvider = form.elements.tts_provider;
    const moneyPrinterTtsFields = [...panel.querySelectorAll('[data-tts-moneyprinter]')];
    const externalTtsFields = [...panel.querySelectorAll('[data-tts-external]')];
    const initialNode = panel.querySelector('.history-video-initial');
    let initialJobs = [];
    try {
      const parsed = JSON.parse(initialNode?.textContent || '[]');
      initialJobs = Array.isArray(parsed) ? parsed : [];
    } catch (_error) {
      initialJobs = [];
    }
    const jobs = new Map();
    const pollTimers = new Map();
    const cardStates = new Map();
    let latestJob = initialJobs[0] || null;
    let scriptTouched = false;

    const setFormDisabled = (disabled) => {
      [...form.elements].forEach((element) => { element.disabled = disabled; });
      submit.disabled = disabled;
    };

    const syncTtsFields = () => {
      const provider = ttsProvider?.value || 'moneyprinter';
      moneyPrinterTtsFields.forEach((field) => {
        field.hidden = provider !== 'moneyprinter';
        field.querySelectorAll('input, select').forEach((element) => { element.disabled = provider !== 'moneyprinter'; });
      });
      externalTtsFields.forEach((field) => {
        field.hidden = provider !== 'openai';
        field.querySelectorAll('input, select').forEach((element) => { element.disabled = provider !== 'openai'; });
      });
    };

    const findCard = (jobId) => [...list.querySelectorAll('[data-video-job-id]')]
      .find((item) => item.dataset.videoJobId === String(jobId));

    const refreshHistoryCardStates = () => {
      [...list.querySelectorAll('[data-video-job-id]')].forEach((card, index) => {
        const jobId = card.dataset.videoJobId;
        const collapsed = cardStates.has(jobId) ? cardStates.get(jobId) : index > 0;
        setHistoryCardCollapsed(card, collapsed);
      });
    };

    list.addEventListener('history-video-card-toggle', (event) => {
      const card = event.target.closest('[data-video-job-id]');
      if (card) cardStates.set(card.dataset.videoJobId, event.detail.collapsed);
    });

    const schedulePoll = (job) => {
      const jobId = String(job.id);
      clearTimeout(pollTimers.get(jobId));
      if (!ACTIVE_STATES.has(job.status)) return;
      pollTimers.set(jobId, setTimeout(async () => {
        try {
          const response = await fetch(`/api/article-videos/${job.id}`);
          const data = await response.json();
          if (!response.ok) throw new Error(data.error || '无法读取视频进度');
          upsertJob(data);
        } catch (_error) {
          schedulePoll(job);
        }
      }, POLL_MS));
    };

    const updateCardProgress = (card, job) => {
      const cardStatus = card.querySelector('[data-video-job-status]');
      if (cardStatus) {
        cardStatus.className = `history-video-status history-video-job-status is-${job.status || 'idle'}`;
        cardStatus.textContent = videoStatusText(job);
      }
      const progress = card.querySelector('[data-video-job-progress]');
      if (progress) progress.value = Number(job.progress || 0);
    };

    const upsertJob = (job, prepend = false) => {
      const jobId = String(job.id);
      const previous = jobs.get(jobId);
      jobs.set(jobId, job);
      if (!latestJob || String(latestJob.id) === jobId || prepend) latestJob = job;
      let card = findCard(job.id);
      if (!card || !previous || previous.status !== job.status) {
        const nextCard = createHistoryJobCard(job);
        if (card) card.replaceWith(nextCard);
        else if (prepend) list.prepend(nextCard);
        else list.append(nextCard);
        card = nextCard;
      } else {
        updateCardProgress(card, job);
      }
      updateCardProgress(card, job);
      refreshHistoryCardStates();
      const hasActiveJob = [...jobs.values()].some((item) => ACTIVE_STATES.has(item.status));
      setFormDisabled(hasActiveJob);
      if (latestJob && status) {
        status.className = `history-video-status is-${latestJob.status || 'idle'}`;
        status.textContent = videoStatusText(latestJob);
      }
      schedulePoll(job);
      if (button && panel.hidden) button.textContent = '查看视频';
    };

    initialJobs.forEach((job) => upsertJob(job));
    syncTtsFields();

    button?.addEventListener('click', () => {
      panel.hidden = !panel.hidden;
      button.textContent = panel.hidden
        ? (jobs.size ? '查看视频' : '生成视频')
        : '收起视频';
    });

    ttsProvider?.addEventListener('change', syncTtsFields);
    script.addEventListener('input', () => {
      scriptTouched = true;
      count.textContent = script.value.length;
    });

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      submit.disabled = true;
      if (status) status.textContent = '正在创建视频任务…';
      const data = new FormData(form);
      const payload = {
        title: data.get('title'),
        script: data.get('script'),
        aspect: data.get('aspect'),
        voice: data.get('voice'),
        voice_rate: Number(data.get('voice_rate')),
        source: data.get('source'),
        search_terms: data.get('search_terms'),
        subtitle_enabled: data.get('subtitle_enabled') === 'on',
        tts_provider: data.get('tts_provider') || 'moneyprinter',
        tts_voice: data.get('tts_voice') || '',
      };
      try {
        const response = await fetch(panel.dataset.videoEndpoint, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(payload),
        });
        const job = await response.json();
        if (!response.ok) throw new Error(job.error || '无法创建视频任务');
        upsertJob(job, true);
      } catch (error) {
        setFormDisabled(false);
        if (status) {
          status.className = 'history-video-status is-failed';
          status.textContent = error.message;
        }
      }
    });

    document.addEventListener('article-content-saved', (event) => {
      if (String(event.detail.articleId) !== String(article.dataset.articleId) || scriptTouched) return;
      script.value = narrationFromMarkdown(event.detail.content);
      count.textContent = script.value.length;
    });
  };

  document.querySelectorAll('.history-article').forEach((article) => {
    const articleId = article.dataset.articleId;
    const endpoint = article.dataset.videoEndpoint || `/api/articles/${articleId}/videos`;
    const button = article.querySelector('.history-video-button');
    const panel = article.querySelector('.history-video-panel');
    if (panel?.dataset.videoHistory === 'true') {
      initArticleVideoHistory(article, panel);
      return;
    }
    const form = panel.querySelector('.history-video-form');
    const script = form.elements.script;
    const count = panel.querySelector('[data-script-count]');
    const status = panel.querySelector('.history-video-status');
    const progress = panel.querySelector('progress');
    const submit = form.querySelector('button[type="submit"]');
    const result = panel.querySelector('.history-video-result');
    const video = result.querySelector('video');
    const download = result.querySelector('[data-video-download]');
    const regenerate = result.querySelector('[data-video-regenerate]');
    const credits = result.querySelector('.history-video-credits');
    const creditsList = credits.querySelector('ul');
    const ttsProvider = form.elements.tts_provider;
    const moneyPrinterTtsFields = [...panel.querySelectorAll('[data-tts-moneyprinter]')];
    const externalTtsFields = [...panel.querySelectorAll('[data-tts-external]')];
    let pollTimer = null;
    let scriptTouched = false;
    let currentJob = JSON.parse(panel.querySelector('.history-video-initial').textContent || 'null');

    const setFormDisabled = (disabled) => {
      [...form.elements].forEach((element) => { element.disabled = disabled; });
      submit.disabled = disabled;
    };

    const syncTtsFields = () => {
      const provider = ttsProvider?.value || 'moneyprinter';
      moneyPrinterTtsFields.forEach((field) => {
        field.hidden = provider !== 'moneyprinter';
        field.querySelectorAll('input, select').forEach((element) => { element.disabled = provider !== 'moneyprinter'; });
      });
      externalTtsFields.forEach((field) => {
        field.hidden = provider !== 'openai';
        field.querySelectorAll('input, select').forEach((element) => { element.disabled = provider !== 'openai'; });
      });
    };

    const renderCredits = (items) => {
      creditsList.replaceChildren();
      (items || []).forEach((item) => {
        const sourceUrl = safeWebUrl(item.source_page);
        const creatorUrl = safeWebUrl(item.creator_page);
        const li = document.createElement('li');
        const provider = item.provider ? item.provider[0].toUpperCase() + item.provider.slice(1) : '素材平台';
        if (creatorUrl && item.creator_name) {
          const creator = document.createElement('a');
          creator.href = creatorUrl;
          creator.target = '_blank';
          creator.rel = 'noreferrer';
          creator.textContent = item.creator_name;
          li.append(creator, document.createTextNode(` · ${provider}`));
        } else {
          li.textContent = item.creator_name ? `${item.creator_name} · ${provider}` : provider;
        }
        if (sourceUrl) {
          const source = document.createElement('a');
          source.href = sourceUrl;
          source.target = '_blank';
          source.rel = 'noreferrer';
          source.textContent = '查看素材';
          li.append(document.createTextNode(' · '), source);
        }
        creditsList.append(li);
      });
      credits.hidden = creditsList.children.length === 0;
    };

    const renderJob = (job) => {
      currentJob = job;
      clearTimeout(pollTimer);
      const active = ACTIVE_STATES.has(job?.status);
      setFormDisabled(active);
      syncTtsFields();
      progress.hidden = !active;
      progress.value = Number(job?.progress || 0);
      status.className = `history-video-status is-${job?.status || 'idle'}`;
      status.textContent = job?.error || job?.message || '准备生成';
      result.hidden = job?.status !== 'success';
      if (job?.status === 'success') {
        video.src = `${job.video_url}?v=${encodeURIComponent(job.updated_at || '')}`;
        download.href = `${job.video_url}?download=1`;
        renderCredits(job.credits);
      }
      if (active) {
        pollTimer = setTimeout(async () => {
          try {
            const response = await fetch(`/api/article-videos/${job.id}`);
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || '无法读取视频进度');
            renderJob(data);
          } catch (error) {
            status.textContent = error.message;
            pollTimer = setTimeout(() => renderJob(job), POLL_MS);
          }
        }, POLL_MS);
      }
    };

    if (button) {
      button.addEventListener('click', () => {
        panel.hidden = !panel.hidden;
        const details = article.querySelector('details');
        if (details) details.open = true;
        button.textContent = panel.hidden ? '生成视频' : '收起视频';
        if (!panel.hidden && currentJob) renderJob(currentJob);
      });
    }

    ttsProvider?.addEventListener('change', syncTtsFields);
    syncTtsFields();

    script.addEventListener('input', () => {
      scriptTouched = true;
      count.textContent = script.value.length;
    });

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      submit.disabled = true;
      status.textContent = '正在创建视频任务…';
      const data = new FormData(form);
      const payload = {
        title: data.get('title'),
        script: data.get('script'),
        aspect: data.get('aspect'),
        voice: data.get('voice'),
        voice_rate: Number(data.get('voice_rate')),
        source: data.get('source'),
        search_terms: data.get('search_terms'),
        subtitle_enabled: data.get('subtitle_enabled') === 'on',
        tts_provider: data.get('tts_provider') || 'moneyprinter',
        tts_voice: data.get('tts_voice') || '',
      };
      try {
        const response = await fetch(endpoint, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(payload),
        });
        const job = await response.json();
        if (!response.ok) throw new Error(job.error || '无法创建视频任务');
        renderJob(job);
      } catch (error) {
        setFormDisabled(false);
        status.className = 'history-video-status is-failed';
        status.textContent = error.message;
      }
    });

    regenerate.addEventListener('click', () => {
      result.hidden = true;
      currentJob = null;
      setFormDisabled(false);
      status.textContent = '可调整参数后重新生成';
      form.scrollIntoView({behavior: 'smooth', block: 'nearest'});
    });

    document.addEventListener('article-content-saved', (event) => {
      if (String(event.detail.articleId) !== String(articleId) || scriptTouched) return;
      script.value = narrationFromMarkdown(event.detail.content);
      count.textContent = script.value.length;
    });

    if (currentJob && ACTIVE_STATES.has(currentJob.status)) renderJob(currentJob);
  });
})();
