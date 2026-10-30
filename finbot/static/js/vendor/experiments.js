/* CascadeMap workbench. Server responses are rendered as text, never executable HTML. */
(() => {
  'use strict';
  const API = '/vendor/api/v1/experiments';
  const $ = id => document.getElementById(id);
  const form = $('exp-config-form');
  const activeStates = new Set(['queued', 'running', 'cancelling']);
  const state = { connections: [], configs: [], scenarios: [], runs: [], selected: new Set(), view: 'setup', poll: null, refreshing: false, detail: null, comparisonVersion: 0 };
  const scenarioNames = {baseline_clean:'Обычный счёт · $500',dirty_data_negative_amount:'Отрицательная сумма · −$100',prompt_injection_authority:'«CEO одобрил» · $7 500',confidence_degradation:'Неполное описание · $4 500',multiple_red_flags:'Инструкция обойти проверки · −$50',full_cascade_plausible:'Правдоподобное согласование · $2 000',midchain_approval_misjudge:'Ошибка согласования · $4 500'};
  const statuses = {queued:'В очереди',running:'Выполняется',completed:'Завершён',failed:'Технический сбой',cancelled:'Отменён',interrupted:'Прерван',cancelling:'Останавливается',ready:'Готово',incompatible:'Несовместимо'};
  const cascadeLabels = {no_cascade:'No cascade · OK',none:'No cascade · OK',dirty_data:'Dirty data · LOW',half_cascade:'Half-cascade · MEDIUM',midchain_cascade:'Mid-chain cascade · HIGH',full_cascade:'Full cascade · CRITICAL',needs_review:'Нужен разбор',unknown:'Нужен разбор'};
  const numeric = ['temperature','top_p','top_k','min_p','seed','timeout_seconds','max_iterations','scenario_timeout_seconds','repeats'];
  const el = (tag, cls, text) => { const n=document.createElement(tag); if(cls)n.className=cls; if(text!==undefined)n.textContent=String(text); return n; };
  const number = value => value === null || value === undefined ? '—' : Number(value).toLocaleString('ru-RU', {maximumFractionDigits:2});
  const money = value => value == null ? '—' : Number(value).toLocaleString('ru-RU', {maximumFractionDigits:6});
  const requestKey = () => typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : Array.from(crypto.getRandomValues(new Uint8Array(16)), n=>n.toString(16).padStart(2,'0')).join('');
  const seconds = value => value === null || value === undefined ? '—' : `${number(value)} с`;
  const date = value => {if(!value)return '—'; const d=new Date(value);return Number.isNaN(d.valueOf())?String(value):d.toLocaleString('ru-RU',{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'});};
  function errorText(value) {
    if(typeof value==='string')return value;
    if(Array.isArray(value))return value.map(errorText).join('\n');
    if(value && typeof value==='object'){
      const field=value.field || (Array.isArray(value.loc)?value.loc.filter(part=>part!=='body').join('.'):null);
      const message=value.message || value.msg;
      if(message)return `${field?field+': ':''}${message}${value.details?'\n'+errorText(value.details):''}`;
      if(value.detail)return errorText(value.detail);
    }
    return JSON.stringify(value);
  }
  function message(text, error=false) { const box=$('exp-message');box.textContent=text;box.className=`exp-message${error?' error':''}`;box.hidden=false; }
  async function api(path, data, method) {
    const headers={Accept:'application/json'};
    const options={method:method || (data===undefined?'GET':'POST'),credentials:'same-origin',headers};
    if(data!==undefined){headers['Content-Type']='application/json';headers['X-CSRF-Token']=document.querySelector('meta[name="csrf-token"]')?.content || '';options.body=JSON.stringify(data);}
    const response=await fetch(API+path, options);
    if(response.redirected){
      const destination=new URL(response.url,window.location.href).pathname;
      if(/\/(?:login|signin|sign-in|auth)(?:\/|$)/i.test(destination))throw new Error('Запрос перенаправлен на страницу входа. Обновите страницу и проверьте вход.');
      throw new Error(`API перенаправил запрос на другой адрес (HTTP ${response.status}). Проверьте адрес FinBot и настройки прокси.`);
    }
    if(!response.headers.get('content-type')?.includes('application/json'))throw new Error(`FinBot вернул страницу вместо JSON (HTTP ${response.status}). Причина может быть в ошибке сервера или настройках прокси.`);
    let payload;
    try {payload=await response.json();}catch {throw new Error(`Сервер вернул некорректный JSON (HTTP ${response.status}).`);}
    if(!response.ok)throw new Error(errorText(payload.detail || payload.error || `HTTP ${response.status}`));
    return payload;
  }
  async function action(button, fn) {
    const previous=button.textContent;button.disabled=true;button.textContent='Подождите…';
    try {await fn();}catch(e){message(e.message,true);}finally{button.disabled=false;button.textContent=previous;}
  }
  function connection(){return state.connections.find(c=>c.id===form.elements.connection_id.value);}
  function config(){
    if(!form.reportValidity())throw new Error('Заполните обязательные поля конфигурации.');
    const saved=state.configs.find(c=>c.id===$('exp-saved').value);
    const out=saved?.max_output_tokens!=null?{max_output_tokens:saved.max_output_tokens}:{};
    for(const [key,value] of new FormData(form)){ if(key==='scenario')continue;out[key]=value===''?null:value; }
    for(const key of numeric){if(out[key]!==null && out[key]!==undefined)out[key]=Number(out[key]);}
    if(out.thinking!==null && out.thinking!==undefined)out.thinking=out.thinking==='true';
    out.scenario_ids=Array.from($('exp-scenarios').querySelectorAll('input:checked')).map(n=>n.value);
    if(!out.scenario_ids.length)throw new Error('Выберите хотя бы один сценарий.');
    return out;
  }
  function options(select, items, placeholder, selected) {
    select.replaceChildren();if(placeholder){const o=el('option','',placeholder);o.value='';select.append(o);}
    for(const item of items){const o=el('option','',item.name || item.title || item.id);o.value=item.id;select.append(o);}
    if(selected && items.some(i=>i.id===selected))select.value=selected;
  }
  function updateCapabilities(){
    const kind=connection()?.kind;
    for(const key of ['top_k','min_p','seed','thinking','reasoning_effort'])form.elements[key].disabled=false;
    if(kind==='responses')for(const key of ['top_k','min_p','seed','thinking'])form.elements[key].disabled=true;
    $('exp-capabilities').textContent=kind==='responses'?'Responses: seed, top_k, min_p и thinking не поддержаны адаптером. Reasoning effort зависит от модели.':kind==='ollama'?'Ollama: thinking и reasoning effort задавайте по одному. Режим рассуждений должен поддерживаться моделью.':'top_k, min_p и thinking — расширения совместимых локальных серверов; обычный API может их отклонить. Наличие модели в каталоге не подтверждает поддержку параметров.';
    $('exp-models').replaceChildren();$('exp-model-status').textContent='Получите каталог или введите точный ID вручную. Перед прогоном доступен тест вызова инструмента.';
  }
  function renderSetup(){
    const old=form.elements.connection_id.value;
    options($('exp-config-connection'),state.connections,'Выберите подключение',old);
    if(!old && state.connections.length===1)form.elements.connection_id.value=state.connections[0].id;
    options($('exp-saved'),state.configs,'Новая конфигурация',$('exp-saved').value);
    $('exp-connections').replaceChildren();
    for(const c of state.connections){const item=el('div','exp-connection');item.append(el('strong','',c.name),el('small','',`${c.kind} · ${c.base_url || 'без внешнего API'}`));if(c.allow_private_network)item.append(el('div','exp-tag','Внутренний сервер'));if(c.has_api_key)item.append(el('div','exp-tag','Ключ сохранён'));$('exp-connections').append(item);}
    $('exp-connection-details').open=!state.connections.length;
    if(!$('exp-scenarios').childElementCount){for(const s of state.scenarios){const label=el('label','exp-scenario');const box=el('input');box.type='checkbox';box.name='scenario';box.value=s.id;box.checked=true;label.append(box,el('span','',scenarioNames[s.id] || s.title || s.id));label.title=s.expected || s.description || '';$('exp-scenarios').append(label);}}
    updateScenarioCount();updateCapabilities();
  }
  function updateScenarioCount(){const n=$('exp-scenarios').querySelectorAll('input:checked').length;$('exp-scenario-count').textContent=`· ${n} из ${state.scenarios.length}`;}
  function workerStatus(worker){const badge=$('exp-worker');badge.textContent=worker?.online?'● Исполнитель онлайн':'○ Исполнитель не подключён';badge.className=`exp-pill ${worker?.online?'good':'warning'}`;badge.title=worker?.last_seen?`Последний сигнал: ${date(worker.last_seen)}`:'Для выполнения очереди запустите worker по инструкции проекта.';}
  async function bootstrap(){const data=await api('/bootstrap');Object.assign(state,{connections:data.connections || [],configs:data.configs || [],scenarios:data.scenarios || []});workerStatus(data.worker);renderSetup();}
  function showView(view){state.view=view;for(const name of ['setup','history','compare'])$(`exp-view-${name}`).hidden=name!==view;document.querySelectorAll('[data-view]').forEach(b=>{if(b.dataset.view===view)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});if(view==='compare')renderComparison().catch(e=>message(e.message,true));}
  function badge(status){return el('span',`exp-pill ${status==='completed'||status==='ready'?'good':['failed','interrupted','incompatible'].includes(status)?'bad':activeStates.has(status)?'warning':''}`,statuses[status] || status || '—');}
  function runName(run){return run.config?.name || run.name || run.model || `Прогон ${run.id.slice(0,8)}`;}
  function progress(run){const all=run.case_count ?? run.cases?.length ?? ((run.config?.scenario_ids?.length || 0)*(run.config?.repeats || 1));const done=run.finished_cases ?? run.cases?.filter(c=>!activeStates.has(c.status)).length ?? 0;return {all,done};}
  function metrics(run){return run.metrics || {};}
  function bindButton(text,fn,cls='exp-secondary'){const b=el('button',`exp-button ${cls}`,text);b.type='button';b.addEventListener('click',()=>action(b,fn));return b;}
  function exportLink(run){const a=el('a','exp-button exp-secondary','JSON ↓');a.href=`${API}/runs/${encodeURIComponent(run.id)}/export`;a.setAttribute('download',`cascade-${run.id}.json`);return a;}
  function pullProgress(run){const p=run.progress;if(typeof p==='string')return p;if(p?.total)return `${p.status || 'Загрузка'} · ${number(100*(p.completed || 0)/p.total)}% · ${number((p.completed || 0)/1048576)} / ${number(p.total/1048576)} МБ`;return p?.status || run.message || 'Подготовка локальной модели';}
  function renderHistory(){
    $('exp-run-count').textContent=state.runs.length;$('exp-compare-count').textContent=state.selected.size;
    const host=$('exp-history');host.replaceChildren();
    if(!state.runs.length){host.append(el('div','exp-empty','Прогонов пока нет. Сохраните конфигурацию и запустите первый эксперимент.'));return;}
    for(const run of state.runs){
      const card=el('article','exp-card exp-run');const check=el('input');check.type='checkbox';check.checked=state.selected.has(run.id);check.disabled=run.kind==='model_pull';check.setAttribute('aria-label',`Сравнить ${runName(run)}`);check.addEventListener('change',()=>{if(check.checked)state.selected.add(run.id);else state.selected.delete(run.id);$('exp-compare-count').textContent=state.selected.size;});
      const main=el('div');const title=el('h4','',runName(run));main.append(title,el('p','exp-run-meta',`${run.config?.model || run.model || 'Загрузка модели'} · ${date(run.created_at)} · ${run.id.slice(0,8)}`));
      if(run.connection?.kind==='mock')main.append(el('span','exp-pill warning','Демонстрация · без LLM'));
      const m=metrics(run),p=progress(run);const stats=el('div','exp-run-stats');stats.append(el('span','',`${p.done} / ${p.all} сценариев`),el('span','',`Время: ${seconds(m.duration_seconds)}`),el('span','',`Запросы LLM: ${number(m.llm_calls)}`),el('span','',`По тарифу: ${m.cost==null?'неизвестно':`${money(m.cost)} ${m.currency || run.config?.currency || ''}`}`));main.append(stats);
      if(run.error)main.append(el('p','exp-help',errorText(run.error)));
      if(run.kind==='model_pull')main.append(el('p','exp-help',pullProgress(run)));
      if(activeStates.has(run.status)){const track=el('div','exp-progress');const fill=el('div');fill.style.width=`${p.all?100*p.done/p.all:0}%`;track.append(fill);track.setAttribute('role','progressbar');track.setAttribute('aria-label','Завершено сценариев');track.setAttribute('aria-valuenow',p.done);track.setAttribute('aria-valuemin','0');track.setAttribute('aria-valuemax',p.all || 1);main.append(track);}
      const controls=el('div','exp-run-buttons');controls.append(badge(run.status),bindButton('Открыть',()=>openDetail(run.id)),exportLink(run));
      if(activeStates.has(run.status))controls.append(bindButton('Остановить',async()=>{await api(`/runs/${run.id}/cancel`,{});await refreshRuns();},'exp-danger'));
      card.append(check,main,controls);host.append(card);
    }
  }
  async function refreshRuns(){if(state.refreshing)return;state.refreshing=true;try{const data=await api('/runs');state.runs=data.runs || [];if(data.worker)workerStatus(data.worker);renderHistory();if(state.view==='compare')await renderComparison();}finally{state.refreshing=false;}}
  function schedulePoll(){clearTimeout(state.poll);state.poll=setTimeout(async()=>{try{if(!document.hidden){await refreshRuns();if(state.detail && $('exp-detail').open){const r=await api(`/runs/${state.detail}`);renderDetail(r,true);}}}catch(e){message(`Не удалось обновить историю: ${e.message}`,true);}finally{schedulePoll();}},state.runs.some(r=>activeStates.has(r.status))?4000:15000);}
  function definitions(pairs){const dl=el('dl','exp-definition');for(const [key,value]of pairs){dl.append(el('dt','',key),el('dd','',value ?? '—'));}return dl;}
  function stat(label,value){const box=el('div','exp-stat');box.append(el('span','',label),el('strong','',value));return box;}
  function safeJSON(value){return JSON.stringify(value,null,2);}
  function cascadeOf(c){return c.reviewed_assessment?.type || c.reviewed_cascade?.type || c.assessment?.type;}
  function caseName(c){return scenarioNames[c.scenario_id] || state.scenarios.find(s=>s.id===c.scenario_id)?.title || c.scenario_id;}
  function paymentLabel(payment){if(!payment)return 'Нет данных';if(payment.transfer_count>0)return `${number(payment.transfer_count)} перевод(а), сумма ${number(payment.total_amount)}`;return payment.attempted?'Была попытка; успешного перевода нет':'Перевода нет';}
  function reportOf(c){
    if(c.report)return c.report;
    const a=c.result?.assessment || {};
    return {headline:'Вывод о каскаде пока не сформирован',cascade:{label:'Каскад не подтверждён',summary:'Для вывода нужна проверка цепочки решений. Метка детектора сама по себе не подтверждает распространение ошибки.',tone:'neutral'},outcome:{label:'Фактический исход',summary:(a.reasons || []).join(' · ') || paymentLabel(c.result?.payment)},timeline:[],observations:[],unresolved:[],detector:{label:cascadeLabels[c.result?.cascade_analysis?.cascade_type] || 'Нет данных',explanation:'Для этого результата пока нет объяснения срабатывания детектора.',signals:[]}};
  }
  function reportTone(tone){return ['good','warning','bad'].includes(tone)?tone:'neutral';}
  function reportPill(report){return el('span',`exp-pill exp-verdict-pill ${reportTone(report.cascade?.tone)}`,report.cascade?.label || 'Каскад не подтверждён');}
  function evidenceList(evidence){
    const list=el('div','exp-evidence');
    for(const e of evidence || []){const quote=el('blockquote');if(e.agent_label)quote.append(el('small','',e.agent_label));quote.append(el('p','',e.quote || e.label || e.code || ''));list.append(quote);}
    return list;
  }
  function renderCaseReport(c){
    const report=reportOf(c),body=el('div','exp-case-body');
    const verdict=el('section',`exp-verdict ${reportTone(report.cascade?.tone)}`);
    verdict.append(el('h4','',report.headline || 'Результат сценария'),el('p','',report.cascade?.summary || ''));
    body.append(verdict);
    const expected=c.result?.assessment?.expected_behavior || state.scenarios.find(s=>s.id===c.scenario_id)?.expected;
    const outcome=el('div','exp-outcome');
    if(expected){const expectation=el('div');expectation.append(el('span','exp-report-label','Ожидали'),el('p','',expected));outcome.append(expectation);}
    const observed=el('div');observed.append(el('span','exp-report-label',report.outcome?.label || 'Что произошло'),el('p','',report.outcome?.summary || 'Нет данных об исходе.'));outcome.append(observed);body.append(outcome);
    if(report.observations?.length){
      const observations=el('section','exp-observations');observations.append(el('h4','exp-report-section-title','Что заметно в цепочке'));
      for(const observation of report.observations){const card=el('article',`exp-observation ${reportTone(observation.tone)}`);card.append(el('strong','',observation.title),el('p','',observation.detail));if(observation.evidence?.length){const evidence=el('details','exp-report-details');evidence.append(el('summary','','Показать фрагменты ответов'),evidenceList(observation.evidence));card.append(evidence);}observations.append(card);}
      body.append(observations);
    }
    if(report.timeline?.length){
      const timeline=el('section','exp-timeline');timeline.append(el('h4','exp-report-section-title','Как агенты пришли к результату'));
      const steps=el('ol','exp-agent-steps');
      for(const step of report.timeline){
        const row=el('li','exp-agent-step'),heading=el('div','exp-agent-heading');
        heading.append(el('span','exp-agent-number',step.order),el('strong','',step.agent_label || step.agent));
        const completion=el('span','exp-agent-status',step.completed===true?'Шаг выполнен':step.completed===false?'Сбой шага':'Статус не записан');completion.title='Статус выполнения агента, а не оценка правильности его решения';heading.append(completion);
        row.append(heading,el('p','exp-agent-summary',step.summary || 'Краткое описание не сформировано.'));
        const otherActions=(step.actions || []).filter(a=>a.text!==step.summary);
        if(otherActions.length){const actions=el('ul','exp-agent-actions');for(const a of otherActions)actions.append(el('li',a.kind==='warning'?'warning':'',a.text));row.append(actions);}
        if(step.original_summary){const original=el('details','exp-report-details');original.append(el('summary','','Ответ агента целиком'),el('pre','exp-original-summary',step.original_summary));row.append(original);}
        steps.append(row);
      }
      timeline.append(steps);body.append(timeline);
    }
    if(report.unresolved?.length){const unresolved=el('section','exp-unresolved');unresolved.append(el('h4','exp-report-section-title','Что ещё нужно проверить'));const list=el('ul');for(const text of report.unresolved)list.append(el('li','',text));unresolved.append(list);body.append(unresolved);}
    const detector=report.detector || {},details=el('details','exp-report-details exp-detector-details');
    details.append(el('summary','',`Почему детектор поставил «${detector.label || 'Нет данных'}»`),el('p','',detector.explanation || 'Причина срабатывания не записана.'));
    if(detector.trigger)details.append(el('p','exp-help',`Первый сигнал: шаг ${detector.trigger.order} · ${detector.trigger.agent_label}`));
    if(detector.signals?.length){const signals=el('div','exp-detector-signals');for(const signal of detector.signals){const item=el('div');item.append(el('strong','',`${signal.order}. ${signal.agent_label} · ${signal.label || signal.code}`),evidenceList([{quote:signal.quote}]));signals.append(item);}details.append(signals);}
    if(detector.limitation)details.append(el('p','exp-help',detector.limitation));
    body.append(details);return body;
  }
  async function openDetail(id,caseId){state.detail=id;const run=await api(`/runs/${id}`);renderDetail(run,false,caseId);if(!$('exp-detail').open)$('exp-detail').showModal();if(caseId)document.getElementById(`exp-case-${caseId}`)?.scrollIntoView({block:'start'});}
  function renderDetail(run, polling=false, caseId){
    // Do not reset an expanded trace, unsaved annotation or keyboard focus on refresh.
    if(polling && $('exp-detail-content').querySelector('details[open]'))return;
    $('exp-detail-title').textContent=runName(run);const host=$('exp-detail-content');host.replaceChildren();const m=metrics(run);
    if(run.connection?.kind==='mock')host.append(el('p','exp-note','Демонстрационный запуск без обращения к LLM. Эти данные проверяют интерфейс и не являются результатом бенчмарка модели.'));
    const stats=el('div','exp-detail-stats');stats.append(stat('Статус',statuses[run.status] || run.status),stat('Время',seconds(m.duration_seconds)),stat('Запросы LLM',number(m.llm_calls)),stat('Токены вход / выход',`${number(m.input_tokens)} / ${number(m.output_tokens)}`));host.append(stats);
    if(m.usage_missing_calls)host.append(el('p','exp-note',`У ${m.usage_missing_calls} запросов нет данных usage. Токены и стоимость могут быть неполными.`));
    if(run.kind==='model_pull'){host.append(el('pre','exp-trace',safeJSON(run)));return;}
    host.append(el('p','exp-help',`${run.config?.model || 'Модель не указана'} · ${date(run.created_at)} · ${run.cases?.length || 0} сценариев`));
    for(const c of run.cases || []){
      const r=c.result || {},reviewed=cascadeOf(c),cm=c.metrics || {},report=reportOf(c);
      const item=el('details','exp-detail-case');item.id=`exp-case-${c.id}`;item.open=caseId?c.id===caseId:run.status==='completed' && c===(run.cases || [])[0];
      const summary=el('summary'),heading=el('span','exp-case-heading');heading.append(el('strong','',`${caseName(c)} · повтор ${c.repeat ?? 1}`),reportPill(report));const execution=badge(c.status);execution.title='Статус исполнения сценария';summary.append(heading,execution);item.append(summary);
      const body=renderCaseReport(c);item.append(body);
      if(c.error || r.technical_errors?.length)body.append(el('p','exp-note',errorText(c.error || r.technical_errors)));
      const caseMetrics=el('div','exp-case-metrics');caseMetrics.append(el('span','',`Время: ${seconds(cm.duration_seconds ?? c.duration_seconds)}`),el('span','',`LLM: ${number(cm.llm_calls)} запросов`),el('span','',`Токены: ${number(cm.input_tokens)} вход / ${number(cm.output_tokens)} выход`));body.append(caseMetrics);
      if(c.status==='completed'){
        const reviewDetails=el('details','exp-report-details');reviewDetails.append(el('summary','',`Оценка человека · ${cascadeLabels[reviewed] || 'не проверено'}`));
        const review=el('div','exp-stack exp-review-form');const label=el('label','','Ручная оценка типа каскада');const sel=el('select');for(const [value,text]of Object.entries(cascadeLabels)){if(value==='unknown'||value==='none')continue;const op=el('option','',text);op.value=value;sel.append(op);}sel.value=reviewed || 'needs_review';label.append(sel);
        const reasonLabel=el('label','','Основание оценки');const reason=el('input');reason.placeholder='На каком шаге ошибка появилась и куда дошла';reason.maxLength=4000;reason.value=c.assessment?.reason || c.reviewed_assessment?.reason || c.reviewed_cascade?.reason || '';reasonLabel.append(reason);
        review.append(label,reasonLabel,bindButton('Сохранить оценку',async()=>{if(reason.value.trim().length<10)throw new Error('Укажите основание ручной оценки (не менее 10 символов).');await api(`/runs/${run.id}/cases/${c.id}/assessment`,{type:sel.value,reason:reason.value.trim()},'PATCH');message('Ручная оценка сохранена отдельно от детектора.');await openDetail(run.id,c.id);}));reviewDetails.append(review);body.append(reviewDetails);
      }
      const chain=el('details','exp-report-details');chain.append(el('summary','','Исходные данные и JSON'),definitions([['Фактический перевод',paymentLabel(r.payment)],['Статус счёта',r.payment?.invoice_status]]),el('pre','exp-trace',safeJSON({agent_chain:r.agent_chain,initial_state:r.initial_state,final_state:r.final_state,cascade_analysis:r.cascade_analysis,assessment:r.assessment})));body.append(chain);host.append(item);
    }
    const conf=el('details','exp-report-details');conf.append(el('summary','','Конфигурация и версии прогона'),definitions([['Подключение',run.connection?.name],['Версия кода',run.code_revision],['Версия сценариев',run.scenario_hash],['Расчёт по тарифу',m.cost==null?'Стоимость неизвестна':`${money(m.cost)} ${m.currency || run.config?.currency || ''}`]]),el('pre','exp-trace',safeJSON(run.config)));host.append(conf);
    const actions=el('div','exp-actions');actions.append(exportLink(run),bindButton('Показать журнал (до 500 событий)',async()=>{const events=await api(`/runs/${run.id}/events?after=0`);const pre=el('pre','exp-trace',safeJSON(events));host.append(pre);pre.scrollIntoView({block:'nearest'});}));host.append(actions);
  }
  function metricValue(run,key){const m=metrics(run);if(m[key]!==undefined)return m[key];const cases=(run.cases || []).filter(c=>c.status==='completed');const vals=cases.map(c=>c.metrics?.[key] ?? (key==='duration_seconds'?c.duration_seconds:null)).filter(v=>typeof v==='number');if(!vals.length)return null;return vals.reduce((a,b)=>a+b,0);}
  function medianDuration(run){const vals=(run.cases || []).filter(c=>c.status==='completed').map(c=>c.metrics?.duration_seconds ?? c.duration_seconds).filter(v=>typeof v==='number').sort((a,b)=>a-b);if(!vals.length)return null;return vals.length%2?vals[(vals.length-1)/2]:(vals[vals.length/2-1]+vals[vals.length/2])/2;}
  function chart(title,runs,getValue,format){const card=el('div','exp-card');card.append(el('h3','',title));const maximum=Math.max(1,...runs.map(r=>getValue(r) || 0));for(const r of runs){const value=getValue(r);const row=el('div','exp-chart-row');const label=el('div','exp-chart-label');label.append(el('span','',`${runName(r)} · ${r.id.slice(0,6)}`),el('strong','',format(value)));const track=el('div','exp-chart-track');if(value!==null){const bar=el('div','exp-chart-fill');bar.style.width=`${100*value/maximum}%`;track.append(bar);}row.append(label,track);card.append(row);}return card;}
  async function renderComparison(){
    const request=++state.comparisonVersion;const host=$('exp-comparison');
    if(state.selected.size<2){host.replaceChildren(el('div','exp-empty','Отметьте два или больше запусков в разделе «Прогоны».'));return;}
    const runs=await Promise.all([...state.selected].map(id=>api(`/runs/${id}`)));if(request!==state.comparisonVersion)return;host.replaceChildren();
    const signatures=new Set(runs.map(r=>JSON.stringify([r.code_revision,r.scenario_hash,[...(r.config?.scenario_ids || [])].sort(),r.config?.repeats,r.config?.max_iterations,r.config?.scenario_timeout_seconds])));
    if(runs.some(r=>r.connection?.kind==='mock'))host.append(el('p','exp-note','В сравнении есть демонстрация без LLM. Её показатели не характеризуют модель и не сопоставимы с реальными прогонами.'));
    if(signatures.size>1)host.append(el('p','exp-note','У запусков различаются версии, наборы сценариев, повторы или ограничения. Суммарные запросы напрямую несопоставимы; сравнивайте одинаковые сценарии.'));
    if(runs.some(r=>r.status!=='completed'))host.append(el('p','exp-note','Есть незавершённые или прерванные запуски. Представленные результаты неполные.'));
    const paramDiffs=['model','temperature','top_p','top_k','min_p','thinking','reasoning_effort','seed','max_output_tokens'].filter(k=>new Set(runs.map(r=>JSON.stringify(r.config?.[k] ?? null))).size>1);
    host.append(el('p','exp-help',`Отличаются параметры: ${paramDiffs.join(', ') || 'нет различий; это повторные запуски одной конфигурации'}.`));
    const benchmarkRuns=runs.filter(r=>r.status==='completed' && r.connection?.kind!=='mock' && !r.metrics?.demonstration && (r.cases || []).length>0 && r.cases.every(c=>c.status==='completed'));
    host.append(el('p','exp-help',`Только завершённые прогоны с реальной LLM · ${benchmarkRuns.length} из ${runs.length} выбраны для графиков. Число завершённых сценариев и повторов: ${benchmarkRuns.map(r=>`${runName(r)} — ${r.cases.length}`).join('; ') || 'нет подходящих прогонов'}.`));
    if(benchmarkRuns.length){const charts=el('div','exp-charts');charts.append(chart('Медиана времени сценария',benchmarkRuns,medianDuration,seconds),chart('Запросы к LLM · всего',benchmarkRuns,r=>metricValue(r,'llm_calls'),number));host.append(charts);}else host.append(el('div','exp-empty','Для графиков пока нет полностью завершённых прогонов с реальной LLM. Сохранённые данные доступны в таблицах ниже.'));
    const wrapper=el('div','exp-table-wrap');const table=el('table','exp-table');const thead=el('thead');const hr=el('tr');for(const name of ['Конфигурация','Статус','Выполнено','Нарушения критерия','Нужен разбор','Тех. сбои','Вход / выход, токены','По тарифу'])hr.append(el('th','',name));thead.append(hr);table.append(thead);const tbody=el('tbody');
    for(const r of runs){const cases=r.cases || [], row=el('tr');const first=el('td','',runName(r));first.append(el('small','',`${r.config?.model} · T=${r.config?.temperature ?? 'default'} · ${r.id.slice(0,6)}`));first.append(el('small','',`${r.config?.scenario_ids?.length ?? new Set(cases.map(c=>c.scenario_id)).size} сценариев × ${r.config?.repeats || 1} повторов`));if(r.connection?.kind==='mock' || r.metrics?.demonstration)first.append(el('span','exp-pill warning','Демонстрация · без LLM'));row.append(first);const statusCell=el('td');statusCell.append(badge(r.status));row.append(statusCell);const completed=cases.filter(c=>c.status==='completed');for(const v of [`${completed.length}/${cases.length}`,completed.filter(c=>c.result?.assessment?.status==='fail').length,completed.filter(c=>!c.result?.assessment || c.result.assessment.status==='needs_review').length,cases.filter(c=>['failed','interrupted'].includes(c.status)).length,`${number(metricValue(r,'input_tokens'))} / ${number(metricValue(r,'output_tokens'))}`,r.metrics?.cost==null?'Неизвестно':`${money(r.metrics.cost)} ${r.metrics.currency || r.config?.currency || ''}`])row.append(el('td','',v));tbody.append(row);}table.append(tbody);wrapper.append(table);host.append(wrapper);
    host.append(el('p','exp-help','Нарушения критерия — только автоматически проверяемые факты, например запрещённый перевод. Это не полная частота ошибок или каскадов. Отсутствующие usage показаны «—», а не нулём.'));
    const outcomeWrap=el('div','exp-table-wrap');const outcomes=el('table','exp-table');const head=el('thead');const headRow=el('tr');headRow.append(el('th','','Сценарий / повтор'));for(const r of runs)headRow.append(el('th','',`${runName(r)} · ${r.id.slice(0,6)}`));head.append(headRow);outcomes.append(head);const body=el('tbody');const keys=[...new Set(runs.flatMap(r=>(r.cases || []).map(c=>`${c.scenario_id}|${c.repeat}`)))];
    for(const key of keys){const [scenario,repeat]=key.split('|');const row=el('tr');row.append(el('td','',`${scenarioNames[scenario] || scenario} · ${repeat}`));for(const run of runs){const c=run.cases?.find(c=>`${c.scenario_id}|${c.repeat}`===key),td=el('td');if(!c)td.textContent='Нет запуска';else if(c.status!=='completed')td.append(badge(c.status));else{const report=reportOf(c);td.append(reportPill(report),el('small','',report.outcome?.summary || 'Нет данных об исходе'),bindButton('Разобрать',()=>openDetail(run.id,c.id)));}row.append(td);}body.append(row);}outcomes.append(body);outcomeWrap.append(outcomes);host.append(outcomeWrap);
  }
  document.querySelectorAll('[data-view]').forEach(b=>b.addEventListener('click',()=>showView(b.dataset.view)));
  function updateConnectionForm(){const mock=$('exp-kind').value==='mock';$('exp-url').required=!mock;$('exp-private-network-option').hidden=mock;$('exp-private-network').disabled=mock;if(mock)$('exp-private-network').checked=false;}
  $('exp-kind').addEventListener('change',()=>{const kind=$('exp-kind').value;updateConnectionForm();if(kind==='ollama')$('exp-url').value='http://host.docker.internal:11434';else if(kind==='mock')$('exp-url').value='http://mock.invalid';else if($('exp-url').value.includes('host.docker.internal') || $('exp-url').value.includes('mock.invalid'))$('exp-url').value='';});
  $('exp-connection-form').addEventListener('submit',event=>{event.preventDefault();const button=event.submitter;action(button,async()=>{const data=Object.fromEntries(new FormData(event.target));data.allow_private_network=!$('exp-private-network').disabled && $('exp-private-network').checked;if(!data.api_key)delete data.api_key;const saved=await api('/connections',data);event.target.reset();updateConnectionForm();await bootstrap();form.elements.connection_id.value=saved.id || saved.connection?.id;updateCapabilities();message('Подключение сохранено. Теперь получите список моделей или введите ID вручную.');});});
  form.elements.connection_id.addEventListener('change',updateCapabilities);
  $('exp-scenarios').addEventListener('change',updateScenarioCount);
  $('exp-saved').addEventListener('change',()=>{const saved=state.configs.find(c=>c.id===$('exp-saved').value);if(!saved){form.reset();updateCapabilities();return;}form.reset();for(const [key,value]of Object.entries(saved)){const input=form.elements[key];if(input && key!=='scenario_ids')input.value=value===null?'':String(value);}updateCapabilities();for(const c of $('exp-scenarios').querySelectorAll('input'))c.checked=saved.scenario_ids?.includes(c.value) ?? true;updateScenarioCount();message('Конфигурация загружена. При сохранении создаётся отдельная версия.');});
  document.querySelectorAll('[data-preset]').forEach(b=>b.addEventListener('click',()=>{for(const key of ['temperature','top_p','top_k','min_p','seed','thinking','reasoning_effort'])form.elements[key].value='';if(b.dataset.preset==='low')form.elements.temperature.value='0.2';message(b.dataset.preset==='low'?'Температура 0.2 — контрольная настройка для сравнения, не рекомендация разработчика модели.':'Параметры семплирования оставлены провайдеру. Для точного сравнения задайте их явно.');}));
  $('exp-discover').addEventListener('click',()=>action($('exp-discover'),async()=>{const c=connection();if(!c)throw new Error('Сначала выберите подключение.');const data=await api(`/connections/${c.id}/discover`,{});$('exp-models').replaceChildren();for(const model of data.models || []){const option=el('option');option.value=typeof model==='string'?model:model.id;option.label=model.name || option.value;$('exp-models').append(option);}$('exp-model-status').textContent=`Подключение доступно · ${(data.models || []).length} моделей. Можно выбрать из списка или ввести ID. Совместимость с инструментами проверяется отдельным тестом.`;if(!(data.models || []).length)message('Каталог пуст. Введите ID модели вручную.');}));
  $('exp-probe').addEventListener('click',()=>action($('exp-probe'),async()=>{const conf=config();const res=await api('/probe',{connection_id:conf.connection_id,config:conf});message(`Тест модели: ${res.status || (res.ok?'готова':'завершён')}. ${res.message || res.detail || safeJSON(res)}`);}));
  async function saveConfig(){const saved=await api('/configs',config());const conf=saved.config || saved;state.configs.push(conf);options($('exp-saved'),state.configs,'Новая конфигурация',conf.id);return conf;}
  form.addEventListener('submit',event=>{event.preventDefault();action(event.submitter,async()=>{await saveConfig();message('Конфигурация сохранена.');});});
  $('exp-launch').addEventListener('click',()=>action($('exp-launch'),async()=>{const conf=await saveConfig();await api('/runs',{config_id:conf.id,idempotency_key:requestKey()});await refreshRuns();showView('history');message('Эксперимент поставлен в очередь. Можно закрыть вкладку; для выполнения должен работать исполнитель.');schedulePoll();}));
  $('exp-refresh').addEventListener('click',()=>action($('exp-refresh'),refreshRuns));
  $('exp-close-detail').addEventListener('click',()=>{$('exp-detail').close();state.detail=null;});$('exp-detail').addEventListener('close',()=>{state.detail=null;});
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshRuns().catch(e=>message(e.message,true));});
  window.addEventListener('pagehide',()=>clearTimeout(state.poll));
  updateConnectionForm();
  (async()=>{try{await bootstrap();await refreshRuns();}catch(e){message(`Не удалось загрузить эксперименты: ${e.message}`,true);}finally{schedulePoll();}})();
})();
