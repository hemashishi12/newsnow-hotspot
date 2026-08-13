(() => {
  if (typeof Chart === 'undefined') return;

  const platformColors = {
    toutiao: '#d83a20',
    zhihu: '#1769aa',
    weibo: '#d97800',
    douyin: '#171713',
    baidu: '#315da8',
    thepaper: '#7b4a2f',
    'bilibili-hot-search': '#c43f72',
    'wallstreetcn-hot': '#8a6500',
    'cls-hot': '#b1432d',
    ifeng: '#8f2f3f',
    tieba: '#087f8c',
    'chongbuluo-hot': '#5a6e26',
    coolapk: '#15966f',
    douban: '#2d7a42',
    freebuf: '#5b52a3',
    'github-trending-today': '#4d5358',
    hackernews: '#dc5b1a',
    hupu: '#a55022',
    'iqiyi-hot-ranklist': '#4a8c22',
    juejin: '#2474c6',
    nowcoder: '#3d8b73',
    producthunt: '#b24b38',
    'qqvideo-tv-hotsearch': '#249b67',
    sspai: '#c2362b',
    steam: '#365b75',
    'tencent-hot': '#13765b',
    'xueqiu-hotstock': '#9a4d19',
  };

  const colorForPlatform = sourceId => {
    if (platformColors[sourceId]) return platformColors[sourceId];
    let hash = 0;
    for (const character of sourceId) hash = (hash * 31 + character.charCodeAt(0)) >>> 0;
    return `hsl(${hash % 360} 58% 39%)`;
  };

  document.querySelectorAll('.rank-chart-panel').forEach((panel, panelIndex) => {
    const canvas = panel.querySelector('.rank-chart');
    const payloadNode = panel.querySelector('.rank-chart-data');
    const legend = panel.querySelector('.rank-chart-legend');
    if (!canvas || !payloadNode) return;

    const payload = JSON.parse(payloadNode.textContent);
    const datasets = payload.series.map(series => {
      const color = colorForPlatform(series.source_id);
      return {
        label: series.source_name,
        data: series.values,
        yAxisID: series.axis,
        borderColor: color,
        backgroundColor: color,
        borderWidth: 2,
        borderDash: series.axis === 'rank-high' ? [6, 4] : [],
        pointRadius: 2.5,
        pointHoverRadius: 5,
        pointBorderWidth: 0,
        tension: 0.22,
        spanGaps: false,
      };
    });

    if (legend) {
      const groups = [
        {axis: 'rank-low', className: 'legend-left'},
        {axis: 'rank-high', className: 'legend-right'},
      ];
      groups.forEach(group => {
        const groupNode = document.createElement('div');
        groupNode.className = `rank-chart-legend-group ${group.className}`;
        payload.series
          .filter(series => series.axis === group.axis)
          .forEach(series => {
            const item = document.createElement('span');
            item.className = 'rank-chart-legend-item';
            const swatch = document.createElement('i');
            swatch.style.backgroundColor = colorForPlatform(series.source_id);
            const label = document.createElement('b');
            label.textContent = series.source_name;
            item.append(swatch, label);
            groupNode.append(item);
          });
        legend.append(groupNode);
      });
      legend.classList.toggle('is-dual', payload.separate_axes);
    }

    const rankScale = (position, drawGrid, axisId) => {
      const values = datasets
        .filter(dataset => dataset.yAxisID === axisId)
        .flatMap(dataset => dataset.data)
        .filter(value => Number.isFinite(value));
      const lowestRank = Math.min(...values);
      const highestRank = Math.max(...values);
      const padding = Math.max(1, Math.ceil((highestRank - lowestRank) * 0.08));
      return {
        type: 'linear',
        position,
        reverse: true,
        min: Math.max(0, lowestRank - padding),
        max: highestRank + padding,
        grid: {color: 'rgba(23,23,19,.10)', drawOnChartArea: drawGrid},
        border: {color: 'rgba(23,23,19,.35)'},
        ticks: {
          color: '#6d6a60',
          precision: 0,
          maxTicksLimit: 5,
          font: {family: 'Georgia, serif', size: 9, weight: 'bold'},
          callback: value => `#${value}`,
        },
      };
    };

    const scales = {
      x: {
        grid: {display: false},
        border: {color: 'rgba(23,23,19,.35)'},
        ticks: {
          color: '#6d6a60',
          autoSkip: true,
          maxTicksLimit: 4,
          maxRotation: 0,
          font: {size: 8, weight: 'bold'},
          callback(value) {
            const label = this.getLabelForValue(value);
            return label.replace(/^#\d+\s*/, '');
          },
        },
      },
      'rank-low': rankScale('left', true, 'rank-low'),
    };
    if (payload.separate_axes) {
      scales['rank-high'] = rankScale('right', false, 'rank-high');
    }

    new Chart(canvas, {
      type: 'line',
      data: {labels: payload.labels, datasets},
      options: {
        responsive: true,
        maintainAspectRatio: false,
        devicePixelRatio: Math.min(window.devicePixelRatio || 1, 2),
        interaction: {mode: 'nearest', intersect: false},
        animation: {duration: Math.min(420 + panelIndex * 25, 700)},
        layout: {padding: {top: 2, right: 2}},
        plugins: {
          legend: {
            display: false,
          },
          tooltip: {
            backgroundColor: '#171713',
            titleColor: '#f2efe5',
            bodyColor: '#f2efe5',
            padding: 10,
            callbacks: {
              label: context => `${context.dataset.label}  #${context.parsed.y}`,
            },
          },
        },
        scales,
      },
    });
  });
})();
