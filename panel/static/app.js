'use strict';
const $=id=>document.getElementById(id);
let state=null,dirty=false,revision=null,exclusions=null,exclusionRevision=null,busy=false,polling=false,toastTimer,lastContainers='',lastLogs='',active='containers';
const boolKeys=['cleanup','monitor_only','rolling_restart','label_enable','include_stopped','revive_stopped','include_restarting','http_api_update','http_api_periodic_polls'];
const jobLabels={idle:'Nenhuma',running:'Em andamento',done:'Concluída',error:'Verificar falha',interrupted:'Interrompida'};
function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e;}
function toast(text,error=false){$('toast').textContent=text;$('toast').classList.toggle('error',error);$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,error?8000:5000);}
async function api(path,body){const opts={cache:'no-store'};if(body!==undefined){opts.method='POST';opts.headers={'Content-Type':'application/json','X-Sal0-Request':'1'};opts.body=JSON.stringify(body);}const response=await fetch(path.replace(/^\//,''),opts);let data;try{data=await response.json();}catch{throw new Error('Resposta inválida do servidor.');}if(!response.ok)throw new Error(data.error||'Não foi possível concluir.');return data;}
function duration(n){n=Math.max(0,Math.floor(n));return [Math.floor(n/3600),Math.floor(n%3600/60),n%60].map(x=>String(x).padStart(2,'0')).join(':');}
function setTab(name){active=name;for(const b of document.querySelectorAll('[data-tab]')){const on=b.dataset.tab===name;b.classList.toggle('active',on);b.setAttribute('aria-selected',String(on));b.tabIndex=on?0:-1;$('view-'+b.dataset.tab).hidden=!on;}}
function markDirty(){dirty=true;$('save-state').textContent='Alterações não salvas';$('save-state').classList.add('dirty');$('save-settings').disabled=false;}
function modes(){const cron=$('schedule-mode').value==='cron';$('cron-label').hidden=!cron;$('interval-label').hidden=cron;}
function fillSettings(s){const c=s.settings;for(const k of boolKeys)$(k).checked=!!c[k];$('interval').value=c.interval||86400;$('schedule').value=c.schedule||'';$('schedule-mode').value=c.schedule?'cron':'interval';modes();$('native-filters').textContent='Escopo: '+(c.scope||'todos')+'. Nomes: '+(c.names?.length?c.names.join(', '):'todos os permitidos')+(c.unknown_flags?'. Há flags nativas adicionais.':'')+(c.no_pull?' Downloads estão desativados.':'')+(c.no_restart?' Reinícios após atualizar estão desativados.':'');$('notifications').textContent=c.notification_configured?'Notificações existentes configuradas e preservadas.':'Nenhum destino de notificação detectado.';dirty=false;revision=s.revision;$('save-state').textContent='Tudo salvo';$('save-state').classList.remove('dirty');$('save-settings').disabled=true;}
function confirm(title,message,label,callback,host=false){$('confirm-title').textContent=title;$('confirm-message').textContent=message;$('confirm-action').textContent=label;$('confirm-error').hidden=true;$('host-confirm-label').hidden=!host;$('host-confirm').checked=false;$('confirm-action').onclick=async()=>{if(busy)return;if(host&&!$('host-confirm').checked){$('confirm-error').textContent='Confirme a ativação da API nesta rede antes de continuar.';$('confirm-error').hidden=false;return;}busy=true;$('confirm-action').disabled=true;try{await callback();$('confirm-dialog').close();}catch(e){$('confirm-error').textContent=e.message;$('confirm-error').hidden=false;}finally{busy=false;$('confirm-action').disabled=false;}};$('confirm-dialog').showModal();}
function applySettings(patch,rev){const host=state.settings.host_network&&!state.settings.http_api_update&&patch.http_api_update;confirm('Aplicar configuração?','O Watchtower será recriado para aplicar essas opções. As configurações de notificação e os volumes serão preservados. Os outros containers continuarão em execução.','Aplicar',async()=>{if(host)patch.confirm_host_api=true;accept(await api('/api/settings',{settings:patch,revision:rev}));dirty=false;exclusions=null;lastContainers='';toast('Aplicação iniciada. Acompanhe a operação em Atividade.');setTab('activity');},host);}
function activate(){if(!state||state.error)return;applySettings({http_api_update:true},state.revision);}
function update(images=[],appTitle=''){if(!state||state.error)return;if(!state.settings.http_api_update){activate();return;}const scope=images.length?((appTitle?'as imagens de '+appTitle:'as imagens selecionadas')+', incluindo outros containers que usam essas imagens'):'todos os containers permitidos pelos filtros atuais';confirm(state.settings.monitor_only?'Verificar novas imagens?':'Verificar e atualizar?',`O Watchtower verificará ${scope}. `+(state.settings.monitor_only?'O modo somente monitorar está ativo.':'Se encontrar novas versões, ele poderá reiniciar os containers atualizados.'),'Iniciar',async()=>{accept(await api('/api/update',{images}));toast('Solicitação enviada ao Watchtower.');});}
const expandedApps=new Set();
try{const saved=JSON.parse(localStorage.getItem('sal0-tower-expanded')||'[]');if(Array.isArray(saved))for(const id of saved)if(typeof id==='string')expandedApps.add(id);}catch{}
function appIcon(url,name,small=false){
  const box=el('span','app-icon'+(small?' small':''));box.setAttribute('aria-hidden','true');
  const initials=(name||'?').split(/[\s_.-]+/).filter(Boolean).slice(0,2).map(x=>x[0]).join('').toUpperCase();
  box.append(el('span','icon-fallback',initials));
  if(url){const img=el('img');img.alt='';img.loading='lazy';img.decoding='async';img.referrerPolicy='no-referrer';img.onload=()=>box.classList.add('loaded');img.onerror=()=>img.remove();img.src=url;box.append(img);}
  return box;
}
function exclusionControl(members,key,caption,s){
  const label=el('label','check'),check=el('input');check.type='checkbox';check.id='exclude-'+key;
  const count=members.filter(c=>exclusions?exclusions.includes(c.name):c.excluded).length;
  check.checked=count===members.length;check.indeterminate=count>0&&count<members.length;
  check.disabled=s.job.state==='running';check.setAttribute('aria-label',caption);
  check.onchange=()=>{
    if(exclusions===null){exclusions=[...s.settings.disabled];exclusionRevision=s.revision;}
    const names=new Set(members.map(c=>c.name));
    exclusions=check.checked?[...new Set([...exclusions,...names])]:exclusions.filter(n=>!names.has(n));
    $('exclusions-bar').hidden=false;$('exclusions-count').textContent='Exclusões alteradas';
    lastContainers='';renderContainers(state);$(check.id)?.focus({preventScroll:true});
  };
  label.append(check,document.createTextNode(members.length>1?'Excluir stack':'Excluir'));return label;
}
function imageButton(members,s,caption='Verificar imagem',title=''){
  const allowed=members.filter(c=>c.eligible!==false);
  const images=[...new Set(allowed.map(c=>c.image.split(':')[0]))];
  const button=el('button','secondary',caption);button.type='button';
  button.disabled=!images.length||s.job.state==='running'||!s.settings.http_api_update||s.status!=='running';
  button.onclick=()=>update(images,title);return button;
}
function containerRow(c,s){
  const row=el('div','container-row');row.dataset.container=c.name;
  const identity=el('div','member-identity'),copy=el('div','member-copy');
  copy.append(el('h4','',c.service||c.display_name||c.name));
  copy.append(el('small','container-id',c.name),el('small','image-name',c.image));
  identity.append(appIcon(c.icon,c.service||c.display_name||c.name,true),copy);
  const badge=el('span','container-status '+(c.state==='running'?'is-running':c.state==='restarting'?'is-warning':'is-stopped'),c.state==='running'?'Ativo':c.state==='restarting'?'Reiniciando':'Parado');
  const header=el('div','member-header');header.append(identity,badge);
  const controls=el('div','tower-actions');controls.append(exclusionControl([c],c.id,'Excluir '+c.name,s),imageButton([c],s));
  row.append(header,el('p','member-reason',c.reason),controls);return row;
}
function renderContainers(s){
  const query=$('search').value.trim().toLocaleLowerCase('pt-BR'),filter=$('app-filter').value;
  const key=JSON.stringify([s.apps,s.containers,query,filter,exclusions,s.job.state,s.status,s.settings.http_api_update]);
  if(key===lastContainers)return;lastContainers=key;
  const wrap=$('containers');wrap.replaceChildren();
  const byId=new Map(s.containers.map(c=>[c.id,c]));let count=0,total=0;
  for(const app of s.apps||[]){
    const members=app.containers.map(id=>byId.get(id)).filter(Boolean);if(!members.length)continue;
    const searchable=[app.title,app.project,...members.flatMap(c=>[c.name,c.display_name,c.service,c.image])].join(' ').toLocaleLowerCase('pt-BR');
    const isExcluded=c=>exclusions?exclusions.includes(c.name):c.excluded;
    if(query&&!searchable.includes(query))continue;
    if(filter==='monitored'&&!members.some(c=>c.eligible===true))continue;
    if(filter==='excluded'&&!members.some(isExcluded))continue;
    if(filter==='stopped'&&members.every(c=>c.state==='running'))continue;
    count++;total+=members.length;
    const running=members.filter(c=>c.state==='running').length,monitored=members.filter(c=>c.eligible===true).length;
    const excluded=members.filter(isExcluded).length,stack=members.length>1;
    const card=el('article','card app-card');card.dataset.appId=app.id;
    const head=el('div','app-card-head'),identity=el('div','app-identity'),copy=el('div','app-copy');
    copy.append(el('h3','app-title',app.title));
    copy.append(el('small','app-subtitle',stack?members.length+' containers na stack':members[0].name));
    identity.append(appIcon(app.icon||members[0].icon,app.title),copy);
    const status=el('span','container-status '+(running===members.length?'is-running':running?'is-warning':'is-stopped'),running===members.length?'Ativo':running?'Parcial':'Parado');
    head.append(identity,status);card.append(head);
    const stats=el('div','app-stats');stats.append(el('span','',running+'/'+members.length+' ativos'),el('span','',monitored+' monitorado'+(monitored===1?'':'s')));
    if(excluded)stats.append(el('span','excluded-stat',excluded+' excluído'+(excluded===1?'':'s')));
    card.append(stats);
    if(!stack){card.append(el('p','single-reason',members[0].reason),el('small','image-name single-image',members[0].image));}
    const controls=el('div','tower-actions app-controls');
    controls.append(exclusionControl(members,app.id,'Excluir '+(stack?'stack ':'aplicativo ')+app.title,s),imageButton(members,s,stack?'Verificar stack':'Verificar imagem',app.title));
    card.append(controls);
    if(stack){
      const details=el('details','stack-details');details.dataset.stack=app.id;
      details.open=!!query||expandedApps.has(app.id);
      const summary=el('summary');summary.append(el('span','', 'Containers da stack'),el('span','stack-count',String(members.length)));
      const body=el('div','stack-members');
      const ordered=[...members].sort((a,b)=>Number(b.id===app.main_container)-Number(a.id===app.main_container)||a.name.localeCompare(b.name,'pt-BR'));
      for(const c of ordered)body.append(containerRow(c,s));
      details.append(summary,body);details.addEventListener('toggle',()=>{
        if(query)return;if(details.open)expandedApps.add(app.id);else expandedApps.delete(app.id);
        try{localStorage.setItem('sal0-tower-expanded',JSON.stringify([...expandedApps]));}catch{}
      });card.append(details);
    }
    wrap.append(card);
  }
  $('empty').hidden=count>0;
  $('apps-count').textContent=count+' aplicativo'+(count===1?'':'s')+' · '+total+' container'+(total===1?'':'s');
  $('catalog-notice').hidden=!!s.error||s.catalog?.available!==false;
}
function renderLogs(s){const filter=$('log-filter').value;const entries=s.logs.filter(x=>filter==='all'||filter==='panel'&&x.source==='painel'||filter==='error'&&/error|warn|fail|erro|falh|limit|denied/i.test(x.message));const key=JSON.stringify([entries,filter]);if(key===lastLogs)return;lastLogs=key;const wrap=$('logs');wrap.replaceChildren();for(const x of [...entries].reverse().slice(0,120)){const row=el('div','activity-item'),date=new Date(x.time*1000),stamp=el('time','',x.time?date.toLocaleTimeString('pt-BR'):'—');if(x.time)stamp.title=date.toLocaleString('pt-BR');const msg=el('div','activity-message',x.message);if(/error|falh|fail|toomanyrequests/i.test(x.message))msg.classList.add('log-error');row.append(stamp,msg);wrap.append(row);}$('empty-logs').hidden=entries.length>0;}
function accept(s){const first=state===null;const oldJob=state?.job;state=s;$('offline').hidden=true;$('connection').textContent='Conectado';$('connection').classList.remove('offline');$('error').textContent=s.error||'';$('error').hidden=!s.error;$('version').textContent=s.version;const running=s.status==='running';for(const id of ['status','service-badge']){$(id).textContent=s.error?'Indisponível':running?'Watchtower ativo':'Watchtower parado';$(id).className='badge '+(running?'running':'');}$('monitored').textContent=s.containers.filter(x=>x.eligible===true).length;$('excluded').textContent=s.containers.filter(x=>x.excluded).length;$('last-job').textContent=jobLabels[s.job.state]||'—';$('job-banner').hidden=s.job.state!=='running';$('job-banner').textContent=s.job.message||'';const c=s.settings;$('schedule-summary').textContent=c.http_api_update&&!c.http_api_periodic_polls?'Somente verificações manuais':c.schedule?'Cron: '+c.schedule:'Intervalo: '+duration(c.interval||86400);const working=s.job.state==='running';$('update-all').textContent=c.http_api_update?'Verificar atualizações':'Ativar controles';$('update-all').disabled=working||!!s.error||!running;$('activity-update').disabled=working||!!s.error||!running;$('toggle-service').textContent=running?'Pausar Watchtower':'Retomar Watchtower';$('toggle-service').disabled=working||!!s.error;$('restart-service').disabled=working||!!s.error;renderContainers(s);renderLogs(s);if(first||(!dirty&&revision!==s.revision&&s.revision))fillSettings(s);if(oldJob?.state==='running'&&s.job.state!=='running'){toast(s.job.message,s.job.state==='error');if(s.job.kind==='settings'||s.job.kind==='restore'){fillSettings(s);exclusions=null;$('exclusions-bar').hidden=true;lastContainers='';renderContainers(s);}}elapsed();}
function elapsed(){if(!state)return;const j=state.job;$('job-duration').textContent=j.started?'Duração: '+duration((j.finished||Date.now()/1000)-j.started):'Pronto para acompanhar';}
async function refresh(){if(polling||busy)return;polling=true;try{accept(await api('/api/state'));}catch(e){$('offline').hidden=false;$('connection').textContent='Sem conexão';$('connection').classList.add('offline');}finally{polling=false;}}
for(const b of document.querySelectorAll('[data-tab]')){b.onclick=()=>setTab(b.dataset.tab);b.onkeydown=e=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();const tabs=[...document.querySelectorAll('[data-tab]')],i=tabs.indexOf(b),next=e.key==='Home'?0:e.key==='End'?2:(i+(e.key==='ArrowRight'?1:2))%3;setTab(tabs[next].dataset.tab);tabs[next].focus();};}
$('search').oninput=()=>{if(state)renderContainers(state);};$('app-filter').onchange=()=>{if(state)renderContainers(state);};$('log-filter').onchange=()=>{if(state)renderLogs(state);};$('update-all').onclick=()=>update();$('activity-update').onclick=()=>update();$('refresh').onclick=refresh;
$('schedule-mode').onchange=modes;$('settings-form').addEventListener('input',markDirty);$('settings-form').addEventListener('change',markDirty);$('settings-form').onsubmit=e=>{e.preventDefault();const patch={interval:Number($('interval').value),schedule:$('schedule-mode').value==='cron'?$('schedule').value:''};for(const k of boolKeys)patch[k]=$(k).checked;if(patch.revive_stopped&&!patch.include_stopped){toast('Para iniciar containers parados, ative também Incluir containers parados.',true);return;}applySettings(patch,revision);};
$('apply-exclusions').onclick=()=>applySettings({disabled:exclusions},exclusionRevision);
$('cancel-exclusions').onclick=()=>{exclusions=null;$('exclusions-bar').hidden=true;lastContainers='';if(state)renderContainers(state);};
function service(action){confirm(action==='stop'?'Pausar o Watchtower?':action==='start'?'Retomar o Watchtower?':'Reiniciar o Watchtower?','Essa ação controla somente o Watchtower. Os containers monitorados continuam funcionando.','Confirmar',async()=>{accept(await api('/api/service',{action}));toast('Comando enviado.');});}
$('toggle-service').onclick=()=>service(state.status==='running'?'stop':'start');$('restart-service').onclick=()=>service('restart');$('restore').onclick=()=>confirm('Restaurar configuração anterior?','O Watchtower será recriado usando as opções salvas antes da última alteração.','Restaurar',async()=>{accept(await api('/api/restore',{revision:state.revision}));toast('Restauração iniciada.');setTab('activity');});
for(const id of ['close-dialog','cancel-dialog'])$(id).onclick=()=>{if(!busy)$('confirm-dialog').close();};$('confirm-dialog').addEventListener('cancel',e=>{if(busy)e.preventDefault();});
try{document.body.classList.toggle('light',localStorage.getItem('sal0-theme')==='light');}catch{}$('theme').onclick=()=>{const on=document.body.classList.toggle('light');try{localStorage.setItem('sal0-theme',on?'light':'dark');}catch{}};
window.addEventListener('beforeunload',e=>{if(dirty||exclusions!==null){e.preventDefault();e.returnValue='';}});document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});refresh();setInterval(()=>{if(!document.hidden)refresh();},5000);setInterval(elapsed,1000);
