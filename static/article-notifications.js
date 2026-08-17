(() => {
  const root = document.querySelector('[data-notifications]'); if (!root) return;
  const list = root.querySelector('[data-notification-list]'), badge = root.querySelector('.notification-badge'); let latest = [];
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const names = {queued:'排队中', starting:'启动视频引擎', processing:'视频生成中', waiting_comments:'采集热评', generating:'AI 写作中', success:'已完成', failed:'失败'};
  const render = data => { latest = data.jobs || []; badge.hidden = !(data.unread > 0); badge.textContent = data.unread > 99 ? '99+' : data.unread; list.innerHTML = latest.length ? latest.map(job => { const p = Math.max(0, Math.min(100, Number(job.progress || 0))); const done = ['success','failed'].includes(job.status); const kind = job.notification_type === 'video' ? '视频' : '写文'; return `<article class="notification-item ${done ? 'is-done' : ''}"><div><b>${esc(job.topic_title)}</b><span>${kind} · ${esc(names[job.status] || job.status)}</span></div><progress max="100" value="${p}"></progress><small>${esc(job.message || '')} · ${p}%</small></article>`; }).join('') : '<p class="notification-empty">暂无运行中的任务</p>'; };
  const refresh = async () => { try { const r = await fetch('/api/article-jobs', {cache:'no-store'}); if (r.ok) render(await r.json()); } catch (_) {} };
  root.addEventListener('mouseenter', refresh); root.addEventListener('focusin', refresh);
  root.addEventListener('mouseleave', async () => { const jobs = latest.filter(j => ['success','failed'].includes(j.status) && !j.read_at).map(j => ({type: j.notification_type || 'article', id: j.id})); if (!jobs.length) return; await fetch('/api/article-jobs/read', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({jobs})}); await refresh(); });
  refresh(); window.setInterval(refresh, 2500);
})();
