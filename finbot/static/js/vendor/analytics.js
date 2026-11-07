/* Read-only analytics; every database value is rendered as text. */
(() => {
  'use strict';
  const API = '/vendor/api/v1/experiments/analytics';
  const $ = id => document.getElementById(`an-${id}`);
  const node = (tag, text, cls) => {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = String(text);
    if (cls) item.className = cls;
    return item;
  };
  const number = v => v == null ? '—' : Number(v).toLocaleString('ru-RU', {maximumFractionDigits: 4});
  const percent = v => v == null ? '—' : `${number(v * 100)}%`;
  const rate = r => r ? `${percent(r.value)} · ${r.positive}/${r.n}${r.wilson95 ? ` [${percent(r.wilson95[0])}; ${percent(r.wilson95[1])}]` : ''}` : '—';
  const names = {no_cascade:'Нет каскада', dirty_data:'Dirty data', half_cascade:'Half cascade', midchain_cascade:'Mid-chain cascade', full_cascade:'Full cascade', correct:'Без ошибки', error:'Ошибка'};
  const state = {catalog: null, report: null, applied: null, offset: 0, total: 0, version: 0, rowVersion: 0, catalogVersion: 0};
  function message(text, error = false) {
    $('message').textContent = text;
    $('message').className = `exp-message${error ? ' error' : ''}`;
  }
  async function api(path, body) {
    const options = {credentials:'same-origin', headers:{Accept:'application/json'}};
    if (body !== undefined) {
      options.method = 'POST';
      options.headers['Content-Type'] = 'application/json';
      options.headers['X-CSRF-Token'] = document.querySelector('meta[name="csrf-token"]')?.content || '';
      options.body = JSON.stringify(body);
    }
    const response = await fetch(API + path, options);
    if (response.redirected || !response.headers.get('content-type')?.includes('application/json')) throw new Error('Сервер вернул страницу вместо данных. Проверьте вход и адрес приложения.');
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : JSON.stringify(result.detail || result));
    return result;
  }
  async function action(button, fn) {
    button.disabled = true;
    try { await fn(); } catch (error) { message(error.message, true); }
    finally { button.disabled = false; }
  }
  function values(id) { return Array.from($(id).selectedOptions, o => o.value); }
  function jsonObject(id) {
    let value;
    try { value = JSON.parse($(id).value); } catch { throw new Error(`Поле ${id}: нужен корректный JSON.`); }
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`Поле ${id}: нужен JSON-объект.`);
    return value;
  }
  function filters() {
    return {source:$('source').value, models:values('models'), run_ids:values('runs'), series_ids:values('series'),
      scenario_ids:values('scenarios'), agents:values('agents'),
      start:$('start').value ? new Date($('start').value).toISOString() : null,
      end:$('end').value ? new Date($('end').value).toISOString() : null,
      include_demo:$('demo').checked, config:jsonObject('config'), metadata:jsonObject('metadata'),
      matrix:$('matrix').value, label_layer:$('labels').value, threshold:Number($('threshold').value),
      bins:Number($('bins').value), k:Number($('k').value)};
  }
  function options(id, items) {
    $(id).replaceChildren(...items.map(([value, text]) => {
      const option = node('option', text); option.value = value; return option;
    }));
  }
  function table(target, headers, rows) {
    const element = node('table', undefined, 'an-table');
    const head = node('thead'), heading = node('tr');
    headers.forEach(h => { const cell = node('th', h); cell.scope = 'col'; heading.append(cell); });
    head.append(heading); element.append(head);
    const body = node('tbody');
    rows.forEach(row => {
      const tr = node('tr');
      row.forEach(value => { const td = node('td'); td.append(value instanceof Node ? value : document.createTextNode(value == null ? '—' : String(value))); tr.append(td); });
      body.append(tr);
    });
    element.append(body);
    target.replaceChildren(element);
    if (!rows.length) target.append(node('p', 'Нет подходящих наблюдений.', 'exp-help'));
  }
  function stats(id, items) {
    $(id).replaceChildren(...items.map(([title, value, hint]) => {
      const box = node('div', undefined, 'an-stat');
      box.append(node('small', title), node('strong', value));
      if (hint) box.append(node('small', hint));
      return box;
    }));
  }
  function jsonCell(value) {
    if (value == null || typeof value !== 'object') return value;
    const details = node('details');
    details.append(node('summary', Array.isArray(value) ? `${value.length} элементов` : `${Object.keys(value).length} полей`), node('pre', JSON.stringify(value, null, 2)));
    return details;
  }
  function heatmap(target, matrix) {
    const element = node('table', undefined, 'an-table an-heatmap');
    const heading = node('tr');
    heading.append(node('th', 'Эталон ↓ / Предсказание →'));
    matrix.labels.forEach(label => { const th = node('th', names[label] || label); th.scope = 'col'; heading.append(th); });
    const thead = node('thead'); thead.append(heading); element.append(thead);
    const body = node('tbody');
    matrix.counts.forEach((row, i) => {
      const tr = node('tr'), th = node('th', names[matrix.labels[i]] || matrix.labels[i]); th.scope = 'row'; tr.append(th);
      const rowN = row.reduce((a, b) => a + b, 0);
      row.forEach((count, j) => {
        const denominator = $('normalize').value === 'row' ? rowN : matrix.n;
        const share = denominator ? count / denominator : 0;
        const td = node('td', $('normalize').value === 'count' ? count : (denominator ? percent(share) : '—'));
        td.style.backgroundColor = count ? (i === j ? `rgba(30,145,125,${0.22 + 0.78 * share})` : `rgba(195,65,85,${0.22 + 0.78 * share})`) : '#0c1421';
        td.append(node('small', `n = ${count}`));
        td.title = `${names[matrix.labels[i]] || matrix.labels[i]} → ${names[matrix.labels[j]] || matrix.labels[j]}: ${count}`;
        tr.append(td);
      });
      body.append(tr);
    });
    element.append(body); target.replaceChildren(element);
  }
  function renderMatrix() {
    const r = state.report; if (!r) return;
    heatmap($('heatmap'), r.matrix);
    $('matrix-size').textContent = `n = ${r.matrix.n} · исключено ${r.matrix.excluded}`;
    $('matrix-help').textContent = r.filters.matrix === 'detector'
      ? 'Строки — ручные метки завершённых сценариев. Все типы детектора, включая reasoning_error, text_propagation и needs_review, показаны отдельно. Различия таксономий требуют разбора; автоматического переименования нет.'
      : `Эталон — error_present внешнего судьи; сигнал ошибки — confidence < ${r.filters.threshold}. Сравнение с тем же судьёй не заменяет независимую разметку.`;
    stats('scores', [['Accuracy', percent(r.matrix.accuracy)], ['Macro F1', number(r.matrix.macro_f1)], ['Сравнимых наблюдений', r.matrix.n]]);
    table($('class-scores'), ['Класс', 'Support', 'Precision', 'Recall', 'F1'], r.matrix.per_class.map(c => [names[c.label] || c.label, c.support, number(c.precision), number(c.recall), number(c.f1)]));
    $('model-matrices').replaceChildren();
    r.matrix.by_model.forEach(matrix => {
      const box = node('details'), container = node('div', undefined, 'an-scroll');
      box.append(node('summary', `${matrix.model} · n=${matrix.n}`), container); heatmap(container, matrix); $('model-matrices').append(box);
    });
  }
  function spark(values) {
    const ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 180 45'); svg.setAttribute('class', 'an-spark');
    svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', values.map(number).join(' → '));
    let segment = [];
    const draw = () => {
      if (!segment.length) return;
      const line = document.createElementNS(ns, 'polyline');
      line.setAttribute('points', segment.join(' ')); line.setAttribute('fill', 'none'); line.setAttribute('stroke', '#7ee7ce'); line.setAttribute('stroke-width', '2'); svg.append(line); segment = [];
    };
    values.forEach((v, i) => { if (v == null) { draw(); return; } segment.push(`${5 + 170 * i / Math.max(1, values.length - 1)},${40 - v * 35}`); }); draw();
    return svg;
  }
  function render() {
    const r = state.report, c = r.coverage, s = r.summary;
    $('results').hidden = false; $('export').disabled = false;
    stats('coverage', [['Прогоны', c.runs], ['Сценарии', c.cases], ['Завершены без demo', c.completed], ['Ручные метки', c.human_labels], ['Итерации', c.iterations]]);
    $('statuses').textContent = `Исполнение: ${JSON.stringify(c.execution_statuses)} · Оценщик: ${JSON.stringify(c.judge_statuses)} · Детектор: ${JSON.stringify(c.detector_types)}`;
    renderMatrix();
    const typeRows = r.type_map.rows.map(row => {
      const total = row.counts.reduce((a,b) => a+b, 0);
      return [row.model, ...row.counts.map(count => {
        const cell = node('div', `${count} · ${percent(count/total)}`);
        cell.style.backgroundColor = `rgba(42,165,145,${count ? 0.2 + 0.8 * count/total : 0})`;
        cell.style.padding = '12px'; cell.style.borderRadius = '5px'; return cell;
      })];
    });
    table($('type-map'), ['Модель', ...r.type_map.types], typeRows);
    stats('baseline', [['P_casc', percent(s.p_casc.value), rate(s.p_casc)], ['P_pay · перевод состоялся', percent(s.p_pay.value), rate(s.p_pay)], ['Роль Payments достигнута', percent(s.p_reached_payments.value), rate(s.p_reached_payments)], [`Риск ≥1 каскада за k=${r.filters.k}`, percent(s.cascade_k), 'При независимых повторах'], ['Ошибка исхода', percent(s.outcome_failure.value), rate(s.outcome_failure)]]);
    table($('groups'), ['Модель', 'Сценарий', 'T', 'Seed', 'N / размечено', 'P_casc · 95% ДИ', 'Типы F(m,t) · количества', 'Длительность · среднее, мс'], r.groups.map(g => [g.model, g.scenario, g.temperature, g.seed, `${g.cases} / ${g.labels}`, rate(g.p_casc), JSON.stringify(g.types), number(g.duration_ms.mean)]));
    table($('dimensions'), ['Срез', 'Модель', 'Значение', 'N', 'P_casc · 95% ДИ'], Object.entries(r.dimensions).flatMap(([dimension, rows]) => rows.map(g => [dimension, g.model, g.value, g.cases, rate(g.p_casc)])));
    table($('confidence'), ['Слой', 'Модель', 'Средняя c', 'N(c)', 'Средняя Δconf', 'N(Δ)', 'Brier ↔ судья', 'ECE ↔ судья', 'N калибровки'], Object.entries(r.confidence).flatMap(([layer, data]) => [{...data, model:'Все выбранные'}, ...data.by_model].map(item => [layer, item.model, number(item.values.mean), item.values.n, number(item.delta.mean), item.delta.n, number(item.calibration.brier), number(item.calibration.ece), item.calibration.n])));
    table($('profiles'), ['Слой', 'Case', 'Роль / исполнение', 'Профиль', 'Значения', 'Δconf'], Object.entries(r.confidence).flatMap(([layer, data]) => data.profiles.map(p => [layer, p.case_id, `${p.agent} / ${p.agent_run_id}`, spark(p.values), p.values.map(number).join(' → '), number(p.delta)])));
    stats('legacy', [['Heuristic confidence', number(r.legacy.heuristic_confidence.mean), `N=${r.legacy.heuristic_confidence.n}`], ['Legacy degradation', number(r.legacy.saved_confidence_degradation.mean), `N=${r.legacy.saved_confidence_degradation.n}`], ['model_confidence', number(r.legacy.model_confidence.mean), `N=${r.legacy.model_confidence.n}`]]);
    table($('agent-stats'), ['Модель', 'Агент', 'Heuristic mean / N', 'Самооценка mean / N', 'Операционные метрики'], r.legacy.agents.map(a => [a.model, a.agent, `${number(a.heuristic_confidence.mean)} / ${a.heuristic_confidence.n}`, `${number(a.model_confidence.mean)} / ${a.model_confidence.n}`, jsonCell(a.operational)]));
    stats('features', [['Отклонение маршрута', percent(s.route_deviation.value), rate(s.route_deviation)], ['Claim assertion', percent(s.claim_assertion.value), rate(s.claim_assertion)], ['Решение без lookup', percent(s.decision_without_lookup.value), rate(s.decision_without_lookup)], ['Глубина проверки', number(s.verification_depth.mean), `N=${s.verification_depth.n}`], ['Carry-over · триграммы', percent(s.carry_over.mean), `N=${s.carry_over.n}`]]);
    table($('case-features'), ['Модель', 'Сценарий', 'Case', 'Маршрут', 'Отклонение', 'Признаки'], r.cases.map(c => [c.model, c.scenario, c.case_id, c.route.join(' → '), c.route_deviation == null ? 'Неизвестно' : c.route_deviation ? 'Да' : 'Нет', jsonCell(c.features)]));
    $('operational').textContent = JSON.stringify({metrics:s.operational, cost_by_currency:s.cost_by_currency}, null, 2);
    $('limitations').replaceChildren(...r.limitations.map(text => node('li', text)));
    $('interpretation').textContent = '';
    $('interpret').disabled = !state.catalog.interpreter_available || !c.completed;
  }
  async function loadRows() {
    if (!state.applied || !$('table').value) return;
    const version = ++state.rowVersion;
    const result = await api(`/tables/${encodeURIComponent($('table').value)}?offset=${state.offset}&limit=25&all_rows=${$('all-rows').checked}`, state.applied);
    if (version !== state.rowVersion) return;
    state.total = result.total;
    const columns = [...new Set(result.rows.flatMap(row => Object.keys(row)))];
    table($('rows'), columns, result.rows.map(row => columns.map(key => jsonCell(row[key]))));
    $('page').textContent = result.available ? `${state.total ? state.offset + 1 : 0}–${Math.min(state.offset + 25, state.total)} из ${state.total}. ${$('all-rows').checked ? 'Все строки источника.' : 'Применены фильтры последнего построенного отчёта.'}` : 'Таблица отсутствует в схеме источника.';
    $('prev').disabled = state.offset === 0; $('next').disabled = state.offset + 25 >= state.total;
  }
  async function query() {
    const body = filters(), version = ++state.version;
    $('export').disabled = true;
    message('Вычисляем показатели по выбранным данным…');
    const report = await api('/query', body);
    if (version !== state.version) return;
    state.report = report; state.applied = body; state.offset = 0;
    render(); await loadRows();
    message(`Отчёт построен · ${report.coverage.runs} прогонов · источник: ${body.source}. Изменённые фильтры применяются кнопкой «Построить аналитику».`);
  }
  async function catalog(source = 'auto') {
    const version = ++state.catalogVersion;
    ++state.version; ++state.rowVersion;
    $('results').hidden = true; $('export').disabled = true; state.report = null; state.applied = null;
    message('Читаем каталог, таблицы и сохранённые настройки…');
    const result = await api(`/catalog?source=${encodeURIComponent(source)}`);
    if (version !== state.catalogVersion) return;
    state.catalog = result; $('source').value = result.source; $('scope').textContent = result.scope;
    $('source').querySelector('option[value="archive"]').disabled = !result.archive_allowed || !result.archive_configured;
    const runs = result.runs;
    options('models', [...new Set(runs.map(r => r.model).filter(Boolean))].sort().map(v => [v, v]));
    options('runs', runs.map(r => [r.id, `${r.model || '—'} · ${r.started_at || r.metadata?.created_at || 'без даты'} · ${r.source_run_id} · ${r.status}${r.is_demo ? ' · DEMO' : ''}`]));
    options('series', [...new Set(runs.map(r => r.series_id).filter(Boolean))].map(v => [v, v]));
    options('scenarios', result.scenarios.map(v => [v, v]));
    options('agents', result.agents.map(v => [v, v]));
    options('table', result.tables.map(t => [t.table, t.table]));
    table($('inventory'), ['Таблица', 'Строк в источнике', 'Колонки'], result.tables.map(t => [t.table, t.available === false ? 'Нет в схеме' : t.rows, t.columns.join(', ')]));
    table($('run-configs'), ['Run ID', 'Модель', 'Config', 'Metadata'], runs.map(r => [r.id, r.model, jsonCell(r.config), jsonCell(r.metadata)]));
    await query();
  }
  $('filter').addEventListener('submit', event => {event.preventDefault(); action($('apply'), query);});
  $('source').addEventListener('change', () => action($('apply'), () => catalog($('source').value)));
  $('reload').addEventListener('click', () => action($('reload'), () => catalog($('source').value)));
  $('filter').addEventListener('reset', () => {const source = $('source').value; setTimeout(() => {$('source').value = source; action($('apply'), query);}, 0);});
  $('normalize').addEventListener('change', renderMatrix);
  $('table').addEventListener('change', () => {state.offset = 0; loadRows().catch(e => message(e.message, true));});
  $('all-rows').addEventListener('change', () => {state.offset = 0; loadRows().catch(e => message(e.message, true));});
  $('prev').addEventListener('click', () => {state.offset = Math.max(0, state.offset - 25); loadRows().catch(e => message(e.message, true));});
  $('next').addEventListener('click', () => {if (state.offset + 25 < state.total) state.offset += 25; loadRows().catch(e => message(e.message, true));});
  $('export').addEventListener('click', () => {
    if (!state.report) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(state.report, null, 2)], {type:'application/json'}));
    const link = node('a'); link.href = url; link.download = 'cascademap-analytics.json'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  $('interpret').addEventListener('click', () => action($('interpret'), async () => {
    const version = state.version;
    $('interpretation').textContent = 'Анализируем агрегаты…';
    const result = await api('/interpret', state.applied);
    if (version === state.version) $('interpretation').textContent = `${result.model}\n${result.text}\n\nUsage: ${JSON.stringify(result.usage)}`;
  }));
  action($('apply'), () => catalog());
})();
