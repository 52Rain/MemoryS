<script>
 import { onMount, tick } from 'svelte';
 import {
  Library,
  Layers3,
  GitPullRequest,
  SlidersHorizontal,
  FolderOpen,
  Search,
  ChevronDown,
  ChevronRight,
  Check,
  RefreshCw,
  FileText,
  Sparkles,
  GitCommitHorizontal,
  CalendarDays,
  Quote,
  X,
  CheckCheck,
  Plug,
  KeyRound,
  Eye,
  EyeOff,
  Pencil,
  ArrowLeft,
  Plus,
  Archive,
  AlertCircle,
  BookOpen,
  ListFilter,
  ExternalLink,
  Tag,
  Trash2,
  Languages,
 } from 'lucide-svelte';
 import { DICT } from './i18n.js';

 function readWebMeta() {
  const el = document.querySelector('meta[name="memorys-web"]');
  if (!el) return null;
  try {
   return JSON.parse(el.getAttribute('content') || '');
  } catch {
   return null;
  }
 }

 const nativeBridge = window.webkit?.messageHandlers?.memorys;
 const webMeta = readWebMeta();
 const isNative = !!nativeBridge;
 const route = location.hash.slice(1);
 const todayIso = () => new Date().toISOString().slice(0, 10);

 function initialLang() {
  try {
   const saved = localStorage.getItem('memorys.lang');
   if (saved === 'en' || saved === 'zh') return saved;
  } catch {}
  return navigator.language?.toLowerCase().startsWith('zh') ? 'zh' : 'en';
 }

 let lang = $state(initialLang());
 const t = $derived(DICT[lang] || DICT.en);

 const pageIcons = {
  library: Library,
  review: GitPullRequest,
  settings: SlidersHorizontal,
 };
 const pageIds = ['library', 'review', 'settings'];

 let page = $state(pageIds.includes(route) ? route : 'library');
 let data = $state({
  vault_path: '',
  cards: [],
  projects: [],
  proposals: [],
  stats: { total: 0, active: 0, history: 0, projects: 0 },
  integrations: {
   claude: { mcp: false, hook: false },
   codex: { mcp: false, hook: false },
   cursor: { mcp: false, hook: false },
  },
  config: { provider: 'OpenAI Compatible', url: 'https://api.openai.com/v1', model: 'gpt-4o-mini' },
  hasKey: false,
  runtime_mode: isNative ? 'native' : 'web',
 });
 let ready = $state(false);
 let search = $state('');
 let scopeFilter = $state('');
 let includeHistory = $state(false);
 let cardId = $state('');
 let proposalId = $state('');
 let selected = $state([]);
 let modal = $state('');
 let busy = $state('');
 let notice = $state('');
 let error = $state('');
 let mobileDetail = $state(false);

 // Settings form
 let provider = $state('');
 let url = $state('');
 let model = $state('');
 let key = $state('');
 let showKey = $state(false);
 let feedback = $state('');
 let savedUrl = $state('');
 let dirty = $state(false);

 // Project aliases/paths editor
 let activeProjId = $state('');
 let projAliases = $state('');
 let projPaths = $state([]);
 let newPathInput = $state('');

 // Create / Edit proposal form
 let formMode = $state('create');
 let formProposalId = $state('');
 let formScopeType = $state('shared/preferences');
 let formExistingProject = $state('');
 let formNewProject = $state('');
 let formTitle = $state('');
 let formBody = $state('');
 let formEffectiveFrom = $state(todayIso());
 let formTarget = $state('');
 let formUseWhen = $state('');
 let formQuote = $state('');

 let searchInput = $state();
 let dialog = $state();
 let returnFocus;
 let lock = false;

 const cards = $derived(
  data.cards.filter((c) => {
   if (!includeHistory && c.status !== 'active') return false;
   if (scopeFilter && c.scope !== scopeFilter) return false;
   if (!search.trim()) return true;
   const q = search.trim().toLowerCase();
   return [c.title, c.body, c.project, c.scope_label, c.source, ...(c.use_when || [])]
    .join(' ')
    .toLowerCase()
    .includes(q);
  })
 );
 const card = $derived(cards.find((c) => c.id === cardId) || cards[0]);

 const proposals = $derived(data.proposals);
 const proposal = $derived(proposals.find((p) => p.id === proposalId) || proposals[0]);
 const chosen = $derived(data.proposals.filter((p) => selected.includes(p.id)));
 const sections = $derived((card?.body || '').split(/\n\s*\n/).filter(Boolean));

 const resolvedFormScope = $derived.by(() => {
  if (formScopeType === 'existing_project') {
   return formExistingProject || (data.projects[0]?.scope || '');
  }
  if (formScopeType === 'new_project') {
   return formNewProject.trim() ? `project/new:${formNewProject.trim()}` : '';
  }
  return formScopeType;
 });

 const targetCandidates = $derived(
  data.cards.filter((c) => c.status === 'active' && c.scope === resolvedFormScope)
 );

 const cleanText = (s) =>
  s.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1').replace(/\*\*/g, '').replace(/^[*-] /gm, '• ');

 function kindText(k) {
  return t.kind[k] || k;
 }

 function statusText(s) {
  return t.status[s] || s;
 }

 function scopeText(scope, fallback) {
  if (scope === 'shared/preferences') return t.scopePreferences;
  if (scope === 'shared/methods') return t.scopeMethods;
  if (scope === 'shared/domain') return t.scopeDomain;
  return fallback || scope;
 }

 function toggleLang() {
  lang = lang === 'en' ? 'zh' : 'en';
  try {
   localStorage.setItem('memorys.lang', lang);
  } catch {}
 }

 async function rpc(action, args = {}) {
  if (nativeBridge) {
   return await nativeBridge.postMessage({ action, args });
  }
  if (webMeta?.token) {
   const res = await fetch('/api/rpc', {
    method: 'POST',
    headers: {
     'Content-Type': 'application/json',
     'X-MemoryS-Token': webMeta.token,
    },
    body: JSON.stringify({ action, args }),
   });
   const payload = await res.json();
   if (!payload.ok) throw Error(payload.error || 'Local RPC error');
   return payload.data;
  }
  throw Error(t.errors.noBridge);
 }

 function configForm() {
  provider = data.config.provider;
  url = data.config.url;
  model = data.config.model;
  savedUrl = url;
  key = '';
  showKey = false;
  dirty = false;
 }

 function syncProjectEditor(pid = activeProjId) {
  const list = data.projects || [];
  const p = list.find((item) => item.project_id === pid) || list[0];
  if (!p) {
   activeProjId = '';
   projAliases = '';
   projPaths = [];
   return;
  }
  activeProjId = p.project_id;
  projAliases = (p.aliases || []).join(', ');
  projPaths = [...(p.paths || [])];
  newPathInput = '';
 }

 async function refresh(initial = false) {
  data = await rpc('snapshot');
  selected = selected.filter((id) => data.proposals.some((p) => p.id === id));
  if (initial || !dirty) configForm();
  syncProjectEditor(initial ? '' : activeProjId);
  ready = true;
 }

 async function run(action, fn) {
  if (lock) return;
  lock = true;
  busy = action;
  notice = '';
  error = '';
  try {
   await fn();
  } catch (e) {
   error = e.message || String(e);
  } finally {
   busy = '';
   lock = false;
  }
 }

 function go(id) {
  page = id;
  location.hash = id;
  mobileDetail = false;
  notice = '';
  error = '';
  feedback = '';
 }

 function toggle(id) {
  selected = selected.includes(id)
   ? selected.filter((x) => x !== id)
   : [...selected, id];
 }

 function initProposalForm(opts = {}) {
  const scope = opts.scope || 'shared/preferences';
  if (scope.startsWith('project/new:')) {
   formScopeType = 'new_project';
   formNewProject = scope.slice('project/new:'.length);
   formExistingProject = data.projects[0]?.scope || '';
  } else if (scope.startsWith('project/')) {
   formScopeType = 'existing_project';
   formExistingProject = scope;
   formNewProject = '';
  } else {
   formScopeType = scope;
   formExistingProject = data.projects[0]?.scope || '';
   formNewProject = '';
  }
  formTitle = opts.title || '';
  formBody = opts.body || '';
  formEffectiveFrom = opts.effective_from || todayIso();
  formTarget = opts.target || '';
  formUseWhen = Array.isArray(opts.use_when) ? opts.use_when.join(', ') : (opts.use_when || '');
  formQuote = opts.quote || '';
 }

 async function openModal(name, opts = {}) {
  returnFocus = document.activeElement;
  if (name === 'create') {
   formMode = 'create';
   formProposalId = '';
   initProposalForm(opts);
   modal = 'proposalForm';
  } else if (name === 'revise' && card) {
   formMode = 'create';
   formProposalId = '';
   initProposalForm({
    scope: card.scope,
    title: card.title,
    body: card.body,
    effective_from: todayIso(),
    target: card.id,
    use_when: card.use_when || [],
    quote: '',
   });
   modal = 'proposalForm';
  } else if (name === 'edit' && proposal) {
   formMode = 'edit';
   formProposalId = proposal.id;
   initProposalForm({
    scope: proposal.scope,
    title: proposal.title,
    body: proposal.body,
    effective_from: proposal.effective_from,
    target: proposal.target,
    use_when: proposal.use_when || [],
    quote: proposal.quote || '',
   });
   modal = 'proposalForm';
  } else {
   modal = name;
  }
  await tick();
  dialog?.showModal();
  dialog?.querySelector('input, textarea, select, button')?.focus();
 }

 function close() {
  dialog?.close();
  modal = '';
  tick().then(() => returnFocus?.focus());
 }

 function changed() {
  dirty = true;
  feedback = '';
 }

 async function settings(action) {
  await run(action, async () => {
   await rpc(action, { provider, url, model, key });
   if (action === 'saveConfig') {
    dirty = false;
    await refresh(true);
    feedback = t.settingsSaved;
   } else {
    feedback = t.connectionOk;
   }
  });
 }

 async function saveProposalForm() {
  await run('saveProposal', async () => {
   const scope = resolvedFormScope;
   if (!scope) throw Error(t.errors.needScope);
   const payload = {
    scope,
    title: formTitle,
    body: formBody,
    effective_from: formEffectiveFrom,
    target: formTarget,
    use_when: formUseWhen,
    quote: formQuote,
   };
   if (formMode === 'edit') {
    await rpc('editProposal', { id: formProposalId, ...payload });
    notice = t.notices.updated;
   } else {
    const created = await rpc('createProposal', {
     ...payload,
     client: isNative ? 'MemoryS.app' : 'MemoryS Web',
    });
    proposalId = created.id;
    page = 'review';
    location.hash = 'review';
    notice = t.notices.queued;
   }
   close();
   await refresh();
  });
 }

 async function execute(name) {
  await run(name, async () => {
   if (name === 'review') {
    await rpc('reviewProposal', { id: proposal.id, lang });
    notice = t.notices.aiDone;
   }
   if (name === 'ignore') {
    await rpc('ignoreProposal', { id: proposal.id });
    notice = t.notices.ignored;
   }
   if (name === 'publish') {
    const r = await rpc('publish', { ids: chosen.map((x) => x.id) });
    notice = r.cancelled ? t.notices.cancelled : t.notices.published(r.count, r.revision);
    if (!r.cancelled) selected = [];
   }
   if (name === 'archive' && card) {
    const r = await rpc('archiveCard', { id: card.id });
    notice = t.notices.archived(card.title, r.revision);
   }
   close();
   await refresh();
  });
 }

 async function openCardExternal(mode) {
  if (!card?.abs_path) return;
  await run('open', async () => {
   await rpc('openExternal', { path: card.abs_path, mode });
  });
 }

 async function addProjectPathViaPicker() {
  await run('pickDir', async () => {
   const res = await rpc('pickDirectory');
   if (!res?.cancelled && res?.path && !projPaths.includes(res.path)) {
    projPaths = [...projPaths, res.path];
   }
  });
 }

 async function switchVaultViaPicker() {
  await run('switchVault', async () => {
   const res = await rpc('pickDirectory', { prompt: 'Select MemoryS Vault' });
   if (!res?.cancelled && res?.path) {
    const r = await rpc('setVaultPath', { path: res.path });
    await refresh(true);
    notice = t.notices.vaultSwitched(r.vault_path);
   }
  });
 }

 async function saveProjectConfig() {
  if (!activeProjId) return;
  await run('saveProject', async () => {
   const r = await rpc('saveProjectMeta', {
    project_id: activeProjId,
    aliases: projAliases,
    paths: projPaths,
   });
   await refresh();
   notice = t.notices.projectSaved(r.title, r.revision);
  });
 }

 function jumpToCard(id) {
  includeHistory = true;
  scopeFilter = '';
  search = '';
  cardId = id;
  mobileDetail = true;
 }

 function shortcuts(e) {
  if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
   e.preventDefault();
   go('library');
   tick().then(() => searchInput?.focus());
  }
 }

 onMount(() => {
  run('load', async () => {
   await refresh(true);
  });
 });
