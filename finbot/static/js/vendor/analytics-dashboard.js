/* Overview charts use the same case denominators and Wilson intervals as the report. */
(() => {
  'use strict';
  const $ = id => document.getElementById(`an-dash-${id}`);
  const palette = ['#7ee4d1', '#f6bd60', '#b9a3ff', '#83baff', '#ff9da7'];
  const format = value => Number(value).toLocaleString('ru-RU', {maximumFractionDigits:1});
  const percent = value => `${format(value * 100)}%`;
  const parameterLabel = value => value == null ? 'По умолчанию' : Number(value).toLocaleString('ru-RU', {maximumFractionDigits:15, useGrouping:false});
  const element = (tag, text, cls) => {
    const node = document.createElement(tag);
    if (text != null) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  };
  let report, contexts = new Map();
  const metricNames = {
    harm:'Вредный исход', utility_failure:'Полезная задача провалена',
    attack:'Атака успешна', cascade:'Каскад', technical:'Сбой или прерывание'
  };
  const explanations = {
    harm:'Знаменатель включает завершённые попытки с известным исходом вреда.',
    utility_failure:'Знаменатель включает завершённые попытки с известным результатом полезной задачи.',
    attack:'Знаменатель включает завершённые попытки с применимой атакой и известным её результатом.',
    cascade:'Знаменатель включает триггерные попытки с каноническим типом в выбранном слое меток. Baseline исключён.',
    technical:'Знаменатель включает завершённые, неуспешные и прерванные попытки. Числитель включает failed и interrupted.'
  };
  function setOptions(id, entries, preferred) {
    const select = $(id), previous = preferred ?? select.value;
    select.replaceChildren(...entries.map(([value, label]) => {
      const option = element('option', label);
      option.value = value;
      return option;
    }));
    if (entries.some(([value]) => value === previous)) select.value = previous;
  }
  function filtered(rows) {
    return rows.filter(row => row.scenario === $('scenario').value &&
      (!$('model').value || row.model === $('model').value));
  }
  function rate(row) { return row.metrics[$('metric').value]; }
  function rateText(value) {
    if (!value.n) return 'Нет известных оценок';
    const bounds = value.confidence_interval;
    return `${percent(value.value)} · ${value.positive}/${value.n} · ДИ [${percent(bounds[0])}; ${percent(bounds[1])}]`;
  }
  function color(model) {
    const models = [...new Set(report.dashboard.runs.map(row => row.model))].sort();
    return palette[models.indexOf(model) % palette.length];
  }
  function drawPlot(id, rows, label) {
    const host = $(id);
    host.replaceChildren();
    if (!rows.length) {
      host.append(element('p', 'В выбранном срезе нет попыток.', 'exp-help'));
      return;
    }
    const scale = element('div', undefined, 'an-dot-scale');
    const ticks = element('div', undefined, 'an-dot-ticks');
    ticks.append(element('span', '0%'), element('span', '50%'), element('span', '100%'));
    scale.append(element('span', 'Доля события'), ticks, element('span', 'Частота и интервал'));
    host.append(scale);
    rows.forEach(row => {
      const value = rate(row), line = element('div', undefined, 'an-dot-row');
      const title = element('span', label(row), 'an-dot-label');
      title.title = `${label(row)}\n${JSON.stringify(row.config || row.configuration_signature)}`;
      const track = element('div', undefined, 'an-dot-track');
      track.setAttribute('role', 'img');
      track.setAttribute('aria-label', `${label(row)} · ${rateText(value)}`);
      if (value.n) {
        const bounds = value.confidence_interval;
        const interval = element('span', undefined, 'an-dot-interval');
        interval.style.left = `${bounds[0] * 100}%`;
        interval.style.width = `${(bounds[1] - bounds[0]) * 100}%`;
        interval.style.backgroundColor = color(row.model);
        const point = element('span', undefined, 'an-dot-point');
        point.style.left = `${value.value * 100}%`;
        point.style.backgroundColor = color(row.model);
        track.append(interval, point);
      } else {
        track.append(element('span', 'Нет известных оценок', 'an-dot-empty'));
      }
      line.append(title, track, element('span', rateText(value), 'an-dot-value'));
      host.append(line);
    });
  }
  function table(host, headers, rows) {
    const table = element('table', undefined, 'an-table an-dashboard-table');
    const head = element('thead'), heading = element('tr');
    headers.forEach(text => {
      const cell = element('th', text); cell.scope = 'col'; heading.append(cell);
    });
    head.append(heading); table.append(head);
    const body = element('tbody');
    rows.forEach(values => {
      const row = element('tr');
      values.forEach(value => {
        const cell = element('td');
        cell.append(value instanceof Node ? value : document.createTextNode(String(value ?? '—')));
        row.append(cell);
      });
      body.append(row);
    });
    table.append(body); host.replaceChildren(table);
  }
  function refreshContexts() {
    const axis = $('axis').value;
    contexts = new Map();
    filtered(report.dashboard.cells).forEach(row => {
      const [config, protocol, versions] = row.configuration_signature;
      const fixed = {...config};
      delete fixed[axis]; delete fixed.model;
      const key = JSON.stringify([fixed, protocol, versions]);
      if (!contexts.has(key)) contexts.set(key, {config:fixed, protocol, versions, rows:[]});
      contexts.get(key).rows.push(row);
    });
    const entries = [...contexts.entries()].sort((a, b) => b[1].rows.length - a[1].rows.length);
    setOptions('context', entries.map(([key, context]) => {
      const fixed = Object.entries(context.config)
        .filter(([name]) => ['temperature', 'top_p', 'seed', 'evaluate_iterations'].includes(name))
        .map(([name, value]) => `${name}=${value}`).join(' · ');
      return [key, `${fixed} · ${context.protocol || 'протокол неизвестен'} · ${context.versions.code_revision || 'версия неизвестна'} · ${context.rows.length} ячеек`];
    }));
    renderParameters();
  }
  function drawParameterChart(rows, axis, values) {
    const host = $('parameters');
    host.replaceChildren();
    if (!rows.length) {
      host.append(element('p', 'В выбранном срезе нет попыток.', 'exp-help'));
      return;
    }
    const width = 1000, height = 380;
    const left = 70, right = 35, top = 35, bottom = 65;
    const plotWidth = width - left - right, plotHeight = height - top - bottom;
    const maximum = Math.max(1, ...rows.map(row => row.attempts));
    const numeric = values.every(value => typeof value === 'number');
    const minimumX = Math.min(...values.filter(value => value != null));
    const maximumX = Math.max(...values.filter(value => value != null));
    const x = value => {
      if (values.length === 1) return left + plotWidth / 2;
      const share = numeric ? (value - minimumX) / (maximumX - minimumX) : values.indexOf(value) / (values.length - 1);
      return left + share * plotWidth;
    };
    const y = value => top + plotHeight * (1 - value / maximum);
    const svgNode = (tag, attributes, text) => {
      const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
      Object.entries(attributes).forEach(([name, value]) => node.setAttribute(name, value));
      if (text != null) node.textContent = text;
      return node;
    };
    const svg = svgNode('svg', {viewBox:`0 0 ${width} ${height}`, class:'an-parameter-chart', role:'img', 'aria-label':`${metricNames[$('metric').value]} по ${axis}. X — значение параметра, Y — количество ошибок, от 0 до ${maximum} выполнений сценария.`});
    svg.append(svgNode('title', {}, 'Количество ошибок и попыток по значениям гиперпараметра'));
    const step = Math.max(1, Math.ceil(maximum / 5));
    const ticks = [...new Set([0, ...Array.from({length:Math.floor(maximum / step)}, (_, index) => (index + 1) * step), maximum])];
    ticks.forEach(value => {
      svg.append(svgNode('line', {x1:left, x2:width-right, y1:y(value), y2:y(value), class:'an-chart-grid'}));
      svg.append(svgNode('text', {x:left-12, y:y(value)+4, 'text-anchor':'end'}, value));
    });
    svg.append(svgNode('text', {x:left, y:18}, 'Количество ошибок / попыток'));
    values.forEach(value => {
      svg.append(svgNode('line', {x1:x(value), x2:x(value), y1:top, y2:height-bottom, class:'an-chart-grid'}));
      svg.append(svgNode('text', {x:x(value), y:height-bottom+24, 'text-anchor':'middle'}, parameterLabel(value)));
    });
    svg.append(svgNode('text', {x:left+plotWidth/2, y:height-12, 'text-anchor':'middle'}, axis));
    const models = [...new Set(rows.map(row => row.model))].sort();
    models.forEach(model => {
      const modelRows = values.map(value => rows.find(row => row.model === model && (row.configuration_signature[0][axis] ?? null) === value));
      // Separate segments preserve gaps for missing attempts and unknown outcomes.
      for (let index = 1; index < modelRows.length; index++) {
        const previous = modelRows[index-1], current = modelRows[index];
        if (previous && current) {
          svg.append(svgNode('line', {x1:x(values[index-1]), x2:x(values[index]), y1:y(previous.attempts), y2:y(current.attempts), stroke:color(model), 'stroke-dasharray':'6 5', 'stroke-width':1.5, opacity:.65}));
          if (rate(previous).n && rate(current).n) {
            svg.append(svgNode('line', {x1:x(values[index-1]), x2:x(values[index]), y1:y(rate(previous).positive), y2:y(rate(current).positive), stroke:color(model), 'stroke-width':2.5}));
          }
        }
      }
      modelRows.forEach((row, index) => {
        if (!row) return;
        const result = rate(row);
        const description = `${model} · ${axis}=${values[index] ?? 'по умолчанию'} · ошибок ${result.n ? result.positive : 'неизвестно'} · известных оценок ${result.n} · завершено ${row.completed} · попыток ${row.attempts}`;
        const attempt = svgNode('rect', {x:x(values[index])-4, y:y(row.attempts)-4, width:8, height:8, fill:'none', stroke:color(model), tabindex:0, 'aria-label':description});
        attempt.append(svgNode('title', {}, description)); svg.append(attempt);
        if (result.n) {
          const point = svgNode('circle', {cx:x(values[index]), cy:y(result.positive), r:5, fill:color(model), tabindex:0, 'aria-label':description});
          point.append(svgNode('title', {}, description)); svg.append(point);
        }
      });
    });
    const legend = element('div', undefined, 'an-parameter-legend');
    models.forEach(model => {
      const item = element('span', model); item.style.color = color(model); legend.append(item);
    });
    host.append(svg, legend, element('p', `Сплошная линия и круги — ошибки. Пунктир и квадраты — число попыток. Верхняя граница Y — ${maximum}, максимум попыток в одной точке. Наведите на точку для числа известных оценок.`, 'exp-help'));
    if (rows.some(row => !rate(row).n)) host.append(element('p', 'В этом срезе есть точки с неизвестной оценкой ошибки. Для них показано только число попыток.', 'exp-help'));
  }
  function renderParameters() {
    const context = contexts.get($('context').value), axis = $('axis').value;
    const rows = context?.rows || [];
    $('context-info').textContent = context ? JSON.stringify({parameters:context.config, protocol:context.protocol, versions:context.versions}, null, 2) : 'Нет сочетаний условий.';
    const axisValue = row => row.configuration_signature[0][axis] ?? null;
    const values = [...new Set(rows.map(axisValue))].sort((a,b) => {
      if (a == null) return b == null ? 0 : 1;
      if (b == null) return -1;
      return a - b;
    });
    const axisLabel = parameterLabel;
    drawParameterChart(rows, axis, values);
    const models = [...new Set(rows.map(row => row.model))].sort();
    table($('intervals'), ['Модель', ...values.map(value => `${axis}=${axisLabel(value)}`)], models.map(model => [model, ...values.map(value => {
      const row = rows.find(row => row.model === model && axisValue(row) === value);
      const box = element('div', undefined, 'an-interval-cell');
      if (!row) { box.append(element('span', 'Нет попыток')); return box; }
      const result = rate(row);
      if (!result.n) { box.append(element('span', 'Нет известных оценок')); }
      else {
        box.style.backgroundColor = `rgba(215, 87, 106, ${0.08 + 0.65 * result.value})`;
        box.append(element('strong', percent(result.value)), element('span', `${result.positive} из ${result.n}`),
          element('span', `ДИ ${percent(result.confidence_level)} [${percent(result.confidence_interval[0])}; ${percent(result.confidence_interval[1])}]`));
      }
      box.append(element('small', `Завершено ${row.completed} · попыток ${row.attempts}`));
      box.title = row ? JSON.stringify(row.configuration_signature) : '';
      return box;
    })]));
    $('matrix-help').textContent = `${metricNames[$('metric').value]} · Wilson ${percent(report.dashboard.confidence_level)}. Насыщенность красного растёт с долей события. В ячейке показаны частота, число событий и знаменатель, интервал и количество выполнений. Отсутствие оценок показано явно. Перекрытие интервалов само по себе не является проверкой различий.`;
  }
  function render() {
    if (!report) return;
    const rows = filtered(report.dashboard.runs);
    const known = rows.reduce((sum, row) => sum + rate(row).n, 0);
    const positive = rows.reduce((sum, row) => sum + rate(row).positive, 0);
    const completed = rows.reduce((sum, row) => sum + row.completed, 0);
    $('help').textContent = `${explanations[$('metric').value]} Доверие ${percent(report.dashboard.confidence_level)} задаётся в общих фильтрах. Сценарий и модель переключаются сразу. Прогоны могут отличаться несколькими параметрами; для сравнения одного параметра используйте нижний график с фиксированными условиями.`;
    $('stats').replaceChildren(...[['Прогоны', rows.length], ['Завершённые попытки', completed], ['Известных оценок', known], [metricNames[$('metric').value], `${positive} из ${known}`]].map(([title, value]) => {
      const card = element('div', undefined, 'an-stat'); card.append(element('small', title), element('strong', value)); return card;
    }));
    drawPlot('runs', rows, row => {
      const date = row.started_at ? new Date(row.started_at).toLocaleString('ru-RU') : 'Без даты';
      return `${row.model} · T=${row.config.temperature ?? 'auto'} · top_p=${row.config.top_p ?? 'auto'} · ${date} · ${row.source_run_id.slice(0,8)}`;
    });
    table($('run-table'), ['Прогон', 'Модель', 'T', 'top_p', 'Seed', 'Завершено / попыток', 'События и ДИ', 'Сбои / прерывания'], rows.map(row => [
      row.source_run_id, row.model, row.config.temperature, row.config.top_p, row.config.seed,
      `${row.completed} / ${row.attempts}`, rateText(rate(row)), `${row.statuses.failed || 0} / ${row.statuses.interrupted || 0}`
    ]));
    refreshContexts();
  }
  document.addEventListener('analytics:report', event => {
    report = event.detail;
    const scenarios = [...new Set(report.dashboard.runs.map(row => row.scenario))].sort();
    const previous = $('scenario').value;
    setOptions('scenario', scenarios.map(value => [value, value]), previous || (scenarios.includes('multiple_red_flags') ? 'multiple_red_flags' : scenarios[0]));
    setOptions('model', [['', 'Все модели'], ...[...new Set(report.dashboard.runs.map(row => row.model))].sort().map(value => [value, value])]);
    render();
  });
  ['metric', 'scenario', 'model'].forEach(id => $(id).addEventListener('change', render));
  $('axis').addEventListener('change', refreshContexts);
  $('context').addEventListener('change', renderParameters);
})();
