const $ = (s, root=document) => root.querySelector(s);
const $$ = (s, root=document) => [...root.querySelectorAll(s)];
const state = { home:'', view:'largest', scope:'', rows:[], page:1, pageSize:100, sort:'size', dir:-1, query:'', token:null, health:null, scanTimer:null, currentPath:'', selected:new Set(), preview:null };
const fmt = (n, dash='—') => { if (n === null || n === undefined || !Number.isFinite(Number(n))) return dash; let v=Math.max(0,Number(n)), u=['B','KB','MB','GB','TB'], i=0; while(v>=1000&&i<u.length-1){v/=1000;i++;} return `${v>=100?v.toFixed(0):v>=10?v.toFixed(1):v.toFixed(2)} ${u[i]}`; };
const date = (v) => v ? new Date(v).toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}) : '—';
const nameOf = p => p?.split('/').filter(Boolean).pop() || p;
const pathLabel = p => state.home && (p === state.home || p?.startsWith(state.home + '/')) ? `~${p.slice(state.home.length)}` : p;
const trashAvailable = () => state.health?.trash?.available === true;
const canManage = r => trashAvailable() && r.can_trash !== false && !!r.path && !r.path.endsWith('/');
const scannedSource = d => d.source === 'filesystem-scan' || d.source?.startsWith('filesystem-exact');
const trashFallback = '当前环境无法调用系统废纸篓。请点击“访达”定位后，在访达中手动处理。';
function toast(msg, error=false){ const el=$('#toast'); el.textContent=msg; el.classList.toggle('error',!!error); el.classList.add('show'); clearTimeout(toast.t); toast.t=setTimeout(()=>el.classList.remove('show'),4000); }
async function api(path, opts={}){ const headers = {'Accept':'application/json', ...(opts.headers||{})}; if(opts.body){headers['Content-Type']='application/json';} if(state.token) headers['X-Atlas-Token']=state.token; const res=await fetch(path,{...opts,headers}); let body={}; try{body=await res.json();}catch{} if(!res.ok) throw new Error(body.error || `${res.status} ${res.statusText}`); return body; }
async function bootstrap(){
  try{
    state.health=await api('/api/health'); state.token=state.health.token||null;
    state.home=state.health.home||''; fillScopes(state.health.defaults); applyTrashCapability();
    setNotice(`本机服务已连接 · ${state.health.index?.available?'Spotlight 可用':'Spotlight 暂不可用'} · 所有数据留在本机`);
    await loadVolume(); await loadResults();
  }catch(e){setNotice(`无法连接本机服务：${e.message}`,true);}
}
function applyTrashCapability(){
  const available=trashAvailable(); const note=$('#trashCapability');
  note.hidden=available; note.textContent=available?'':trashFallback;
  $('#selectPage').disabled=!available;
  $('#reviewButton').title=available?'':trashFallback;
  if(!available){state.selected.clear();state.preview=null;$('#confirmTrash').disabled=true;}
  updateSelection();
}
function fillScopes(defaults={}){
  const sel=$('#scope');sel.replaceChildren();const home=state.home;const base=defaults.scan_path||home||'/';
  const opts=home?[[home,'用户目录 · ~'],[home+'/Downloads','下载 · ~/Downloads'],[home+'/Documents','文稿 · ~/Documents'],[home+'/Library','用户 Library · ~/Library']]:[];
  opts.push(['/Applications','应用程序 · /Applications'],['/','整个启动卷 · /']);
  if(defaults.data_path)opts.push([defaults.data_path,'数据卷 · '+defaults.data_path]);
  if(!opts.some(([path])=>path===base))opts.unshift([base,'默认目录 · '+pathLabel(base)]);
  const seen=new Set();for(const [path,label] of opts){if(seen.has(path))continue;seen.add(path);const option=document.createElement('option');option.value=path;option.textContent=label;sel.append(option);}
  const custom=document.createElement('option');custom.value='__custom__';custom.textContent='自定义绝对路径';sel.append(custom);
  state.scope=base;state.currentPath=base;sel.value=base;
}
async function loadVolume(){ const d=await api('/api/volumes'); const v=d.volumes?.find(x=>x.internal)||d.volumes?.[0]; if(!v)return; $('#used').textContent=fmt(v.used_bytes); $('#free').textContent=`可用 ${fmt(v.free_bytes)}`; $('#total').textContent=`总容量 ${fmt(v.total_bytes)}`; const pct=v.total_bytes? v.used_bytes/v.total_bytes*100:0; $('#percent').textContent=`${pct.toFixed(1)}% 已使用`; $('#meter').style.width=`${Math.min(100,pct)}%`; }
function setNotice(msg,error=false){const n=$('#notice');n.textContent=msg;n.classList.toggle('error',!!error);}
async function loadResults(){ if(state.view==='apps') return loadApps(); if(state.view==='tree') return loadTree(state.currentPath); return loadLargest(); }
async function loadLargest(){
  state.selected.clear();updateSelection();setNotice('正在读取所选范围的大文件…');
  try{
    const d=await api(`/api/largest?path=${encodeURIComponent(state.scope)}&limit=500`);
    state.rows=(d.files||[]).map(x=>({...x,size:x.allocated_size??x.size,kind:x.kind||'file'}));state.page=1;state.sort='size';state.dir=-1;render();
    const scanned=scannedSource(d);const limited=d.partial||d.complete===false;
    $('#source').textContent=scanned?(limited?'扫描结果 · 部分':'扫描结果'):'Spotlight · >100 MB';
    $('#mapCaption').textContent=`${pathLabel(state.scope)} · ${scanned?'扫描记录中的大文件候选':'索引候选可能遗漏未覆盖目录'}`;
    $('#resultNote').textContent='仅合计当前候选项目，不代表整个目录占用';
    const suffix=scanned?(limited?' · 扫描未完整覆盖全部项目':''):' · 系统索引不代表完整磁盘统计';
    setNotice(`${state.rows.length} 个候选文件${suffix}${d.error?' · 读取过程中存在问题':''}`,!!d.error);
  }catch(e){setNotice(`读取大文件失败：${e.message}`,true);}
}
async function loadTree(path){
  state.currentPath=path;state.scope=path;state.selected.clear();updateSelection();setNotice(`正在读取 ${pathLabel(path)}…`);
  try{
    const d=await api(`/api/tree?path=${encodeURIComponent(path)}&limit=500`);const actual=d.path||path;
    state.currentPath=actual;state.scope=actual;state.rows=(d.children||[]).map(x=>({...x,size:x.allocated_size??x.size,kind:x.kind||'directory'}));state.page=1;state.sort='size';state.dir=-1;render();
    const scanned=scannedSource(d);const partial=d.partial||d.complete===false;
    $('#source').textContent=scanned?(partial?'扫描结果 · 部分':'已扫描目录'):'浅层目录视图';
    $('#mapCaption').textContent=`${pathLabel(actual)} · ${scanned?(partial?'扫描存在未覆盖项目，合计不完整':'已扫描的目录占用'):'未扫描目录大小未知，仅显示浅层项目'}`;
    $('#resultNote').textContent=scanned?(partial?'部分扫描结果，目录合计可能偏小':'当前目录已显示项目的占用合计'):'未知目录未计入合计，请先扫描';
    $('#upButton').hidden=actual==='/';
    setNotice(`目录 ${pathLabel(actual)} · ${state.rows.length} 项${d.has_more?'（仅显示前 500 项）':''}${d.error_count?` · ${d.error_count} 个读取问题`:scanned&&partial?' · 统计不完整':''}`,!!d.error_count);
  }catch(e){setNotice(`目录读取失败：${e.message}`,true);}
}
async function loadApps(){ state.selected.clear();updateSelection();setNotice('正在读取应用本体大小…'); try{const d=await api('/api/apps'); state.rows=(d.apps||[]).map(x=>({...x,size:x.allocated_size??x.size,kind:'application'})); state.page=1;state.sort='size';state.dir=-1;render();$('#source').textContent='应用程序本体';$('#mapCaption').textContent='只计算 .app 本体大小；支持数据不会自动删除';setNotice(`${state.rows.length} 个应用 · 删除前请先确认应用用途`);}catch(e){setNotice(`应用列表读取失败：${e.message}`,true);}}
function visibleRows(){let a=state.rows.filter(r=>!state.query||`${r.name} ${r.path} ${r.kind}`.toLowerCase().includes(state.query.toLowerCase()));a.sort((x,y)=>{let k=state.sort==='name'?'name':state.sort==='modified'?'modified':'size';let av=x[k]??(k==='size'?-1:'');let bv=y[k]??(k==='size'?-1:'');return (av>bv?1:av<bv?-1:0)*state.dir;});return a;}
function render(){const rows=visibleRows();const total=rows.reduce((s,r)=>s+Number(r.size||0),0);$('#resultSize').textContent=fmt(total);const first=[...rows].sort((a,b)=>Number(b.size||0)-Number(a.size||0))[0];$('#largestSize').textContent=fmt(first?.size);$('#largestName').textContent=first?.name||'暂无结果';$('#count').textContent=`${rows.length} 个项目`;const pages=Math.max(1,Math.ceil(rows.length/state.pageSize));state.page=Math.min(state.page,pages);$('#page').textContent=`第 ${state.page}/${pages} 页`;$('#prev').disabled=state.page<=1;$('#next').disabled=state.page>=pages;const pageRows=rows.slice((state.page-1)*state.pageSize,state.page*state.pageSize);$('#rows').innerHTML=pageRows.length?pageRows.map(rowHtml).join(''):`<tr><td colspan="6"><div class="empty">没有匹配项目。可以换个范围、刷新 Spotlight，或先执行“扫描目录占用”。</div></td></tr>`;$$('.row-check').forEach(x=>x.checked=state.selected.has(x.dataset.path));bindRowActions();renderMap(rows.slice(0,30));}
function rowHtml(r){const can=canManage(r);const dir=r.kind==='directory';return `<tr class="${state.selected.has(r.path)?'highlight':''}"><td><input class="row-check" type="checkbox" data-path="${esc(r.path)}" ${state.selected.has(r.path)?'checked':''} ${can?'':'disabled'} aria-label="选择 ${esc(r.name)}"></td><td class="name-cell"><button class="name-button ${dir?'is-dir':''}" data-open-path="${esc(r.path)}">${esc(r.name||nameOf(r.path))}</button><small class="path">${esc(pathLabel(r.path))}${r.trash_reason?` · ${esc(r.trash_reason)}`:''}</small></td><td class="number">${fmt(r.size)}</td><td class="number">${fmt(r.logical_size??r.size)}</td><td>${date(r.modified)}</td><td><button class="row-action" data-reveal-path="${esc(r.path)}">访达</button> ${dir?'<button class="row-action" data-open-path="'+esc(r.path)+'">打开</button>':can?'<button class="row-action" data-review-path="'+esc(r.path)+'">复核</button>':'<span title="'+esc(trashAvailable()?(r.trash_reason||'受保护'):trashFallback)+'">'+(trashAvailable()?'受保护':'访达处理')+'</span>'}</td></tr>`;}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function bindRowActions(){$$('.row-check').forEach(x=>x.addEventListener('change',()=>{x.checked?state.selected.add(x.dataset.path):state.selected.delete(x.dataset.path);updateSelection();render();}));$$('[data-open-path]').forEach(x=>x.addEventListener('click',()=>{const r=state.rows.find(y=>y.path===x.dataset.openPath);if(r?.kind==='directory')loadTree(x.dataset.openPath);else reveal(x.dataset.openPath);}));$$('[data-review-path]').forEach(x=>x.addEventListener('click',()=>{state.selected.add(x.dataset.reviewPath);updateSelection();openReview();}));$$('[data-reveal-path]').forEach(x=>x.addEventListener('click',()=>reveal(x.dataset.revealPath)));}
function renderMap(rows){const el=$('#treemap');const weighted=rows.filter(r=>Number(r.size)>0).slice(0,24);if(!weighted.length){el.innerHTML='<div class="empty">当前结果没有可绘制的占用数据</div>';return;}const rects=layoutMap(weighted,0,0,100,100);const colors=['#4e6cdb','#6484e8','#7d98ea','#98a9e8','#b0bdeb','#7790d9'];el.innerHTML=weighted.map((r,i)=>{const q=rects[i];const pct=Number(r.size)/weighted.reduce((a,x)=>a+Number(x.size),0)*100;return `<button class="tile ${pct<3?'tiny':''}" data-open-path="${esc(r.path)}" style="left:${q.x}%;top:${q.y}%;width:${q.w}%;height:${q.h}%;background:${colors[i%colors.length]}"><span>${esc(r.name||nameOf(r.path))}</span><small>${fmt(r.size)} · ${pct.toFixed(1)}%</small></button>`;}).join('');$$('.tile',el).forEach(x=>x.addEventListener('click',()=>{const r=state.rows.find(y=>y.path===x.dataset.openPath);if(r?.kind==='directory')loadTree(r.path);else if(state.view==='tree'&&r?.kind==='file')reveal(r.path);else{state.query=r.name;$('#filter').value=r.name;render();}}));}
function layoutMap(items,x,y,w,h){if(items.length===1)return[{x,y,w,h}];const total=items.reduce((a,r)=>a+Number(r.size),0)||1;let sum=0,cut=1;for(;cut<items.length;cut++){sum+=Number(items[cut-1].size);if(sum>=total/2)break;}sum=items.slice(0,cut).reduce((a,r)=>a+Number(r.size),0);const ratio=sum/total;let first,second;if(w>=h){const part=w*ratio;first=layoutMap(items.slice(0,cut),x,y,part,h);second=layoutMap(items.slice(cut),x+part,y,w-part,h);}else{const part=h*ratio;first=layoutMap(items.slice(0,cut),x,y,w,part);second=layoutMap(items.slice(cut),x,y+part,w,h-part);}return first.concat(second);}
function updateSelection(){const n=state.selected.size;$('#selectionCount').textContent=trashAvailable()?(n?`已选择 ${n} 项`:'未选择项目'):'请通过访达管理文件';$('#reviewButton').disabled=!trashAvailable()||!n;}

