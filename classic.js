'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const S = { previewOnly: false, detailsHistory: {}, lastScan: null, autoStarting: false, switchingDetails: false, followDetails: true, mode: 'quick', quickFallback: false, quickRoot: '', quickMeta: null, quickLoading: false, quickTimer: null, quickAbort: null, quickStarted: 0, quickReused: false, home: '', token: '', root: null, rootPath: '', nodes: new Map(), selected: null, panelPath: '', volumes: [], filter: '', sort: 'size', direction: -1, extension: null, map: null, extensions: [], treeVersion: 0, panelVersion: 0, scanTimer: null, scanPending: false, scanRunning: false, scanPath: '', scanPollBusy: false, errors: [], ready: false, panelAbort: null };
const palette = ['#45a36c', '#4388c4', '#b568c8', '#ca883b', '#46a5a4', '#bd6473', '#93a23a', '#7469c6', '#bc6550', '#4c9bbb', '#bc9f45', '#5d9c7c'];
const fmt = value => { if (value == null || !Number.isFinite(Number(value))) return '—'; let n = Math.max(0, Number(value)), i = 0; const units = ['B', 'KB', 'MB', 'GB', 'TB']; while (n >= 1000 && i < units.length - 1) { n /= 1000; i++; } return `${n.toFixed(i === 0 ? 0 : n >= 100 ? 1 : 2)} ${units[i]}`; };
const count = value => value == null ? '—' : Number(value).toLocaleString('zh-CN');
const nameOf = path => path === '/' ? '/' : path?.split('/').filter(Boolean).pop() || '';
const parentOf = path => path === '/' ? '/' : path.replace(/\/+$/, '').replace(/\/[^/]+$/, '') || '/';
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const attr = escapeHtml;
const allocated = node => node?.allocated_size ?? node?.size ?? null;
const within = (path, root) => root === '/' ? path?.startsWith('/') : path === root || path?.startsWith(`${root}/`);
function extensionOf(node) { if (node.extension != null) return String(node.extension).replace(/^\./, '').toLowerCase(); const n = node.name || nameOf(node.path); const at = n.lastIndexOf('.'); return at > 0 ? n.slice(at + 1).toLowerCase() : '(no extension)'; }
function extKey(value) { const v = String(value || '(no extension)').replace(/^\./, '').toLowerCase(); return ['(no extension)', '(none)', '无扩展名', ''].includes(v) ? '(no extension)' : v; }
function colorFor(extension) { const key = extKey(extension); let hash = 2166136261; for (let i = 0; i < key.length; i++) { hash ^= key.charCodeAt(i); hash = Math.imul(hash, 16777619); } return palette[(hash >>> 0) % palette.length]; }
function sourceLabel(data) { if ((S.mode === 'quick' && !S.quickFallback) || data.coverage === 'indexed-large-files-only') return data.query_complete === false ? '索引候选 · 查询不完整' : 'Spotlight 候选'; if (data.requires_scan) return '尚未扫描'; if (data.partial || data.error_count || data.complete === false) return '部分结果'; if (data.source?.startsWith('filesystem-exact')) return '已扫描'; if (data.source === 'filesystem-shallow') return '浅层目录'; return data.complete ? '已扫描' : '本机数据'; }
function viewUrl(endpoint, path, extra = {}) { const params = new URLSearchParams({ path, ...extra }); if (S.mode === 'quick' && !S.quickFallback) { params.set('mode', 'quick'); params.set('scope', S.quickRoot || S.rootPath); } return `/api/${endpoint}?${params}`; }
function updateMode() {
  const quick = S.mode === 'quick', candidates = quick && !S.quickFallback; $('.mode-bar').classList.toggle('full', !quick); $('#coverageNote').hidden = !quick;
  $$('[data-mode]').forEach(button => { const active = button.dataset.mode === S.mode; button.classList.toggle('active', active); button.setAttribute('aria-selected', String(active)); });
  $('#modeDescription').textContent = quick ? (S.previewOnly ? '仅显示索引候选，不启动后台补全' : '先显示索引结果，再在后台补全细节') : '展示已读取的目录；权限缺口会标注为部分结果';
  $('#treeTitle').textContent = candidates ? '大文件候选树' : quick ? '目录浅层预览' : '目录树'; $('#scanButton').textContent = quick ? '刷新快速结果' : '重新完整扫描'; $('#previewOnly').checked = S.previewOnly;
  $$('[data-full-label]').forEach(button => { button.firstChild.textContent = `${candidates ? button.dataset.quickLabel : button.dataset.fullLabel} `; });
  $('#extPctHeader').textContent = candidates ? '候选占比' : '占比'; $('#extSizeHeader').textContent = candidates ? '候选占用' : '磁盘占用'; $('#extCountHeader').textContent = candidates ? '候选数' : '文件数';
  $('#coverageNote strong').textContent = S.quickFallback ? '索引未返回可用结果，当前展示浅层目录，未知子目录占用显示为 —。' : '只含已索引且文件大小 ≥100 MB 的候选文件，不代表全部目录占用。'; $('#coverageNote > span').textContent = S.quickFallback ? (S.previewOnly ? '仅快速预览已开启，不会自动完整扫描。' : '目录细节将在后台补全，仍可展开和停止。') : '目录计数、类型占比和矩形面积均基于这些候选。';
  $('#treeHint').textContent = candidates ? '仅显示索引候选及其父目录 · 单击选择 / ＋ 展开' : '单击选择 · ＋ 展开目录 · 双击文件在访达显示'; updateQuickMeta(); controls();
}
function updateQuickMeta() {
  if (S.mode !== 'quick') { $('#quickMeta').textContent = '目录详情 · 不重新启动扫描'; return; }
  if (S.quickLoading) { $('#quickMeta').textContent = `正在${S.quickReused ? '读取快照' : '查询索引'} · ${((Date.now() - S.quickStarted) / 1000).toFixed(1)} 秒`; return; }
  const meta = S.quickMeta; if (!meta) { $('#quickMeta').textContent = 'Spotlight 系统索引'; return; }
  if (meta.available === false || meta.failed) { $('#quickMeta').textContent = meta.failed ? '索引查询未完成 · 显示浅层预览' : 'Spotlight 不可用 · 显示浅层预览'; return; }
  const cached = meta.cache_hit || meta.cache?.hit; const age = Number(meta.age_seconds ?? meta.cache?.age_seconds ?? 0);
  const origin = S.quickReused ? '复用本次快照' : cached ? `缓存${age >= 1 ? ` · ${Math.round(age)} 秒前` : ''}` : '新查询';
  $('#quickMeta').textContent = `${origin} · ${count(meta.indexed_file_count ?? meta.candidate_count)} 个候选 · 建立用时 ${(Number(meta.elapsed_ms || 0) / 1000).toFixed(2)} 秒`;
  $('#quickMeta').title = `索引快照范围：${S.quickRoot}${meta.error ? `；${meta.error}` : ''}`;
}
function quickEmptyMessage() { if (S.quickMeta?.available === false) return 'Spotlight 当前不可用。请切换到完整扫描，再手动开始扫描。'; if (S.quickMeta?.error) return `索引查询未完成：${S.quickMeta.error}。可刷新快速结果，或切换目录详情。`; return '没有找到已索引且 ≥100 MB 的候选文件。可切换完整扫描查看其他占用。'; }
async function changeMode(mode) { if (!['quick', 'full'].includes(mode) || mode === S.mode) return; S.followDetails = mode !== 'quick'; S.mode = mode; updateMode(); await changeRoot(S.rootPath || S.home, { autoDetails: false }); }