</script>

<svelte:window onkeydown={shortcuts} />

<div class="shell">
 <aside class="rail">
  <div class="brand">
   <svg viewBox="0 0 36 36" width="32" height="32" aria-hidden="true">
    <path fill="#29584b" d="M3 9 14 5v23L3 32zm13-4 8 4v22l-8-4zm10 3 7-3v23l-7 4z" />
   </svg>
   <strong>MemoryS</strong>
  </div>
  <nav aria-label="Main Navigation">
   {#each pageIds as pid}
    {@const Icon = pageIcons[pid]}
    <button
     class:active={page === pid}
     aria-current={page === pid ? 'page' : undefined}
     onclick={() => go(pid)}
    >
     <Icon size={19} />
     <span>{t.nav[pid]}</span>
     {#if pid === 'review' && data.proposals.length}
      <small>{data.proposals.length}</small>
     {/if}
    </button>
   {/each}
  </nav>
  <div class="vault-badge">
   <small>{t.brandSubtitle}</small>
   <span>{t.activeSummary(data.stats.active, data.stats.projects)}</span>
  </div>
 </aside>

 <main>
  <header class="top">
   <span class="crumb">
    <Layers3 size={16} />{t.workspaceCrumb} <ChevronRight size={14} />
    <b>{t.nav[page]}</b>
   </span>
   <div class="top-actions">
    <button class="subtle" onclick={toggleLang} title="Switch Language / 切换语言">
     <Languages size={15} />{lang === 'en' ? '中文' : 'EN'}
    </button>
    <button
     class="subtle"
     disabled={!!busy}
     onclick={() => run('refresh', async () => {
      await refresh();
      notice = t.refreshed;
     })}
    >
     <RefreshCw size={15} class={busy === 'refresh' ? 'spin' : ''} />{t.refresh}
    </button>
   </div>
  </header>

  <section class="surface">
   <div class="heading">
    <div>
     <h1>{t.nav[page]}</h1>
     <p>{t.desc[page]}</p>
    </div>
    {#if page === 'library'}
     <div class="heading-actions">
      <span class="count"><BookOpen size={15} />{t.countBadge(data.stats.active, data.stats.total)}</span>
      <button class="primary" disabled={!!busy} onclick={() => openModal('create')}>
       <Plus size={16} />{t.newMemory}
      </button>
     </div>
    {:else if page === 'review'}
     <button class="outline" disabled={!!busy} onclick={() => openModal('create')}>
      <Plus size={16} />{t.newDraft}
     </button>
    {/if}
   </div>

   {#if error}
    <div class="feedback error" role="alert">
     <AlertCircle size={16} />
     <span>{error}</span>
     <button aria-label="Close" onclick={() => (error = '')}><X size={14} /></button>
    </div>
   {:else if notice}
    <div class="feedback" role="status"><Check size={16} /><span>{notice}</span></div>
   {/if}

   {#if page === 'library'}
    <div class="toolbar">
     <label class="search">
      <Search size={17} />
      <input
       aria-label={t.searchPlaceholder}
       placeholder={t.searchPlaceholder}
       bind:this={searchInput}
       bind:value={search}
      />
      <kbd>⌘ K</kbd>
     </label>
     <label class="filter">
      <ListFilter size={15} />
      <select aria-label={t.allScopes} bind:value={scopeFilter}>
       <option value="">{t.allScopes}</option>
       <option value="shared/preferences">{t.scopePreferences}</option>
       <option value="shared/methods">{t.scopeMethods}</option>
       <option value="shared/domain">{t.scopeDomain}</option>
       {#each data.projects as p}
        <option value={p.scope}>{t.scopeProjectPrefix}{p.title}</option>
       {/each}
      </select>
      <ChevronDown size={14} />
     </label>
     <label class="history-toggle">
      <input type="checkbox" bind:checked={includeHistory} />
      <span>{t.includeHistory}</span>
     </label>
    </div>

    <div class="workspace" class:detailOpen={mobileDetail}>
     <div class="index">
      <div class="list-heading">{t.entriesHeading} <span>{cards.length}</span></div>
      {#each cards as c}
       <button
        class="entry"
        class:current={card?.id === c.id}
        class:muted-entry={c.status !== 'active'}
        onclick={() => {
         cardId = c.id;
         mobileDetail = true;
        }}
       >
        <span class="entry-meta">
         <FileText size={14} />{scopeText(c.scope, c.project)}
         {#if c.status !== 'active'}
          <span class="pill status-{c.status}">{statusText(c.status)}</span>
         {/if}
        </span>
        <b>{c.title}</b>
        <small>{kindText(c.kind)} <i></i> {c.effective_from || '—'}</small>
       </button>
      {:else}
       <p class="inline-empty">
        {ready ? t.noMatchingCards : t.loadingVault}
       </p>
      {/each}
     </div>

     <article class="reading">
      <button class="back" onclick={() => (mobileDetail = false)}>
       <ArrowLeft size={16} />{t.backToList}
      </button>
      {#if card}
       <div class="doc-meta">
        <span class="type"><FileText size={14} />{kindText(card.kind)}</span>
        <span>{scopeText(card.scope, card.scope_label)}</span>
        <span class="pill status-{card.status}">{statusText(card.status)}</span>
       </div>
       <h2>{card.title}</h2>
       <div class="properties">
        <span>
         <CalendarDays size={15} />
         {t.effectiveRange(card.effective_from, card.effective_until)}
        </span>
        <span><GitCommitHorizontal size={15} />{card.version}</span>
       </div>

       {#if card.use_when?.length}
        <div class="tags-row">
         <Tag size={14} />
         {#each card.use_when as kw}
          <span class="tag-chip">{kw}</span>
         {/each}
        </div>
       {/if}

       <div class="prose">
        {#each sections as s}
         {#if s.startsWith('#')}
          <h3>{cleanText(s.replace(/^#+\s*/, ''))}</h3>
         {:else}
          <p>{cleanText(s)}</p>
         {/if}
        {/each}
       </div>

       {#if card.supersedes?.length || card.superseded_by}
        <div class="chain-note">
         {#if card.supersedes?.length}
          <div>
           <b>{t.supersedes}</b>
           {#each card.supersedes as prev}
            <button class="link-btn" onclick={() => jumpToCard(prev)}>{prev}</button>
           {/each}
          </div>
         {/if}
         {#if card.superseded_by}
          <div>
           <b>{t.supersededBy}</b>
           <button class="link-btn" onclick={() => jumpToCard(card.superseded_by)}>
            {card.superseded_by}
           </button>
          </div>
         {/if}
        </div>
       {/if}

       <div class="source-note">
        <div><Quote size={16} /><b>{card.quote ? t.sourceQuote : t.cardLocation}</b></div>
        {#if card.quote}<p>{card.quote}</p>{/if}
        <small>{card.source} · ID: {card.id}</small>
       </div>

       <div class="actions">
        <button class="primary" disabled={!!busy} onclick={() => openModal('revise')}>
         <Pencil size={15} />{t.revise}
        </button>
        {#if card.status === 'active'}
         <button class="outline danger-outline" disabled={!!busy} onclick={() => openModal('archive')}>
          <Archive size={15} />{t.archive}
         </button>
        {/if}
        <button class="outline" disabled={!!busy} onclick={() => openCardExternal('obsidian')}>
         <ExternalLink size={15} />{t.openObsidian}
        </button>
        <button class="subtle" disabled={!!busy} onclick={() => openCardExternal('finder')}>
         <FolderOpen size={15} />{t.revealFile}
        </button>
       </div>
      {:else}
       <div class="empty">
        <Search size={26} />
        <h3>{ready ? t.emptyLibraryTitle : t.emptyLibraryLoading}</h3>
        {#if search || scopeFilter || !includeHistory}
         <button
          class="outline"
          onclick={() => {
           search = '';
           scopeFilter = '';
           includeHistory = true;
          }}
         >
          {t.resetFilters}
         </button>
        {/if}
       </div>
      {/if}
     </article>
    </div>

   {:else if page === 'review'}
    <div class="toolbar review-tools">
     <div class="queue-hint">{t.queueHint(proposals.length)}</div>
     <button class="primary" disabled={!chosen.length || !!busy} onclick={() => openModal('publish')}>
      <CheckCheck size={16} />{t.publishBtn(chosen.length)}
     </button>
    </div>

    <div class="workspace" class:detailOpen={mobileDetail}>
     <div class="index">
      <div class="list-heading">
       <label>
        <input
         type="checkbox"
         aria-label={t.pendingList}
         disabled={!proposals.length}
         checked={proposals.length > 0 && proposals.every((p) => selected.includes(p.id))}
         onchange={(e) =>
          (selected = e.currentTarget.checked ? proposals.map((p) => p.id) : [])}
        />
        {t.pendingList}
       </label>
       <span>{proposals.length}</span>
      </div>
      {#each proposals as p}
       <div class="check-row" class:current={proposal?.id === p.id}>
        <input
         aria-label={'Select ' + p.title}
         type="checkbox"
         checked={selected.includes(p.id)}
         onchange={() => toggle(p.id)}
        />
        <button
         class="entry"
         onclick={() => {
          proposalId = p.id;
          mobileDetail = true;
         }}
        >
         <span class="entry-meta">{scopeText(p.scope, p.scope_label)} · {p.client || 'Manual'}</span>
         <b>{p.title}</b>
         <small>
          {statusText(p.state)} · {p.effective_from}
          {#if p.target}<i></i>{t.revisionTag}{/if}
          {#if p.opinion}<i></i>{t.checkedTag}{/if}
         </small>
        </button>
       </div>
      {:else}
       <p class="inline-empty">{t.noPendingInline}</p>
      {/each}
     </div>

     <article class="reading">
      <button class="back" onclick={() => (mobileDetail = false)}>
       <ArrowLeft size={16} />{t.backToList}
      </button>
      {#if proposal}
       <div class="doc-meta">
        <span class="type">{statusText(proposal.state)}</span>
        <span>{scopeText(proposal.scope, proposal.scope_label)}</span>
        <span>{t.effectiveDate} {proposal.effective_from}</span>
       </div>
       <h2>{proposal.title}</h2>
       <div class="review-section">
        <h3>{t.beforeHeading}</h3>
        <p>{proposal.before || t.beforeEmpty}</p>
       </div>
       <div class="review-section after">
        <h3>{t.afterHeading}</h3>
        <p>{proposal.body}</p>
       </div>
       <div class="review-section">
        <h3>{t.scopeTriggersHeading}</h3>
        <p>
         {scopeText(proposal.scope, proposal.scope_label)} ({proposal.scope}) ·
         {proposal.target ? t.supersedesTag(proposal.target) : t.newEntryTag}
        </p>
        {#if proposal.use_when?.length}
         <div class="tags-row">
          {#each proposal.use_when as kw}
           <span class="tag-chip">{kw}</span>
          {/each}
         </div>
        {/if}
       </div>
       <div class="review-section">
        <h3>{t.aiCheckHeading}</h3>
        <p>{proposal.opinion || t.aiCheckEmpty}</p>
        {#if proposal.reviewer}<small>{t.aiReviewer(proposal.reviewer)}</small>{/if}
       </div>
       <div class="source-note">
        <div><Quote size={15} /><b>{t.sourceNoteHeading}</b></div>
        <p>{proposal.quote || t.noQuote}</p>
        <small>{t.submittedMeta(proposal.client, proposal.date)}</small>
       </div>
       <div class="actions">
        <button class="outline" disabled={!!busy} onclick={() => openModal('review')}>
         <Sparkles size={15} />{t.aiCheckBtn}
        </button>
        <button class="outline" disabled={!!busy} onclick={() => openModal('edit')}>
         <Pencil size={15} />{t.editBtn}
        </button>
        <button class="subtle" disabled={!!busy} onclick={() => openModal('ignore')}>
         {t.ignoreBtn}
        </button>
       </div>
      {:else}
       <div class="empty">
        <GitPullRequest size={28} />
        <h3>{t.noPendingTitle}</h3>
        <p>{t.noPendingDesc}</p>
        <button class="primary" onclick={() => openModal('create')}>
         <Plus size={16} />{t.createDraftBtn}
        </button>
       </div>
      {/if}
     </article>
    </div>

   {:else}
    <div class="settings-stack">
     <section class="settings">
      <div class="form-title"><Plug size={20} /><h2>{t.integrationsHeading}</h2></div>
      <p class="section-desc">
       {t.vaultPathLabel} <code>{data.vault_path}</code> ({t.vaultStatsInline(data.stats.active, data.stats.history)})
       {#if isNative}
        <button class="link-btn" type="button" disabled={!!busy} onclick={switchVaultViaPicker}>
         {t.switchVault}
        </button>
       {/if}
      </p>
      <div class="status-grid">
       <div class="status-card">
        <b>Claude Code</b>
        <span>MCP (memorys): {data.integrations.claude?.mcp ? t.ready : t.notConfigured}</span>
        <span>UserPromptSubmit Hook: {data.integrations.claude?.hook ? t.ready : t.notConfigured}</span>
       </div>
       <div class="status-card">
        <b>Codex</b>
        <span>MCP (memorys): {data.integrations.codex?.mcp ? t.ready : t.notConfigured}</span>
        <span>UserPromptSubmit Hook: {data.integrations.codex?.hook ? t.ready : t.notConfigured}</span>
       </div>
       <div class="status-card">
        <b>Cursor</b>
        <span>MCP (memorys): {data.integrations.cursor?.mcp ? t.ready : t.notConfigured}</span>
        <span>Hook: N/A</span>
       </div>
      </div>
      <p class="section-desc" style="margin-top:12px;margin-bottom:0">{t.connectTip}</p>
     </section>

     <section class="settings">
      <div class="form-title"><FolderOpen size={20} /><h2>{t.projectsHeading}</h2></div>
      <p class="section-desc">{t.projectsDesc}</p>
      {#if data.projects.length}
       <label>
        {t.selectProject}
        <select value={activeProjId} onchange={(e) => syncProjectEditor(e.currentTarget.value)}>
         {#each data.projects as p}
          <option value={p.project_id}>{p.title} ({p.project_id})</option>
         {/each}
        </select>
       </label>
       <label>
        {t.aliasesLabel}
        <input placeholder={t.aliasesPlaceholder} bind:value={projAliases} />
       </label>
       <div class="paths-block">
        <span class="field-label">{t.pathsLabel}</span>
        {#each projPaths as p}
         <div class="path-row">
          <code>{p}</code>
          <button
           type="button"
           class="subtle"
           onclick={() => (projPaths = projPaths.filter((x) => x !== p))}
          >
           <Trash2 size={14} />
          </button>
         </div>
        {:else}
         <small>{t.noPathsHint}</small>
        {/each}
        <div class="path-add">
         <input placeholder={t.pathInputPlaceholder} bind:value={newPathInput} />
         <button
          type="button"
          class="outline"
          onclick={() => {
           const val = newPathInput.trim();
           if (val && !projPaths.includes(val)) {
            projPaths = [...projPaths, val];
            newPathInput = '';
           }
          }}
         >
          {t.addPath}
         </button>
         {#if isNative}
          <button type="button" class="outline" disabled={!!busy} onclick={addProjectPathViaPicker}>
           <FolderOpen size={15} />{t.browseDir}
          </button>
         {/if}
        </div>
       </div>
       <div class="actions">
        <button class="primary" type="button" disabled={!!busy} onclick={saveProjectConfig}>
         {t.saveProjectBtn}
        </button>
       </div>
      {:else}
       <p class="inline-empty">{t.noProjectsEmpty}</p>
      {/if}
     </section>

     <form class="settings" onsubmit={(e) => { e.preventDefault(); settings('saveConfig'); }}>
      <div class="form-title"><Sparkles size={20} /><h2>{t.modelHeading}</h2></div>
      <label>{t.providerLabel}<input required maxlength="100" bind:value={provider} oninput={changed} /></label>
      <label>
       {t.apiUrlLabel}
       <input required type="url" placeholder={t.apiUrlPlaceholder} bind:value={url} oninput={changed} />
       <small>{t.apiUrlHint}</small>
      </label>
      <label>{t.modelIdLabel}<input required bind:value={model} oninput={changed} /></label>
      <label>
       {t.apiKeyLabel}
       <span class="secret">
        <input
         type={showKey ? 'text' : 'password'}
         placeholder={data.hasKey && url === savedUrl ? t.keySavedPlaceholder : t.keyInputPlaceholder}
         bind:value={key}
         oninput={changed}
         autocomplete="off"
        />
        <button
         type="button"
         aria-label={showKey ? 'Hide key' : 'Show key'}
         onclick={() => (showKey = !showKey)}
        >
         {#if showKey}<EyeOff size={17} />{:else}<Eye size={17} />{/if}
        </button>
       </span>
       <small><KeyRound size={13} />{isNative ? t.keyStorageNative : t.keyStorageWeb}</small>
      </label>
      <div class="actions">
       <button class="primary" type="submit" disabled={!!busy}>
        {busy === 'saveConfig' ? t.saving : t.saveSettings}
       </button>
       <button class="outline" type="button" disabled={!!busy} onclick={() => settings('test')}>
        <Plug size={15} />{busy === 'test' ? t.testing : t.testConnection}
       </button>
      </div>
      {#if feedback}<p class="form-status" role="status"><Check size={16} />{feedback}</p>{/if}
     </form>
    </div>
   {/if}
  </section>
 </main>
</div>

{#if modal}
 <dialog bind:this={dialog} oncancel={(e) => { if (busy) e.preventDefault(); else close(); }}>
  <div class="dialog-head">
   <h2>
    {modal === 'proposalForm'
     ? formMode === 'edit'
       ? t.modalTitle.edit
       : t.modalTitle.create
     : t.modalTitle[modal]}
   </h2>
   <button aria-label="Close" disabled={!!busy} onclick={close}><X size={18} /></button>
  </div>

  {#if modal === 'proposalForm'}
   <label>
    {t.formScope}
    <select bind:value={formScopeType} onchange={() => (formTarget = '')}>
     <option value="shared/preferences">{t.scopeOptPref}</option>
     <option value="existing_project">{t.scopeOptExisting}</option>
     <option value="new_project">{t.scopeOptNew}</option>
     <option value="shared/methods">{t.scopeOptMethod}</option>
     <option value="shared/domain">{t.scopeOptDomain}</option>
    </select>
   </label>

   {#if formScopeType === 'existing_project'}
    <label>
     {t.selectProject}
     <select bind:value={formExistingProject} onchange={() => (formTarget = '')}>
      {#each data.projects as p}
       <option value={p.scope}>{p.title} ({p.project_id})</option>
      {/each}
     </select>
    </label>
   {:else if formScopeType === 'new_project'}
    <label>
     {t.newProjectName}
     <input placeholder={t.newProjectPlaceholder} bind:value={formNewProject} />
    </label>
   {/if}

   <label>
    {t.titleLabel}
    <input placeholder={t.titlePlaceholder} maxlength="120" bind:value={formTitle} />
   </label>

   <label>
    {t.bodyLabel}
    <textarea rows="5" placeholder={t.bodyPlaceholder} maxlength="8000" bind:value={formBody}></textarea>
   </label>

   <div class="form-row-2">
    <label>
     {t.effectiveFromLabel}
     <input type="date" bind:value={formEffectiveFrom} />
    </label>
    <label>
     {t.supersedesSelectLabel}
     <select bind:value={formTarget}>
      <option value="">{t.noSupersede}</option>
      {#each targetCandidates as c}
       <option value={c.id}>{c.title} ({c.effective_from})</option>
      {/each}
     </select>
    </label>
   </div>

   <label>
    {t.triggersLabel}
    <input placeholder={t.triggersPlaceholder} bind:value={formUseWhen} />
   </label>

   <label>
    {t.quoteLabel}
    <textarea rows="2" placeholder={t.quotePlaceholder} bind:value={formQuote}></textarea>
   </label>

   <div class="actions">
    <button
     class="primary"
     disabled={!!busy || !formTitle.trim() || !formBody.trim() || !resolvedFormScope}
     onclick={saveProposalForm}
    >
     {formMode === 'edit' ? t.saveChanges : t.queueProposal}
    </button>
    <button class="subtle" disabled={!!busy} onclick={close}>{t.cancel}</button>
   </div>

  {:else if modal === 'review' && proposal}
   <p>{t.aiReviewConfirm(data.config.provider, data.config.model)}</p>
   <small>{data.config.url}</small>
   <blockquote>{proposal.body}</blockquote>
   <div class="actions">
    <button class="primary" disabled={!!busy} onclick={() => execute('review')}>
     {busy ? t.checking : t.runAiCheck}
    </button>
    <button class="subtle" disabled={!!busy} onclick={close}>{t.cancel}</button>
   </div>

  {:else if modal === 'publish'}
   <p>{t.publishConfirmText(chosen.length)}</p>
   <ul>
    {#each chosen as p}
     <li>{p.title} ({scopeText(p.scope, p.scope_label)})</li>
    {/each}
   </ul>
   <div class="actions">
    <button class="primary" disabled={!!busy || !chosen.length} onclick={() => execute('publish')}>
     {t.confirmPublish}
    </button>
    <button class="subtle" disabled={!!busy} onclick={close}>{t.cancel}</button>
   </div>

  {:else if modal === 'ignore'}
   <p>{t.ignoreConfirmText}</p>
   <div class="actions">
    <button class="outline" disabled={!!busy} onclick={() => execute('ignore')}>{t.confirmIgnore}</button>
    <button class="subtle" disabled={!!busy} onclick={close}>{t.cancel}</button>
   </div>

  {:else if modal === 'archive' && card}
   <p>{t.archiveConfirmText(card.title)}</p>
   <div class="actions">
    <button class="primary danger-btn" disabled={!!busy} onclick={() => execute('archive')}>
     {t.confirmArchive}
    </button>
    <button class="subtle" disabled={!!busy} onclick={close}>{t.cancel}</button>
   </div>
  {/if}

  {#if error}<p class="feedback error" role="alert">{error}</p>{/if}
 </dialog>
{/if}