async function openReview(){if(!trashAvailable()){toast(trashFallback);return;}state.preview=null;const paths=[...state.selected].filter(path=>state.rows.some(r=>r.path===path&&canManage(r)));if(!paths.length)return;const d=$('#reviewDialog');$('#reviewError').textContent='正在读取复核信息…';$('#reviewItems').innerHTML='';$('#confirmTrash').disabled=true;d.showModal();try{const p=await api('/api/trash/preview',{method:'POST',body:JSON.stringify({paths})});state.preview=p;$('#reviewItems').innerHTML=(p.items||[]).map(x=>`<div class="review-item"><b>${esc(x.name)}</b><small>${esc(pathLabel(x.path))} · 分配 ${fmt(x.allocated_size??x.size)} · 文件 ${fmt(x.logical_size??x.size)}${x.trash_reason?` · ${esc(x.trash_reason)}`:''}</small></div>`).join('');$('#reviewError').textContent=p.expires_in?`复核令牌 ${p.expires_in} 秒后失效。`:'';$('#confirmTrash').disabled=!(p.confirmation_token&&p.items?.length); }catch(e){$('#reviewError').textContent=e.message;}}
async function confirmTrash(){if(!trashAvailable()){toast(trashFallback);return;}if(!state.preview?.confirmation_token||state.trashPending)return;state.trashPending=true;$('#confirmTrash').disabled=true;try{const d=await api('/api/trash',{method:'POST',body:JSON.stringify({confirmation_token:state.preview.confirmation_token})});$('#reviewDialog').close();state.preview=null;state.selected.clear();updateSelection();toast(`${d.trashed?.length||0} 个项目已移入废纸篓。废纸篓未清空，可在访达中恢复。`);await loadResults();}catch(e){$('#reviewError').textContent=e.message;}finally{state.trashPending=false;$('#confirmTrash').disabled=!trashAvailable()||!state.preview?.confirmation_token;}}
async function reveal(path){try{await api('/api/reveal',{method:'POST',body:JSON.stringify({path})});}catch(e){toast(e.message,true);}}
function updateScanControls(){const busy=!!state.scanTimer||!!state.scanStarting;$('#scanButton').disabled=busy;$('#cancelScan').disabled=!busy||!!state.cancelRequested;}
function beginScanPolling(){if(!state.scanTimer)state.scanTimer=setInterval(pollScanStatus,700);updateScanControls();pollScanStatus();}
async function pollScanStatus(){
  if(state.scanPolling)return;state.scanPolling=true;
  try{
    const s=await api('/api/scan/status');$('#scanDetail').textContent=`${(s.files_scanned||0).toLocaleString()} 个文件 · ${(s.dirs_scanned||0).toLocaleString()} 个目录${s.error_count?` · ${s.error_count} 个读取问题`:''}`;
    if(!s.running){
      clearInterval(state.scanTimer);state.scanTimer=null;state.scanStarting=false;state.cancelRequested=false;$('#scanStatus').hidden=true;updateScanControls();
      const message=s.cancelled?'扫描已取消，保留已经读取的部分结果':s.error_count?`扫描结束，有 ${s.error_count} 个读取问题；统计不完整`:s.complete?'扫描完成，结果已保存':'扫描已结束，结果尚不完整';
      toast(message,!!s.error_count);await loadResults();
    }
  }catch(e){$('#scanLabel').textContent='暂时无法读取扫描状态';toast(e.message,true);}
  finally{state.scanPolling=false;}
}
async function startScan(){
  if(state.scanTimer||state.scanStarting)return;state.scanStarting=true;state.cancelRequested=false;updateScanControls();const path=state.scope;
  $('#scanStatus').hidden=false;$('#scanLabel').textContent='正在扫描目录';$('#scanDetail').textContent=`${pathLabel(path)} · 不读取文件内容`;
  try{
    await api('/api/scan',{method:'POST',body:JSON.stringify({path})});state.scanStarting=false;
    if(state.cancelRequested){try{await api('/api/scan/cancel',{method:'POST',body:'{}'});}catch(e){state.cancelRequested=false;toast(`取消失败：${e.message}`,true);}}
    beginScanPolling();
  }catch(e){state.scanStarting=false;state.cancelRequested=false;$('#scanStatus').hidden=true;updateScanControls();toast(`扫描无法开始：${e.message}`,true);}
}
async function cancelScan(){
  if(!state.scanTimer&&!state.scanStarting)return;state.cancelRequested=true;updateScanControls();$('#scanLabel').textContent='正在请求停止扫描';
  if(state.scanStarting){toast('扫描启动后将立即请求停止');return;}
  try{const r=await api('/api/scan/cancel',{method:'POST',body:'{}'});$('#scanLabel').textContent=r.running?'正在等待扫描停止':'扫描已停止';await pollScanStatus();}
  catch(e){state.cancelRequested=false;updateScanControls();$('#scanLabel').textContent='扫描仍在进行';toast(`取消失败：${e.message}`,true);}
}
async function runSpotlightSearch(query){ if(!query){await loadResults();return;} setNotice(`正在用 Spotlight 搜索“${query}”…`); try{const d=await api(`/api/search?q=${encodeURIComponent(query)}&path=${encodeURIComponent(state.scope)}&limit=500`); state.rows=(d.results||[]).map(x=>({...x,size:x.allocated_size??x.size,kind:x.kind||'file'})); state.page=1;state.sort='size';state.dir=-1;render();$('#source').textContent='Spotlight 搜索';setNotice(`${state.rows.length} 个 Spotlight 结果${d.partial?' · 已展示部分结果，可能存在未覆盖项目':''}`);}catch(e){setNotice(`Spotlight 搜索失败：${e.message}`,true);} }
function bind(){$('#scope').addEventListener('change',()=>{if($('#scope').value==='__custom__'){$('#customPath').hidden=false;$('#customPath').focus();}else{$('#customPath').hidden=true;state.scope=$('#scope').value;state.currentPath=state.scope;loadResults();}});$('#customPath').addEventListener('change',()=>{state.scope=$('#customPath').value.trim();state.currentPath=state.scope;loadResults();});$('#refreshDisk').addEventListener('click',()=>loadVolume());$('#indexButton').addEventListener('click',()=>loadLargest());$('#scanButton').addEventListener('click',startScan);$('#cancelScan').addEventListener('click',cancelScan);$('#reviewButton').addEventListener('click',openReview);$('#confirmTrash').addEventListener('click',confirmTrash);$('#closeReview').addEventListener('click',()=>$('#reviewDialog').close());$('#cancelReview').addEventListener('click',()=>$('#reviewDialog').close());$('#helpButton').addEventListener('click',()=>$('#helpDialog').showModal());$('#closeHelp').addEventListener('click',()=>$('#helpDialog').close());$('#filter').addEventListener('input',e=>{state.query=e.target.value;state.page=1;render();});$('#search').addEventListener('keydown',e=>{if(e.key==='Enter'){runSpotlightSearch(e.target.value.trim());}});$('#search').addEventListener('input',e=>{clearTimeout(state.searchTimer);state.searchTimer=setTimeout(()=>{if(e.target.value.trim()){$('#filter').value=e.target.value;state.query=e.target.value;render();}},280);});$$('nav button').forEach(b=>b.addEventListener('click',()=>{$$('nav button').forEach(x=>x.classList.remove('active'));b.classList.add('active');state.view=b.dataset.view;$('#listTitle').textContent=state.view==='apps'?'应用程序':state.view==='tree'?'目录视图':'大文件';loadResults();}));$$('[data-sort]').forEach(b=>b.addEventListener('click',()=>{state.sort=b.dataset.sort;state.dir*=-1;render();}));$('#prev').addEventListener('click',()=>{state.page--;render();});$('#next').addEventListener('click',()=>{state.page++;render();});$('#upButton').addEventListener('click',()=>{const p=state.currentPath.replace(/\/[^/]+$/,'')||'/';loadTree(p);});$('#selectPage').addEventListener('change',e=>{visibleRows().slice((state.page-1)*state.pageSize,state.page*state.pageSize).filter(canManage).forEach(r=>e.target.checked?state.selected.add(r.path):state.selected.delete(r.path));updateSelection();render();});$('#exportButton').addEventListener('click',()=>{const cell=v=>{const s=String(v??'').replace(/[\t\r\n]+/g,' ');return /^[=+\-@]/.test(s)?`'${s}`:s;};const text=visibleRows().map(r=>[cell(r.name),cell(r.size),cell(r.path)].join('\t')).join('\n');const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([text],{type:'text/tab-separated-values'}));a.download='磁盘空间结果.tsv';a.click();URL.revokeObjectURL(a.href);});document.addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'){e.preventDefault();$('#search').focus();}if(e.key==='Escape')$$('dialog[open]').forEach(x=>x.close());});}
bind();bootstrap();