function toast(message) { const el = $('#toast'); el.textContent = message; el.classList.add('show'); clearTimeout(toast.timer); toast.timer = setTimeout(() => el.classList.remove('show'), 3500); }
function status(message, kind = '') { $('#statusText').textContent = message; $('#statusLight').className = `status-light ${kind}`; }
function controls() { const quick = S.mode === 'quick'; $('#scanButton').disabled = !S.ready || !S.rootPath || (quick ? S.quickLoading : S.scanPending || S.scanRunning); $('#cancelButton').disabled = !S.scanRunning; $('#cancelButton').textContent = quick ? '停止后台补全' : '停止完整扫描'; $('#openPath').disabled = !S.ready; $('#rootSelect').disabled = !S.ready; $('#rootPath').disabled = !S.ready; $('#upButton').disabled = !S.ready || !S.rootPath || S.rootPath === '/'; $('#refreshButton').disabled = !S.ready || S.quickLoading; $('#revealButton').disabled = !S.ready || !S.selected?.path || S.selected.can_reveal === false; $$('[data-mode]').forEach(button => button.disabled = !S.ready); }
async function api(path, options = {}) { const headers = { Accept: 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...(S.token ? { 'X-Atlas-Token': S.token } : {}) }; const response = await fetch(path, { ...options, headers }); let data; try { data = await response.json(); } catch { throw new Error(`服务未返回有效数据 (${response.status})`); } if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`); return data; }
function dateText(value) { if (!value) return '—'; const d = new Date(value); return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }); }

async function boot() {
  try {
    const health = await api('/api/health'); S.home = health.home || health.defaults?.scan_path || '/'; S.token = health.token || ''; S.ready = true;
    readPreferences(); await loadVolumes(); updateMode(); await pollScan(true); await changeRoot(S.home);
  } catch (error) { status(`无法连接本机服务：${error.message}`, 'error'); $('#treeEmpty').textContent = '请通过“磁盘空间管理”桌面入口启动本机服务后重试。'; }
}
async function loadVolumes() {
  const data = await api('/api/volumes'); S.volumes = data.volumes || [];
  const select = $('#rootSelect'); select.replaceChildren(); const paths = new Set();
  const add = (path, label) => { if (paths.has(path)) return; const option = document.createElement('option'); option.value = path; option.textContent = label; select.append(option); paths.add(path); };
  add(S.home, '用户目录'); for (const volume of S.volumes) add(volume.path, `${volume.name} · ${volume.path === "/" ? "启动卷" : volume.path === "/System/Volumes/Data" ? "数据卷" : volume.path} · ${fmt(volume.total_bytes)}`); add('__custom__', '自定义目录…'); updateVolume();
}
function updateVolume() {
  const volume = S.volumes.filter(v => within(S.rootPath || S.home, v.path)).sort((a, b) => b.path.length - a.path.length)[0] || S.volumes[0]; if (!volume) return;
  $('#diskName').textContent = volume.name; $('#usedValue').textContent = fmt(volume.used_bytes); $('#freeValue').textContent = fmt(volume.free_bytes); $('#totalValue').textContent = fmt(volume.total_bytes); $('#capacityFill').style.width = `${volume.total_bytes ? Math.min(100, volume.used_bytes / volume.total_bytes * 100) : 0}%`;
}
function makeNode(data, parent = null) { return { ...data, parent, _expanded: false, _loaded: false, _loading: false, _error: '', _children: [] }; }
async function changeRoot(path, options = {}) {
  path = String(path || '').trim(); if (!path.startsWith('/')) { toast('请输入以 / 开头的绝对目录路径'); return; }
  const version = ++S.treeVersion; if (options.autoDetails !== false) S.followDetails = true; ++S.panelVersion; S.panelAbort?.abort(); S.quickAbort?.abort(); clearInterval(S.quickTimer); S.quickTimer = null;
  const quick = S.mode === 'quick'; S.quickLoading = quick; S.quickStarted = Date.now(); S.quickReused = quick && !options.refreshQuick && !!S.quickMeta && within(path, S.quickRoot);
  S.quickFallback = false; S.rootPath = path; S.root = null; S.nodes.clear(); S.selected = null; S.panelPath = ''; S.map = null; S.extensions = []; S.extension = null;
  $('#rootPath').value = path; const option = [...$('#rootSelect').options].find(o => o.value === path); $('#rootSelect').value = option ? path : '__custom__';
  $('#treeRows').replaceChildren(); $('#treeEmpty').hidden = false; $('#treeEmpty').textContent = quick ? '正在读取 Spotlight 大文件索引…' : '正在读取目录…'; $('#treeSource').textContent = '读取中'; $('#treeCount').textContent = quick ? '0 个已加载候选项目' : '0 个已加载项目'; $('#selectedPath').textContent = path; $('#useFullButton').hidden = true;
  status(quick ? `正在读取索引候选 · ${path}…` : `读取 ${path}…`, 'busy'); controls(); updateVolume(); setPanelsLoading(path); updateQuickMeta();
  if (quick) S.quickTimer = setInterval(updateQuickMeta, 250);
  try {
    if (quick && !S.quickReused) {
      const abort = new AbortController(); S.quickAbort = abort;
      try {
        const meta = await api(`/api/quick?path=${encodeURIComponent(path)}${options.refreshQuick ? '&refresh=1' : ''}`, { signal: abort.signal }); if (version !== S.treeVersion) return;
        S.quickMeta = meta; S.quickRoot = meta.scope || meta.root || meta.path || path;
      } catch (error) {
        if (version !== S.treeVersion || error.name === 'AbortError') return;
        S.quickMeta = { failed: true, error: error.message, candidate_count: 0 }; S.quickRoot = path;
      }
    }
    if (quick) { S.quickFallback = S.quickMeta?.available === false || S.quickMeta?.failed || S.quickMeta?.empty === true || Number(S.quickMeta?.indexed_file_count ?? S.quickMeta?.candidate_count ?? 0) === 0; updateMode(); }
    const data = await api(viewUrl('tree', path, { limit: 2000 })); if (version !== S.treeVersion) return;
    if (data.kind && data.kind !== 'directory') throw new Error('该路径不是目录，请选择它的上级文件夹');
    S.rootPath = data.path || path; S.root = makeNode({ ...data, kind: 'directory' }); S.root._expanded = true; S.nodes.set(S.root.path, S.root); mergeChildren(S.root, data); S.selected = S.root; $('#treeSource').textContent = sourceLabel(data); renderTree(); controls(); await selectNode(S.root, true); if (version !== S.treeVersion) return;
    if (quick) {
      const candidates = S.quickFallback ? 0 : data.files ?? S.quickMeta?.candidate_count ?? 0; const incomplete = S.quickMeta?.query_complete === false;
      S.quickLoading = false; clearInterval(S.quickTimer); S.quickTimer = null; updateQuickMeta(); controls();
      $('#useFullButton').hidden = candidates > 0 && !incomplete;
      status(candidates ? `${count(candidates)} 个索引候选文件 · 只含 ≥100 MB 大文件，不代表全部目录占用${incomplete ? ' · 索引查询未完成' : ''}` : `索引未返回可用结果 · 已显示浅层目录${S.previewOnly ? '，仅快速预览，不自动补全' : '，准备在后台补全，仍可展开 / 停止'}`, incomplete ? 'error' : 'good');
      if (options.autoDetails !== false) await maybeStartDetails(S.rootPath, version);
    } else if (!S.scanRunning) status(data.source?.startsWith('filesystem-exact') ? `${sourceLabel(data)} · ${data.path}${data.error_count ? ` · ${data.error_count} 个读取问题` : ''}` : `尚无完整目录统计；点击“开始完整扫描”逐项核算 · ${data.path}`, data.error_count ? 'error' : 'good');
  } catch (error) {
    if (version !== S.treeVersion || error.name === 'AbortError') return;
    $('#treeEmpty').hidden = false; $('#treeEmpty').textContent = error.message; $('#treeSource').textContent = quick ? '索引不可用' : '读取失败'; status(`${quick ? '索引' : '目录'}读取失败：${error.message}`, 'error'); setPanelError(quick ? error.message : '无法读取所选目录'); if (quick) $('#useFullButton').hidden = false;
  } finally {
    if (version === S.treeVersion) { S.quickLoading = false; clearInterval(S.quickTimer); S.quickTimer = null; updateQuickMeta(); controls(); }
  }
}

function mergeChildren(node, data) {
  Object.assign(node, { size: data.size, allocated_size: data.allocated_size, logical_size: data.logical_size, files: data.files, folders: data.folders, items: data.items, modified: data.modified, complete: data.complete, partial: data.partial, error_count: data.error_count, source: data.source, has_more: data.has_more });
  node._children = (data.children || []).map(item => { const child = makeNode(item, node); S.nodes.set(child.path, child); return child; }); node._loaded = true; node._loading = false; node._error = '';
}
async function expandNode(node) {
  if (!node || node.kind !== 'directory' || node._loading) return;
  if (node._loaded) { node._expanded = !node._expanded; renderTree(); return; }
  node._expanded = true; node._loading = true; renderTree(); const version = S.treeVersion;
  try { const data = await api(viewUrl('tree', node.path, { limit: 2000 })); if (version !== S.treeVersion) return; mergeChildren(node, data); renderTree(); }
  catch (error) { if (version !== S.treeVersion) return; node._loading = false; node._error = error.message; renderTree(); toast(`目录展开失败：${error.message}`); }
}
function sorted(nodes) { return [...nodes].sort((a, b) => { let av = S.sort === 'size' ? allocated(a) : a[S.sort], bv = S.sort === 'size' ? allocated(b) : b[S.sort]; if (av == null && bv != null) return 1; if (bv == null && av != null) return -1; if (typeof av === 'string') return av.localeCompare(bv || '', 'zh-CN', { numeric: true }) * S.direction; return ((av || 0) - (bv || 0)) * S.direction; }); }
function matches(node) { return `${node.name} ${node.path}`.toLowerCase().includes(S.filter); }
function branchMatches(node) { return matches(node) || node._children.some(branchMatches); }
function renderTree() {
  if (!S.root) return; const rows = [];
  const walk = (node, depth) => { if (S.filter && !branchMatches(node)) return; rows.push(rowHtml(node, depth)); if (node._expanded || S.filter) { for (const child of sorted(node._children)) walk(child, depth + 1); if (node._loading) rows.push(`<tr class="loading-row"><td colspan="8" style="padding-left:${24 + depth * 15}px">正在读取…</td></tr>`); if (node._error) rows.push(`<tr class="loading-row"><td colspan="8">${escapeHtml(node._error)}</td></tr>`); if (node.has_more) rows.push(`<tr class="more-row"><td colspan="8">此目录仅显示前 2,000 个${S.mode === 'quick' && !S.quickFallback ? '候选项目；总量仅包含快照中的索引大文件' : '项目；目录总量包含已扫描的全部项目'}。</td></tr>`); } };
  walk(S.root, 0); $('#treeRows').innerHTML = rows.join(''); $('#treeEmpty').hidden = rows.length > 0; $('#treeEmpty').textContent = '已加载的目录中没有匹配项。展开更多目录后可继续筛选。'; $('#treeCount').textContent = `${(S.nodes.size - 1).toLocaleString()} 个已加载${S.mode === 'quick' && !S.quickFallback ? '候选' : ''}项目`; controls();
}
function rowHtml(node, depth) {
  const dir = node.kind === 'directory'; const size = allocated(node); const psize = allocated(node.parent); const pct = node.parent ? (node.percent_parent ?? (size != null && psize > 0 ? size / psize * 100 : null)) : size > 0 ? 100 : null;
  const toggle = dir ? `<button class="tree-toggle" data-toggle="${attr(node.path)}" aria-label="${node._expanded ? '收起' : '展开'} ${attr(node.name)}" ${node._loading ? 'disabled' : ''}>${node._loading ? '·' : node._expanded ? '−' : '+'}</button>` : '<span class="toggle-space"></span>';
  return `<tr data-path="${attr(node.path)}" tabindex="0" class="${S.selected?.path === node.path ? 'selected' : ''}" ${dir ? `aria-expanded="${node._expanded}"` : ''}><td title="${attr(node.path)}"><div class="tree-name" style="padding-left:${depth * 15}px">${toggle}<span class="node-icon ${dir ? '' : 'file'}">${dir ? '▰' : '▤'}</span><span class="node-label">${escapeHtml(node.name || node.path)}</span></div></td><td><div class="tree-pct">${pct == null ? '—' : `<i style="width:${Math.max(0, Math.min(100, pct))}%"></i><span>${Number(pct).toFixed(1)}%</span>`}</div></td><td>${fmt(node.logical_size)}</td><td>${fmt(size)}</td><td>${count(node.items)}</td><td>${count(node.files)}</td><td>${count(node.folders)}</td><td>${dateText(node.modified)}</td></tr>`;
}
function selectNode(node, force = false) {
  S.selected = node; $('#selectedPath').textContent = node.path || ''; controls(); renderTree(); highlightMap();
  if (node.kind === 'directory' && (force || S.panelPath !== node.path)) return loadPanels(node.path);
}
function setPanelsLoading(path) { $('#extensionRows').replaceChildren(); $('#extensionEmpty').hidden = false; $('#extensionEmpty').textContent = '正在读取所选目录的类型统计…'; $('#extensionCount').textContent = '读取中'; $('#extensionSource').textContent = path; $('#mapPath').textContent = path; $('#mapSource').textContent = '读取中'; $('#treemap').innerHTML = `<div class="empty-state">${S.mode === 'quick' ? '正在读取索引候选的空间分布…' : '正在读取已扫描的目录分布…'}</div>`; $('#mapSize').textContent = '—'; $('#mapHover').textContent = '读取中'; }
function setPanelError(message) { $('#extensionEmpty').hidden = false; $('#extensionEmpty').textContent = message; $('#extensionCount').textContent = '—'; $('#mapSource').textContent = '读取失败'; $('#treemap').innerHTML = `<div class="empty-state">${escapeHtml(message)}</div>`; }
async function loadPanels(path) {
  const version = ++S.panelVersion; S.panelPath = path; S.extension = null; S.map = null; S.extensions = []; S.panelAbort?.abort(); const abort = new AbortController(); S.panelAbort = abort; $('#clearExtension').hidden = true; setPanelsLoading(path);
  const requests = [api(viewUrl('extensions', path), { signal: abort.signal }), api(viewUrl('treemap', path, { depth: $('#mapDepth').value, maxnodes: 800 }), { signal: abort.signal })];
  const results = await Promise.allSettled(requests); if (version !== S.panelVersion) return;
  const [ext, map] = results;
  if (ext.status === 'fulfilled') { const d = ext.value; S.extensions = (d.extensions || []).sort((a, b) => (allocated(b) || 0) - (allocated(a) || 0)); renderExtensions(d); }
  else if (ext.reason.name !== 'AbortError') { $('#extensionEmpty').hidden = false; $('#extensionEmpty').textContent = `统计读取失败：${ext.reason.message}`; $('#extensionCount').textContent = '错误'; }
  if (map.status === 'fulfilled') { S.map = map.value; $('#mapSource').textContent = `${sourceLabel(S.map)}${S.map.truncated ? ' · 细节已合并' : ''}`; renderMap(); }
  else if (map.reason.name !== 'AbortError') { $('#mapSource').textContent = '错误'; $('#treemap').innerHTML = `<div class="empty-state">矩形图读取失败：${escapeHtml(map.reason.message)}</div>`; }
}
function renderExtensions(data) {
  $('#extensionCount').textContent = `${S.extensions.length} 种`; $('#extensionEmpty').hidden = S.extensions.length > 0; $('#extensionEmpty').textContent = S.mode === 'quick' ? (S.quickFallback ? (S.previewOnly ? '浅层预览暂不统计类型。可手动开始完整扫描。' : '正在等待后台目录详情，浏览和定位仍可使用。') : quickEmptyMessage()) : data.requires_scan ? '尚未扫描此目录。点击“开始完整扫描”建立类型统计。' : '此目录没有已统计的文件。';
  const total = S.extensions.reduce((sum, r) => sum + Number(allocated(r) || 0), 0);
  $('#extensionRows').innerHTML = S.extensions.map(row => { const key = extKey(row.extension), pct = row.percent ?? (total ? Number(allocated(row) || 0) / total * 100 : 0); return `<tr data-extension="${attr(key)}" tabindex="0"><td title="${attr(key)}"><div class="extension-name"><i class="color-swatch" style="background:${colorFor(key)}"></i><span>${escapeHtml(key === '(no extension)' ? '无扩展名' : `.${key}`)}</span></div></td><td>${Number(pct).toFixed(1)}%</td><td>${fmt(allocated(row))}</td><td>${count(row.count)}</td></tr>`; }).join('');
  $('#extensionSource').textContent = `${sourceLabel(data)}${data.error_count ? ` · ${data.error_count} 个读取问题` : ''}${S.mode === 'quick' ? ' · 占比仅基于候选' : ''} · 点击类型高亮矩形图`;
}
function setExtension(key) { S.extension = S.extension === key ? null : key; $('#clearExtension').hidden = !S.extension; $$('#extensionRows tr').forEach(row => row.classList.toggle('active', row.dataset.extension === S.extension)); highlightMap(); }

// Squarified sibling rectangles preserve the reported allocated-byte ratios.
function squarify(items, x, y, width, height) {
  const sum = items.reduce((n, item) => n + Number(allocated(item) || 0), 0); if (!(sum > 0 && width > 0 && height > 0)) return [];
  const queue = items.map(item => ({ item, area: Number(allocated(item) || 0) / sum * width * height })).filter(i => i.area > 0).sort((a, b) => b.area - a.area); const output = []; let row = [];
  const worst = (r, side) => { if (!r.length || side <= 0) return Infinity; const a = r.reduce((n, i) => n + i.area, 0), max = r[0].area, min = r[r.length - 1].area; return Math.max(side * side * max / (a * a), a * a / (side * side * min)); };
  const commit = () => { const area = row.reduce((n, i) => n + i.area, 0); if (width >= height) { const w = area / height; let cy = y; for (const item of row) { const h = item.area / w; output.push({ node: item.item, x, y: cy, w, h }); cy += h; } x += w; width = Math.max(0, width - w); } else { const h = area / width; let cx = x; for (const item of row) { const w = item.area / h; output.push({ node: item.item, x: cx, y, w, h }); cx += w; } y += h; height = Math.max(0, height - h); } row = []; };
  while (queue.length) { const item = queue[0], side = Math.min(width, height); if (!row.length || worst([...row, item], side) <= worst(row, side)) { row.push(queue.shift()); } else commit(); } if (row.length) commit(); return output;
}
function renderMap() {
  const el = $('#treemap'), data = S.map; if (!data) return; el.replaceChildren(); const root = data.root;
  if (data.requires_scan || !root || !(allocated(root) > 0) || !root.children?.length) { const note = S.mode === 'quick' ? (S.quickFallback ? (S.previewOnly ? '浅层预览没有完整空间图，可手动开始完整扫描。' : '快速预览已就绪，目录空间图将在后台补全。') : quickEmptyMessage()) : data.requires_scan ? '尚未扫描所选目录，点击“开始完整扫描”建立空间图。' : '所选目录没有可绘制的已分配空间。'; el.innerHTML = `<div class="empty-state"><span class="empty-symbol">▦</span><b>${escapeHtml(note)}</b><span>${S.mode === 'quick' ? (S.previewOnly ? '仅快速预览已开启；不会自动扫描目录。' : '可继续展开目录、在访达定位，或停止后台补全。') : '仅使用真实扫描数据，不推算未知目录的大小。'}</span></div>`; $('#mapSize').textContent = fmt(allocated(root)); $('#mapHover').textContent = S.panelPath; return; }
  const width = el.clientWidth, height = el.clientHeight; if (!width || !height) return; const fragment = document.createDocumentFragment(); let visible = 0;
  const draw = (children, x, y, w, h, depth) => { for (const rect of squarify(children, x, y, w, h)) { const { node } = rect; const rw = Math.max(0, rect.w - 1), rh = Math.max(0, rect.h - 1); if (rw < .8 || rh < .8) continue; const dir = node.kind === 'directory', summary = node.kind === 'summary', key = extKey(extensionOf(node)); const button = document.createElement('button'); button.className = `map-node${dir ? ' directory' : ''}${summary ? ' summary' : ''}`; button.style.cssText = `left:${rect.x}px;top:${rect.y}px;width:${rw}px;height:${rh}px;z-index:${depth + 1};--tile-color:${dir ? '#314253' : colorFor(key)};`; button.dataset.path = node.path || ''; button.dataset.kind = node.kind || 'file'; button.dataset.extension = key; button.dataset.name = node.name || nameOf(node.path); button.dataset.size = String(allocated(node) || 0); button.dataset.reveal = String(!!node.path && node.can_reveal !== false); button.setAttribute('aria-label', `${node.name || nameOf(node.path)}，${fmt(allocated(node))}`); button.title = `${node.path || node.name || '合并项目'}\n磁盘占用 ${fmt(allocated(node))}${summary ? '\n较小项目已合并，不能直接定位' : ''}`;
      if (summary || !node.path) button.tabIndex = -1; if (rw > 43 && rh > 15) { const label = document.createElement('span'); label.textContent = node.name || nameOf(node.path); button.append(label); } if (!dir && rw > 58 && rh > 36) { const size = document.createElement('small'); size.textContent = fmt(allocated(node)); button.append(size); } fragment.append(button); visible++;
      if (dir && node.children?.length && rw > 8 && rh > 22) draw(node.children, rect.x + 2, rect.y + 17, rw - 4, rh - 19, depth + 1);
    } };
  draw(root.children, 0, 0, width, height, 0); el.append(fragment); $('#mapSize').textContent = `${S.mode === 'quick' && !S.quickFallback ? '候选 ' : ''}${fmt(allocated(root))} · ${visible} 个可见区块`; $('#mapHover').textContent = `${S.panelPath}${data.truncated ? ' · 较小项目合并为灰色区块' : ''}`; highlightMap();
}
function highlightMap() { $$('.map-node').forEach(tile => { tile.classList.toggle('selected', !!tile.dataset.path && tile.dataset.path === S.selected?.path); tile.classList.toggle('dimmed', !!S.extension && tile.dataset.kind !== 'directory' && tile.dataset.extension !== S.extension); }); }
async function selectMapPath(tile) {
  const path = tile.dataset.path; if (!path) { toast('较小项目已合并；进入上一级目录可查看更多细节。'); return; }
  let node = S.nodes.get(path);
  if (!node && within(path, S.rootPath)) {
    const version = S.treeVersion; const chain = []; let current = parentOf(path); while (within(current, S.rootPath) && current !== S.rootPath) { chain.unshift(current); const parent = parentOf(current); if (parent === current) break; current = parent; } chain.unshift(S.rootPath);
    for (const parent of chain) { const p = S.nodes.get(parent); if (p?.kind === 'directory') { if (!p._loaded) { try { const d = await api(viewUrl('tree', parent, { limit: 2000 })); if (version !== S.treeVersion) return; mergeChildren(p, d); } catch { break; } } p._expanded = true; } }
    node = S.nodes.get(path);
  }
  if (!node) node = { path, name: tile.dataset.name, kind: tile.dataset.kind, size: Number(tile.dataset.size), can_reveal: tile.dataset.reveal === 'true', _children: [] };
  selectNode(node); const row = $$('#treeRows tr[data-path]').find(r => r.dataset.path === path); row?.scrollIntoView({ block: 'nearest' });
}
async function reveal(path = S.selected?.path) { if (!path) return; $('#revealButton').disabled = true; try { await api('/api/reveal', { method: 'POST', body: JSON.stringify({ path }) }); } catch (error) { toast(`无法在访达中显示：${error.message}`); } finally { controls(); } }
function showErrors(errors) { S.errors = errors || []; $('#errorButton').hidden = !S.errors.length; $('#errorDetails').textContent = S.errors.map(e => typeof e === 'string' ? e : JSON.stringify(e)).join('\n'); }
function readPreferences() {
  try { S.previewOnly = localStorage.getItem('disk-space-preview-only') === 'true'; const history = JSON.parse(localStorage.getItem('disk-space-detail-history') || '{}'); S.detailsHistory = history && typeof history === 'object' && !Array.isArray(history) ? history : {}; } catch { S.detailsHistory = {}; }
}
function rememberDetails(path, phase) { if (!path) return; S.detailsHistory[path] = { phase, at: Date.now() }; try { localStorage.setItem('disk-space-detail-history', JSON.stringify(S.detailsHistory)); } catch {} }
function scanCovers(path, scan = S.lastScan) { return !!scan?.path && within(path, scan.path); }
async function setPreviewOnly(checked) {
  S.previewOnly = checked; try { localStorage.setItem('disk-space-preview-only', String(checked)); } catch {} updateMode();
  if (checked) { S.followDetails = false; if (S.scanRunning && scanCovers(S.rootPath)) await cancelScan(); else toast('仅快速预览已开启；不会自动补全目录。'); }
  else { S.followDetails = true; toast('已允许后台补全；此前停止的范围需手动重新完整扫描。'); await maybeStartDetails(S.rootPath, S.treeVersion); }
}
async function maybeStartDetails(path, version) {
  if (S.mode !== 'quick' || S.previewOnly || S.autoStarting || version !== S.treeVersion) return;
  S.autoStarting = true;
  try {
    const scan = await api('/api/scan/status'); if (version !== S.treeVersion || S.previewOnly || S.mode !== 'quick') return;
    S.lastScan = scan; S.scanRunning = !!scan.running; if (scan.path) S.scanPath = scan.path; controls();
    if (scan.running) { if (!S.scanTimer) S.scanTimer = setInterval(() => pollScan(), 900); await pollScan(true); return; }
    if (scanCovers(path, scan) && scan.finished_at) {
      rememberDetails(path, scan.cancelled ? 'cancelled' : 'reused');
      $('#showDetailsButton').hidden = !scan.cancelled;
      if (!scan.cancelled && S.followDetails) await showFullDetails();
      return;
    }
    if (Object.keys(S.detailsHistory).some(root => within(path, root))) { status(`快速结果已就绪 · 此范围已补全过程或已停止；可手动重新完整扫描。`, 'good'); return; }
    rememberDetails(path, 'started');
    await beginFullScan(path, true);
  } catch (error) { toast(`快速结果可继续浏览；后台详情未启动：${error.message}`); }
  finally { S.autoStarting = false; }
}
async function showFullDetails({ manual = false } = {}) {
  if (S.switchingDetails || !S.rootPath) return; S.switchingDetails = true;
  const path = S.rootPath, selected = S.selected?.path, expanded = [...S.nodes.values()].filter(n => n._expanded).map(n => n.path);
  try {
    const check = await api(`/api/tree?path=${encodeURIComponent(path)}&limit=1`);
    if (path !== S.rootPath || (!manual && (S.previewOnly || !S.followDetails)) || check.requires_scan || !check.source?.startsWith('filesystem-exact')) { $('#showDetailsButton').hidden = false; return; }
    S.mode = 'full'; updateMode(); await changeRoot(path, { autoDetails: false }); const version = S.treeVersion;
    if (selected && within(selected, path)) { let parent = parentOf(selected); while (within(parent, path)) { expanded.push(parent); if (parent === path) break; const above = parentOf(parent); if (above === parent) break; parent = above; } }
    for (const target of [...new Set(expanded)].sort((a, b) => a.length - b.length).slice(0, 60)) {
      if (version !== S.treeVersion) return; const node = S.nodes.get(target); if (!node || node.kind !== 'directory') continue;
      if (!node._loaded) { try { const data = await api(viewUrl('tree', target, { limit: 2000 })); if (version !== S.treeVersion) return; mergeChildren(node, data); } catch { continue; } } node._expanded = true;
    }
    renderTree(); const previous = selected && S.nodes.get(selected); if (previous) { await selectNode(previous, true); $$('#treeRows tr[data-path]').find(row => row.dataset.path === selected)?.scrollIntoView({ block: 'nearest' }); }
    $('#showDetailsButton').hidden = true;
  } catch (error) { toast(`已读详情暂时无法显示：${error.message}`); }
  finally { S.switchingDetails = false; }
}
async function startScan() {
  if (S.mode === 'quick') { await changeRoot(S.rootPath, { refreshQuick: true }); return; }
  if (S.scanRunning || S.scanPending || !S.rootPath) return;
  S.followDetails = true; rememberDetails(S.rootPath, 'manual'); await beginFullScan(S.rootPath, false);
}
async function beginFullScan(path, background) {
  if (S.scanRunning || S.scanPending) return; S.scanPending = true; controls();
  if (!background) status(`准备完整扫描 ${path}…`, 'busy'); else $('#scanProgress').textContent = '快速结果已就绪 · 正在启动后台补全…';
  try {
    const result = await api('/api/scan', { method: 'POST', body: JSON.stringify({ path }) }); S.scanPath = result.path || path; S.lastScan = { ...S.lastScan, path: S.scanPath, running: true }; S.scanRunning = true; S.scanPending = false; controls(); if (!S.scanTimer) S.scanTimer = setInterval(() => pollScan(), 900); await pollScan();
    if (background && S.previewOnly && scanCovers(S.rootPath)) await cancelScan();
  } catch (error) { S.scanPending = false; controls(); if (background) { $('#scanProgress').textContent = '后台补全未启动，可继续浏览快速结果'; toast(error.message); } else status(`扫描无法开始：${error.message}`, 'error'); }
}
async function pollScan(initial = false) {
  if (S.scanPollBusy) return; S.scanPollBusy = true;
  try {
    const data = await api('/api/scan/status'), wasRunning = S.scanRunning; S.lastScan = data; S.scanRunning = !!data.running; if (data.path) S.scanPath = data.path; showErrors(data.errors); controls();
    if (data.running) {
      if (!S.scanTimer) S.scanTimer = setInterval(() => pollScan(), 900);
      if (S.mode === 'full') status(`正在完整扫描 ${data.path || S.scanPath}…`, 'busy');
      $('#scanProgress').textContent = `${S.mode === 'quick' ? '后台补全 · ' : ''}${count(data.files_scanned)} 文件 / ${count(data.dirs_scanned)} 目录${data.dirs_completed != null ? ` · ${count(data.dirs_completed)} 个目录已完成` : ''} · ${fmt(data.allocated_bytes ?? data.bytes_scanned)}`; $('#scanProgress').title = data.current_directory || data.path || '';
    } else {
      clearInterval(S.scanTimer); S.scanTimer = null; $('#scanProgress').textContent = '';
      if (scanCovers(S.rootPath, data)) $('#showDetailsButton').hidden = !data.cancelled || !data.finished_at;
      if (wasRunning && !initial) {
        const message = data.cancelled ? '后台补全已停止，保留快速结果；可手动查看已读详情。' : data.error_count ? `目录详情已就绪 · ${data.error_count} 个读取问题，结果不完整。` : data.complete ? '目录详情已补全。' : '后台读取已结束，结果尚不完整。';
        rememberDetails(data.path, data.cancelled ? 'cancelled' : 'finished');
        if (S.mode === 'full') { status(message, data.error_count ? 'error' : 'good'); await changeRoot(S.rootPath, { autoDetails: false }); }
        else { toast(message); if (!data.cancelled && !S.previewOnly && S.followDetails && scanCovers(S.rootPath, data)) await showFullDetails(); }
      }
    }
  } catch (error) { if (!initial) { if (S.mode === 'full') status(`暂时无法读取扫描状态：${error.message}`, 'error'); else $('#scanProgress').textContent = '后台补全状态暂不可用'; } }
  finally { S.scanPollBusy = false; }
}
async function cancelScan() {
  if (!S.scanRunning) return; S.followDetails = false; rememberDetails(S.scanPath || S.rootPath, 'cancelled'); $('#cancelButton').disabled = true;
  try { await api('/api/scan/cancel', { method: 'POST', body: '{}' }); if (S.mode === 'full') status('已请求停止，等待当前目录结束…', 'busy'); else $('#scanProgress').textContent = '正在停止后台补全…'; }
  catch (error) { toast(`停止失败：${error.message}`); controls(); }
}

function bind() {
  $$('[data-mode]').forEach(button => button.addEventListener('click', () => changeMode(button.dataset.mode))); $('#useFullButton').addEventListener('click', () => changeMode('full')); $('#previewOnly').addEventListener('change', e => setPreviewOnly(e.target.checked)); $('#showDetailsButton').addEventListener('click', () => showFullDetails({ manual: true }));
  $('#rootSelect').addEventListener('change', e => { if (e.target.value === '__custom__') { $('#rootPath').focus(); $('#rootPath').select(); } else changeRoot(e.target.value); });
  $('#openPath').addEventListener('click', () => changeRoot($('#rootPath').value)); $('#rootPath').addEventListener('keydown', e => { if (e.key === 'Enter') changeRoot(e.target.value); }); $('#upButton').addEventListener('click', () => changeRoot(parentOf(S.rootPath))); $('#refreshButton').addEventListener('click', async () => { try { await loadVolumes(); await changeRoot(S.rootPath); } catch (error) { toast(error.message); } }); $('#scanButton').addEventListener('click', startScan); $('#cancelButton').addEventListener('click', cancelScan); $('#revealButton').addEventListener('click', () => reveal());
  $('#treeFilter').addEventListener('input', e => { S.filter = e.target.value.trim().toLowerCase(); renderTree(); });
  $$('[data-sort]').forEach(button => button.addEventListener('click', () => { if (S.sort === button.dataset.sort) S.direction *= -1; else { S.sort = button.dataset.sort; S.direction = S.sort === 'name' ? 1 : -1; } $$('[data-sort] span').forEach(span => span.textContent = ''); $('span', button).textContent = S.direction < 0 ? '↓' : '↑'; renderTree(); }));
  $('#treeRows').addEventListener('click', e => { const toggle = e.target.closest('[data-toggle]'); if (toggle) { expandNode(S.nodes.get(toggle.dataset.toggle)); return; } const row = e.target.closest('tr[data-path]'); if (row) selectNode(S.nodes.get(row.dataset.path)); });
  $('#treeRows').addEventListener('dblclick', e => { if (e.target.closest('[data-toggle]')) return; const row = e.target.closest('tr[data-path]'), node = row && S.nodes.get(row.dataset.path); if (!node) return; if (node.kind === 'directory') expandNode(node); else reveal(node.path); });
  $('#treeRows').addEventListener('keydown', e => { const row = e.target.closest('tr[data-path]'), node = row && S.nodes.get(row.dataset.path); if (!node) return; if (e.key === 'Enter') { e.preventDefault(); selectNode(node); } if (e.key === 'ArrowRight' && node.kind === 'directory' && !node._expanded) { e.preventDefault(); expandNode(node); } if (e.key === 'ArrowLeft' && node._expanded) { e.preventDefault(); expandNode(node); } });
  $('#extensionRows').addEventListener('click', e => { const row = e.target.closest('[data-extension]'); if (row) setExtension(row.dataset.extension); }); $('#extensionRows').addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); const row = e.target.closest('[data-extension]'); if (row) setExtension(row.dataset.extension); } }); $('#clearExtension').addEventListener('click', () => setExtension(S.extension));
  $('#mapDepth').addEventListener('change', () => { if (S.panelPath) loadPanels(S.panelPath); });
  $('#treemap').addEventListener('pointerover', e => { const tile = e.target.closest('.map-node'); if (!tile) return; $('#mapHover').textContent = tile.dataset.path || `${tile.dataset.name}（合并项目）`; $('#mapSize').textContent = fmt(Number(tile.dataset.size)); });
  $('#treemap').addEventListener('pointerleave', () => { $('#mapHover').textContent = S.panelPath || '选择目录查看空间分布'; $('#mapSize').textContent = fmt(allocated(S.map?.root)); });
  $('#treemap').addEventListener('click', e => { const tile = e.target.closest('.map-node'); if (!tile) return; clearTimeout(S.mapClick); S.mapClick = setTimeout(() => selectMapPath(tile), 210); });
  $('#treemap').addEventListener('dblclick', e => { clearTimeout(S.mapClick); const tile = e.target.closest('.map-node'); if (tile?.dataset.path && tile.dataset.reveal === 'true') reveal(tile.dataset.path); else toast('合并项目无法直接定位，请进入上一级目录。'); });
  $('#errorButton').addEventListener('click', () => $('#errorDialog').showModal()); $('#closeErrors').addEventListener('click', () => $('#errorDialog').close());
  document.addEventListener('keydown', e => { if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'f') { e.preventDefault(); $('#treeFilter').focus(); } });
  let resizeTimer; new ResizeObserver(() => { clearTimeout(resizeTimer); resizeTimer = setTimeout(renderMap, 80); }).observe($('#treemap'));
}
bind(); boot();
