import {connectionState,acceptSnapshot,lines} from './state.mjs';
import {renderMarkdown} from './markdown.mjs';
const $=id=>document.getElementById(id);
let selected=null,snapshot=null,stream=null,generation=0,refreshTimer=null,disconnected=false;
let dirtyTask=false,dirtySettings=false,taskBase=null,settingsBase=null,detailSource=null;
const names={profiler:'Profiler',git:'Git',docs:'Documentation'};
const requestId=()=>crypto.randomUUID();
function notice(text){$('notice').textContent=text;$('notice').hidden=!text;}
async function api(path,method='GET',body){
  const response=await fetch('/api'+path,{method,headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined});
  const result=await response.json();if(!response.ok)throw new Error(result.code||'request_failed');return result;
}
async function command(path,body={},method='POST'){
  const owner=selected;
  try{const result=await api(`/conversations/${owner}${path}`,method,{request_id:requestId(),...body});if(owner===selected)await refresh();return result;}
  catch(error){if(owner===selected)notice('Не удалось выполнить: '+error.message);throw error;}
}
async function list(){
  const items=await api('/conversations');$('conversations').replaceChildren();
  for(const item of items){const button=document.createElement('button');button.textContent='◷  '+item.title;button.className=item.id===selected?'active':'';button.onclick=()=>select(item.id);$('conversations').append(button);}
  return items;
}
async function select(id){
  if(selected)$('draft').value && sessionStorage.setItem('draft:'+selected,$('draft').value);
  selected=id;snapshot=null;generation++;stream?.close();clearTimeout(refreshTimer);
  dirtyTask=false;dirtySettings=false;disconnected=false;notice('');$('token').value='';
  $('draft').value=sessionStorage.getItem('draft:'+id)||'';$('sidebar').classList.remove('open');
  await list();await refresh();if(selected===id)connect(id,generation);
}
function connect(id,epoch){
  stream=new EventSource(`/api/conversations/${id}/events?after=${snapshot?.event_id||0}`);
  stream.onopen=()=>{if(epoch!==generation)return;disconnected=false;notice('');refresh();};
  stream.onmessage=()=>{if(epoch===generation){clearTimeout(refreshTimer);refreshTimer=setTimeout(refresh,70);}};
  stream.onerror=()=>{if(epoch!==generation)return;disconnected=true;notice('Соединение потеряно. Переподключаемся; Worker продолжает работу.');};
}
async function refresh(){
  const owner=selected,epoch=generation;if(!owner)return;
  try{const next=await api('/conversations/'+owner);if(epoch!==generation||!acceptSnapshot(snapshot,next,owner,selected))return;snapshot=next;render();}
  catch(error){if(epoch===generation){notice('Не удалось обновить состояние. Повтор через 2 секунды.');clearTimeout(refreshTimer);refreshTimer=setTimeout(refresh,2000);}}
}
function renderConnections(){
  if(!snapshot)return;$('connections').replaceChildren();
  for(const source of Object.keys(names)){
    const checking=snapshot.jobs.some(j=>j.kind==='diagnostics'&&j.sources?.includes(source));
    const status=connectionState(snapshot.checks[source],checking);const button=document.createElement('button');
    button.className='status-chip '+status.kind;button.textContent='● '+names[source];const label=document.createElement('span');label.textContent=status.label;button.append(label);
    button.onclick=()=>showCheck(source);$('connections').append(button);
  }
}
function render(){
  renderConnections();
  const nearBottom=$('messages').scrollHeight-$('messages').scrollTop-$('messages').clientHeight<80;
  $('messages').replaceChildren();
  if(!snapshot.messages.length){
    const welcome=document.createElement('div');welcome.className='welcome';
    for(const [tag,cls,text] of [['div','welcome-icon','⌁'],['h2','','От вопроса — к проверяемому выводу'],['p','','Подготовьте задачу, подключите профиль или исходный код и исследуйте причину замедления. Вы управляете каждым запуском.'],['div','starter','Начните с /task: «Исследуй задержку обработки запросов». Затем укажите Profile ID или Git-репозиторий справа.']]){const el=document.createElement(tag);el.className=cls;el.textContent=text;welcome.append(el);}$('messages').append(welcome);
  }
  for(const message of snapshot.messages){const el=document.createElement('article');el.className='message '+message.role+' '+message.classification;
    const label=document.createElement('div');label.className='message-label';label.textContent=(message.role==='user'?'ВЫ':'▥  ASSISTANT')+' · '+message.classification;
    const content=document.createElement('div');content.className='message-text';renderMarkdown(content,message.text);el.append(label,content);$('messages').append(el);
  }
  for(const run of snapshot.runs){if(run.status==='waiting')for(const q of run.questions.filter(q=>q.answer===null)){const el=document.createElement('div');el.className='question';el.textContent='Требуется ответ\n'+q.payload.prompt+'\nСледующее сообщение будет ответом на этот вопрос.';$('messages').append(el);}}
  if(nearBottom)$('messages').scrollTop=$('messages').scrollHeight;
  const task=snapshot.task;
  if(!dirtyTask){taskBase=task.spec_version;$('summary').value=task.summary;$('task-profile').value=task.profile_id||'';$('task-repo').value=task.repository||'';$('task-ref').value=task.ref;$('source-paths').value=task.options.source_paths.join('\n');$('open-questions').value=task.open_questions.join('\n');$('budget').value=task.options.active_seconds;$('executor').value=task.options.executor;}
  $('task-version').textContent='v'+task.spec_version+(dirtyTask?' · черновик':'');
  const settings=snapshot.settings;
  if(!dirtySettings){settingsBase=settings.revision;$('settings-profile').value=settings.profile_id||'';$('settings-repo').value=settings.repository||'';$('settings-ref').value=settings.ref;}
  const active=snapshot.runs.find(r=>['queued','running','waiting'].includes(r.status));
  $('run').disabled=!!active||!!snapshot.readiness.length||dirtyTask;
  $('cancel').disabled=!active;
  $('change-context').disabled=!active;
  const reasons={summary_required:'Добавьте описание.',target_required:'Укажите профиль или репозиторий.',open_questions:'Ответьте на открытые вопросы.'};
  $('readiness').textContent=dirtyTask?'Сохраните изменения перед запуском.':snapshot.readiness.map(r=>reasons[r]||r).join(' ')||'Задача готова к запуску.';
  $('execution').hidden=!snapshot.runs.length;
  $('execution-text').textContent=snapshot.runs.map(r=>`${r.status} · ${r.checkpoint.phase} · ${r.active_seconds.toFixed(1)} с / ${r.snapshot.options.active_seconds} с${r.failure?' · '+r.failure:''}`).join('\n');
  $('evidence').replaceChildren();
  for(const run of snapshot.runs){
    if(run.report){const button=document.createElement('button');button.textContent='↗ Отчёт · '+run.status;button.onclick=()=>showDetail('Структурированный отчёт',run.report);$('evidence').append(button);}
    for(const id of run.checkpoint.evidence_ids||[]){const button=document.createElement('button');button.textContent='◇ '+id.slice(0,16)+'…';const owner=selected;button.onclick=async()=>{try{const e=await api(`/conversations/${owner}/evidence/${id}`);if(owner===selected)showDetail('Evidence · '+id,e);}catch(error){notice(error.message);}};$('evidence').append(button);}
  }
  if(disconnected)notice('Соединение потеряно. Переподключаемся; Worker продолжает работу.');
}
function showDetail(title,body){detailSource=null;$('detail-title').textContent=title;$('detail-body').textContent=JSON.stringify(body,null,2);$('recheck').hidden=true;$('detail').showModal();}
function showCheck(source){showDetail(names[source],snapshot.checks[source]||{status:'Не проверено',note:'Проверка запускается только явно.'});detailSource=source;$('recheck').hidden=false;}
$('close-detail').onclick=()=>$('detail').close();
$('recheck').onclick=()=>{const source=detailSource;$('detail').close();command('/connection-checks',{sources:[source],expected_settings_revision:snapshot.settings.revision}).catch(()=>{});};
$('new').onclick=async()=>{try{const item=await api('/conversations','POST',{});await select(item.id);}catch(error){notice(error.message);}};
$('menu').onclick=()=>$('sidebar').classList.toggle('open');
$('draft').oninput=()=>sessionStorage.setItem('draft:'+selected,$('draft').value);
$('chat-form').onsubmit=async event=>{event.preventDefault();const text=$('draft').value.trim();if(!text)return;const owner=selected;$('send').disabled=true;try{await command('/messages',{text});if(owner===selected){$('draft').value='';sessionStorage.removeItem('draft:'+owner);}}catch{}finally{$('send').disabled=false;}};
$('draft').onkeydown=e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey))$('chat-form').requestSubmit();};
$('task-form').oninput=()=>{dirtyTask=true;$('run').disabled=true;$('readiness').textContent='Сохраните изменения перед запуском.';};
$('task-form').onsubmit=async event=>{event.preventDefault();const owner=selected;try{await command('/task',{patch:{expected_version:taskBase,summary:$('summary').value,profile_id:$('task-profile').value||null,repository:$('task-repo').value||null,ref:$('task-ref').value||'HEAD',open_questions:lines($('open-questions').value),options:{...snapshot.task.options,source_paths:lines($('source-paths').value),active_seconds:Number($('budget').value),executor:$('executor').value}}},'PATCH');if(owner===selected){dirtyTask=false;render();}}catch{}};
$('settings-form').oninput=()=>{dirtySettings=true;};
$('settings-form').onsubmit=async event=>{event.preventDefault();const owner=selected;const token=$('token').value;$('token').value='';try{await command('/settings',{expected_revision:settingsBase,profile_id:$('settings-profile').value||null,repository:$('settings-repo').value||null,ref:$('settings-ref').value||'HEAD',token:token||null,clear_token:$('clear-token').checked},'PUT');if(owner===selected){dirtySettings=false;$('clear-token').checked=false;render();}}catch{}};
$('run').onclick=()=>command('/runs').catch(()=>{});
$('cancel').onclick=()=>command('/cancel').catch(()=>{});
$('change-context').onclick=()=>{const run=snapshot.runs.find(r=>['queued','running','waiting'].includes(r.status));if(run)command(`/runs/${run.id}/context`,{context:{revision:run.context.revision,profile_id:$('task-profile').value||null,repository:$('task-repo').value||null,ref:$('task-ref').value||'HEAD'}}).catch(()=>{});};
$('check-all').onclick=()=>command('/connection-checks',{expected_settings_revision:snapshot.settings.revision}).catch(()=>{});
$('copy').onclick=()=>navigator.clipboard.writeText($('execution-text').textContent).catch(()=>notice('Копирование недоступно. Выделите текст вручную.'));
setInterval(renderConnections,15000);
try{let items=await list();if(!items.length){items=[await api('/conversations','POST',{})];}await select(items[0].id);}catch(error){notice('Не удалось загрузить приложение: '+error.message);}
