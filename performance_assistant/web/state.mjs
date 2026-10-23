export function connectionState(check, checking=false, now=Date.now()/1000) {
  if(checking) return {kind:'checking', label:'проверка…'};
  if(!check) return {kind:'unknown',label:'не проверено'};
  if(check.stale || check.expires_at<=now) return {kind:'stale',label:'устарело'};
  const stages=Object.values(check.stages);
  if(stages.some(s=>s.outcome==='fail')) return {kind:'fail',label:'ошибка / отказ'};
  const target=check.source==='docs'?check.stages.documentation:check.stages.selected_object;
  if(target?.outcome==='pass') return {kind:'pass',label:'доступ проверен'};
  if(stages.some(s=>s.outcome==='pass')) return {kind:'partial',label:'частично'};
  return {kind:'unknown',label:'неизвестно'};
}
export function acceptSnapshot(current, incoming, owner, selected) {
  return owner===selected && (!current || incoming.event_id>=current.event_id);
}
export function lines(text){return text.split('\n').map(s=>s.trim()).filter(Boolean);}
