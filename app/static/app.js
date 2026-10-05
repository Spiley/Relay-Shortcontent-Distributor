'use strict';
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const escapeHTML = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon = (name) => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const labels = {tiktok:'TikTok', instagram:'Instagram', youtube:'YouTube Shorts'};
const uploadLinks = {tiktok:'https://www.tiktok.com/tiktokstudio/upload',instagram:'https://www.instagram.com/',youtube:'https://studio.youtube.com/'};
const statusLabels = {queued:'Queued',uploading:'Uploading',processing:'Processing',publishing:'Publishing',published:'Published',restricted:'Restricted visibility',draft:'Finish in TikTok',attention:'Needs attention',failed:'Failed',reviewed:'Checked manually'};
function newRequestID() {
  // randomUUID needs HTTPS or localhost; getRandomValues also works on HTTP LAN URLs.
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
}
let settings = null;
let jobs = [];
let selectedFile = null;
let objectURL = null;
let selected = new Set();
let requestID = newRequestID();
let busy = false;
let currentView = 'compose';
let setupRequired = false;
let settingsDirty = false;
let creatorReady = false;
let creatorLoading = false;
let toastTimer;
let pollTimer;

function toast(message) {
  clearTimeout(toastTimer);
  $('#toast').textContent = message;
  $('#toast').hidden = false;
  toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 6000);
}

async function api(url, options = {}) {
  const headers = {'X-Relay':'1', ...(options.headers || {})};
  if (options.body && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  const response = await fetch(url, {...options, headers});
  let data;
  try { data = await response.json(); } catch { throw new Error('Your server returned an unexpected response. Check that Relay is running.'); }
  if (!response.ok) {
    if (response.status === 401 && url !== '/api/login') showAuth(false);
    throw new Error(typeof data.detail === 'string' ? data.detail : 'The request could not be completed. Check your inputs.');
  }
  return data;
}

function showAuth(firstRun) {
  setupRequired = firstRun;
  clearInterval(pollTimer);
  $('#workspace').hidden = true;
  $('#auth-screen').hidden = false;
  $('#auth-title').textContent = firstRun ? 'Your own little launchpad.' : 'Welcome back to Relay.';
  $('#auth-description').textContent = firstRun ? 'Set a password to keep your videos and account connections private.' : 'Sign in to your studio. Your next video is waiting.';
  $('#password').placeholder = firstRun ? 'At least 12 characters' : 'Your Relay password';
  $('#password').minLength = firstRun ? 12 : 1;
  $('#password').autocomplete = firstRun ? 'new-password' : 'current-password';
  $('#auth-submit').innerHTML = (firstRun ? 'Create my workspace' : 'Open my workspace') + icon('arrow');
}

async function openWorkspace() {
  $('#auth-screen').hidden = true;
  $('#workspace').hidden = false;
  await loadSettings(true);
  await loadJobs();
  clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    if (!document.hidden) loadJobs().catch(() => {});
  }, 5000);
  const query = new URLSearchParams(location.search);
  if (query.has('connected')) {
    toast(`${labels[query.get('connected')] || 'Account'} connected. You're ready to create a post.`);
    $('#manual-mode').checked = false;
    selected = new Set(Object.keys(labels).filter(p => settings.accounts[p].connected));
    renderPlatforms();
    history.replaceState({}, '', '/');
  }
  if (query.has('connection_error')) {
    showView('connections');
    toast(query.get('connection_error'));
    history.replaceState({}, '', '/');
  }
}

async function loadSettings(initial = false) {
  settings = await api('/api/settings');
  $('#upload-limit').textContent = settings.max_upload_mb;
  if (initial) {
    const connected = Object.keys(labels).filter(p => settings.accounts[p].connected);
    $('#manual-mode').checked = connected.length === 0;
    selected = new Set(connected.length ? connected : Object.keys(labels));
  }
  renderConnections();
  renderPlatforms();
}

function renderPlatforms() {
  if (!settings) return;
  const manual = $('#manual-mode').checked;
  if (!manual) selected = new Set([...selected].filter(p => settings.accounts[p].connected));
  $('#platform-grid').innerHTML = Object.entries(labels).map(([p,label]) => {
    const account = settings.accounts[p];
    const unavailable = !manual && !account.connected;
    const detail = manual ? 'Manual upload' : !account.connected ? 'Connect account' : (p === 'tiktok' && account.mode === 'draft') ? 'Send to drafts' : account.name;
    return `<label class="platform-choice ${selected.has(p) ? 'selected' : ''} ${unavailable ? 'unavailable' : ''}"><input type="checkbox" data-platform="${p}" aria-label="Select ${label}" ${selected.has(p) ? 'checked' : ''} ${unavailable ? 'disabled' : ''}><span class="platform-logo ${p}">${icon(p)}</span><span class="platform-text"><strong>${label}</strong><small title="${escapeHTML(detail)}">${escapeHTML(detail)}</small></span><span class="selection-indicator">${icon('check')}</span></label>`;
  }).join('');
  $('#publish-summary').innerHTML = icon('lock') + (manual ? 'Saved on your own server.' : `${selected.size} destination${selected.size === 1 ? '' : 's'} selected`);
  const hasDraft = !manual && selected.has('tiktok') && settings.accounts.tiktok.mode === 'draft';
  const notice = $('#compose-notice');
  notice.hidden = !manual && !hasDraft && selected.size > 0;
  notice.textContent = manual ? 'Relay will prepare and save your video. Then download it, copy your caption, and open each platform to post. This mode works without any API accounts.' : hasDraft ? 'TikTok will receive a draft: open its inbox to publish and paste the caption. Other selected platforms upload automatically. Results appear below; platform processing can take a few minutes.' : 'Connect an account to publish automatically, or turn on manual upload to get started now.';
  $('#mode-hint').textContent = manual ? 'Download, copy caption, post' : 'No platform accounts needed';
  updateTikTokOptions();
  updatePublishButton();
}

function updatePublishButton() {
  const manual = $('#manual-mode').checked;
  const direct = settings && !manual && selected.has('tiktok') && settings.accounts.tiktok.mode === 'direct';
  const invalidDirect = direct && (!creatorReady || !$('#tiktok-privacy').value || !$('#tiktok-consent').checked ||
    ($('#commercial').checked && !$('#own-brand').checked && !$('#branded-content').checked) ||
    ($('#branded-content').checked && $('#tiktok-privacy').value === 'SELF_ONLY'));
  $('#publish').disabled = busy || !selected.size || !!invalidDirect;
  if (!busy) {
    const title = manual ? 'Prepare video' : selected.has('tiktok') && settings?.accounts.tiktok.mode === 'draft' ? 'Upload video' : 'Publish video';
    $('#publish').innerHTML = title + icon('arrow');
  }
}

async function updateTikTokOptions() {
  const direct = !$('#manual-mode').checked && selected.has('tiktok') && settings.accounts.tiktok.mode === 'direct';
  $('#tiktok-options').hidden = !direct;
  if (!direct || creatorLoading || creatorReady) return;
  creatorLoading = true;
  $('#tiktok-account').textContent = 'Loading the latest account settings…';
  try {
    const info = await api('/api/tiktok/creator');
    $('#tiktok-account').textContent = `Posting as ${info.creator_nickname}. Account limit: ${info.max_video_post_duration_sec} seconds. Unaudited apps can only post privately.`;
    const choices = {PUBLIC_TO_EVERYONE:'Everyone', MUTUAL_FOLLOW_FRIENDS:'Friends', FOLLOWER_OF_CREATOR:'Followers', SELF_ONLY:'Only me'};
    $('#tiktok-privacy').innerHTML = '<option value="">Choose visibility…</option>' + (info.privacy_level_options || []).map(p => `<option value="${escapeHTML(p)}">${escapeHTML(choices[p] || p)}</option>`).join('');
    for (const field of ['comment','duet','stitch']) {
      const checkbox = $('#allow-' + field);
      checkbox.disabled = !!info[field + '_disabled'];
      checkbox.checked = false;
      checkbox.parentElement.title = checkbox.disabled ? 'Disabled by your TikTok account settings' : '';
    }
    creatorReady = true;
  } catch (error) {
    $('#tiktok-account').textContent = error.message + ' Reload the page to try again.';
  } finally {
    creatorLoading = false;
    updatePublishButton();
  }
}

function updateBrandOptions() {
  $('#brand-options').hidden = !$('#commercial').checked;
  if (!$('#commercial').checked) { $('#own-brand').checked = false; $('#branded-content').checked = false; }
  $('#branded-consent').hidden = !$('#branded-content').checked;
  $('#brand-label').textContent = $('#branded-content').checked ? "Your video will be labeled as 'Paid partnership'. Visibility cannot be Only me." : $('#own-brand').checked ? "Your video will be labeled as 'Promotional content'." : 'Select your brand, branded content, or both.';
  updatePublishButton();
}

const instructions = {
  youtube: `<li>Open <a href="https://console.cloud.google.com/apis/library/youtube.googleapis.com" target="_blank" rel="noopener noreferrer">Google Cloud</a>. Create a project and enable YouTube Data API v3.</li><li>Set up the OAuth consent screen. Add your Google account as a test user if the app is in Testing.</li><li>Create an OAuth client of type <strong>Web application</strong>. Add the callback URL below as an authorized redirect URI. Paste the client ID and secret here.</li><li>Save, then connect your channel. Testing refresh tokens usually expire after 7 days; reconnect or move the app to Production. Unverified API uploads may be private.</li>`,
  instagram: `<li>Use a <strong>Creator or Business</strong> Instagram account. Personal accounts cannot publish through this API.</li><li>Create a <a href="https://developers.facebook.com/apps/" target="_blank" rel="noopener noreferrer">Meta developer app</a> with Instagram API and <strong>Instagram Login</strong> (not Facebook Login).</li><li>Add the callback URL below. Enable <code>instagram_business_basic</code> and <code>instagram_business_content_publish</code>. Add your account as an Instagram tester and accept its invitation.</li><li>Paste the <strong>Instagram app</strong> ID and secret. Set a public HTTPS URL above so Instagram can fetch your video. Save, then connect.</li>`,
  tiktok: `<li>Create an app at <a href="https://developers.tiktok.com/apps/" target="_blank" rel="noopener noreferrer">TikTok for Developers</a>. Add Login Kit and Content Posting API.</li><li>Register the HTTPS callback URL below. Draft mode needs <code>video.upload</code> and <code>user.info.basic</code>; Direct Post needs <code>video.publish</code> and <code>user.info.basic</code>.</li><li>Paste your <strong>Client key</strong> and client secret. Save, then connect. Draft uploads must be finished in TikTok and the caption pasted manually.</li><li>Public Direct Post needs TikTok's audit. TikTok excludes tools for only your own or your team's accounts; approval is not guaranteed. Unaudited direct posting requires private accounts and Only me visibility.</li>`
};

function renderConnections() {
  const c = settings.config;
  $('#base-url').value = c.base_url;
  $('#connection-dot').classList.toggle('connected', Object.values(settings.accounts).some(a => a.connected));
  $('#connection-cards').innerHTML = ['youtube','instagram','tiktok'].map(p => {
    const a = settings.accounts[p];
    const expired = a.expires_at && a.expires_at < Date.now()/1000;
    const status = a.connected ? (expired ? 'Access expired · reconnect' : 'Connected') : 'Not connected';
    return `<article class="connection-card"><div class="connection-header"><span class="platform-logo ${p}">${icon(p)}</span><div><h2>${labels[p]}</h2><small>${p === 'instagram' ? 'Posts as an Instagram Reel' : p === 'tiktok' ? 'Draft or approved direct posting' : 'Square or vertical videos, up to 3 minutes'}</small></div><span class="status-badge ${a.connected && !expired ? 'status-published' : ''}">${status}</span></div><div class="connection-body"><div><label for="${p}-client-id">${p === 'tiktok' ? 'Client key' : p === 'instagram' ? 'Instagram app ID' : 'Client ID'}</label><input id="${p}-client-id" value="${escapeHTML(c[p+'_client_id'] || '')}" autocomplete="off" spellcheck="false"><label for="${p}-client-secret">${p === 'instagram' ? 'Instagram app secret' : 'Client secret'}</label><input id="${p}-client-secret" type="password" autocomplete="new-password" placeholder="${a.configured ? 'Saved securely · leave blank to keep' : 'Paste your app secret'}">${p === 'tiktok' ? `<label for="tiktok-mode">Posting mode</label><select id="tiktok-mode"><option value="draft" ${c.tiktok_mode === 'draft' ? 'selected' : ''}>Send to TikTok drafts (finish in app)</option><option value="direct" ${c.tiktok_mode === 'direct' ? 'selected' : ''}>Direct Post (requires approval)</option></select>` : ''}</div><ol class="setup-instructions">${instructions[p]}</ol></div><label>Callback URL <small>Copy this exactly into the developer app</small></label><div class="callback-row"><code>${escapeHTML(a.callback_url)}</code><button type="button" class="text-button" data-copy-callback="${p}">Copy</button></div><div class="connection-footer"><p>${a.connected ? `Connected as <strong>${escapeHTML(a.name)}</strong>` : 'One-time permission from your account.'}</p><div class="connection-actions">${a.connected ? `<button class="text-button" type="button" data-disconnect="${p}">Disconnect</button> &nbsp; ` : ''}<button class="button secondary" type="button" data-connect="${p}">${a.connected ? 'Reconnect' : 'Connect'} ${p === 'youtube' ? 'YouTube' : labels[p]} ${icon('external')}</button></div></div></article>`;
  }).join('');
  settingsDirty = false;
}

function settingsValues() {
  const values = {base_url:$('#base-url').value.trim(), tiktok_mode:$('#tiktok-mode').value,instagram_version:settings.config.instagram_version || 'v24.0'};
  for (const p of Object.keys(labels)) {
    values[p+'_client_id'] = $('#'+p+'-client-id').value.trim();
    values[p+'_client_secret'] = $('#'+p+'-client-secret').value.trim();
  }
  return values;
}

async function saveSettings() {
  $('#settings-error').textContent = '';
  $('#save-settings').disabled = true;
  try {
    await api('/api/settings',{method:'POST',body:JSON.stringify(settingsValues())});
    creatorReady = false;
    await loadSettings();
    toast('Settings saved securely on your server.');
  } catch (error) {
    $('#settings-error').textContent = error.message;
    throw error;
  } finally { $('#save-settings').disabled = false; }
}

function showView(view) {
  currentView = view;
  $$('.view').forEach(el => {el.hidden = el.id !== 'view-' + view;});
  $$('.nav-item').forEach(el => el.classList.toggle('active',el.dataset.view === view));
  $('#page-name').textContent = {compose:'New post',activity:'Post history',connections:'Connections'}[view];
  if (view === 'compose') { creatorReady = false; updateTikTokOptions(); }
  if (view === 'connections') updateCallbacks();
  window.scrollTo({top:0,behavior:'instant'});
}

function updateCallbacks() {
  const base = $('#base-url').value.trim().replace(/\/$/,'');
  $$('.callback-row').forEach((row,index) => {
    const p = ['youtube','instagram','tiktok'][index];
    row.querySelector('code').textContent = `${base}/oauth/${p}/callback`;
  });
}

function pickFile(file) {
  if (busy) return;
  $('#post-error').textContent = '';
  if (!file || !/\.(mp4|mov|webm)$/i.test(file.name)) { $('#post-error').textContent = 'Choose an MP4, MOV, or WebM video.'; return; }
  if (file.size > settings.max_upload_mb * 1024 * 1024) { $('#post-error').textContent = `Use a video smaller than ${settings.max_upload_mb} MB.`; return; }
  selectedFile = file;
  requestID = newRequestID();
  if (objectURL) URL.revokeObjectURL(objectURL);
  objectURL = URL.createObjectURL(file);
  $('#preview').src = objectURL;
  $('#preview').hidden = false;
  $('#upload-prompt').hidden = true;
  $('#file-details').hidden = false;
  $('#file-name').textContent = `${file.name} · ${formatBytes(file.size)}`;
  $('#caption').focus();
}

function removeFile() {
  if (busy) return;
  selectedFile = null;
  $('#preview').pause();
  $('#preview').removeAttribute('src');
  $('#preview').load();
  if (objectURL) URL.revokeObjectURL(objectURL);
  objectURL = null;
  $('#preview').hidden = true;
  $('#upload-prompt').hidden = false;
  $('#file-details').hidden = true;
  $('#video-file').value = '';
  requestID = newRequestID();
}

function postOptions() {
  return {
    manual:$('#manual-mode').checked, youtube_title:$('#youtube-title').value.trim(), youtube_privacy:$('#youtube-privacy').value,
    made_for_kids:$('#made-for-kids').checked, ai_generated:$('#ai-generated').checked,
    tiktok_privacy:$('#tiktok-privacy').value, tiktok_consent:$('#tiktok-consent').checked,
    allow_comment:$('#allow-comment').checked,allow_duet:$('#allow-duet').checked,allow_stitch:$('#allow-stitch').checked,
    commercial:$('#commercial').checked,own_brand:$('#own-brand').checked,branded_content:$('#branded-content').checked
  };
}

async function uploadPost(event) {
  event.preventDefault();
  if (busy) return;
  $('#post-error').textContent = '';
  if (!selectedFile) { $('#post-error').textContent = 'Add your video first.'; return; }
  if (!selected.size) { $('#post-error').textContent = 'Select at least one destination.'; return; }
  const manual = $('#manual-mode').checked;
  const form = new FormData();
  form.append('video',selectedFile);
  form.append('caption',$('#caption').value);
  form.append('platforms',JSON.stringify([...selected]));
  form.append('options',JSON.stringify(postOptions()));
  form.append('request_id',requestID);
  busy = true;
  updatePublishButton();
  $('#publish').textContent = 'Preparing…';
  $('#upload-progress').hidden = false;
  $('#progress').value = 0;
  $('#progress-text').textContent = 'Uploading to your server…';
  try {
    await new Promise((resolve,reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST','/api/jobs');
      xhr.setRequestHeader('X-Relay','1');
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
          const progress = Math.round(e.loaded/e.total*100);
          $('#progress').value = progress;
          $('#progress-text').textContent = progress === 100 ? 'Preparing your video for the platforms. You can leave this page after it appears in history.' : `Uploading to your server… ${progress}%`;
        }
      };
      xhr.onerror = () => reject(new Error('Connection interrupted. Check post history before submitting again. Retry this same upload to recover an already saved post.'));
      xhr.onload = () => {
        let data;
        try {data = JSON.parse(xhr.responseText);} catch {reject(new Error('Unexpected server response. Check history before submitting again.'));return;}
        if (xhr.status >= 200 && xhr.status < 300) resolve(data);
        else reject(new Error(typeof data.detail === 'string' ? data.detail : 'The video could not be prepared. Check your settings.'));
      };
      xhr.send(form);
    });
    busy = false;
    removeFile();
    requestID = newRequestID();
    $('#caption').value = '';
    $('#caption-count').textContent = '0';
    $('#youtube-title').value = '';
    $('#tiktok-consent').checked = false;
    $('#tiktok-privacy').value = '';
    creatorReady = false;
    await loadJobs();
    toast(manual ? 'Video prepared. Download and copy your caption below.' : 'Video queued. Watch the results below as each platform processes it.');
  } catch (error) { $('#post-error').textContent = error.message; }
  finally { busy = false; $('#upload-progress').hidden = true; updatePublishButton(); }
}

function formatBytes(bytes) {
  if (bytes >= 1024**3) return (bytes/1024**3).toFixed(1) + ' GB';
  return (bytes/1024**2).toFixed(1) + ' MB';
}

function safeLink(url) {
  try { const u = new URL(url); return u.protocol === 'https:' ? escapeHTML(u.href) : ''; } catch { return ''; }
}

function renderPost(job) {
  const manual = job.options.manual;
  const active = job.targets.some(t => ['queued','uploading','processing','publishing','attention'].includes(t.status));
  const badges = manual ? '<span class="status-badge">Saved for manual upload</span>' : job.targets.map(t => `<span class="status-badge status-${escapeHTML(t.status)}">${icon(t.platform)} ${labels[t.platform]} · ${statusLabels[t.status] || escapeHTML(t.status)}</span>`).join('');
  const details = job.targets.map(t => {
    const message = t.error || t.result.message;
    const url = safeLink(t.result.url);
    if (!message && !url && !['processing','uploading','queued'].includes(t.status)) return '';
    const pendingText = t.status === 'processing' ? 'The platform is processing your video.' : t.status === 'uploading' ? 'Uploading from your server.' : t.status === 'queued' ? 'Waiting in your local queue.' : '';
    const check = ['attention','processing'].includes(t.status) && t.remote_id;
    return `<div class="target-detail"><strong>${labels[t.platform]}:</strong> ${escapeHTML(message || pendingText)}${t.result.privacy ? ' · Visibility: ' + escapeHTML(t.result.privacy) : ''} ${url ? `<a href="${url}" target="_blank" rel="noopener noreferrer">Open post ↗</a>` : ''}${check ? `<button type="button" class="text-button" data-check-job="${job.id}" data-check-platform="${t.platform}">Check status</button>` : ''}${t.status === 'attention' ? `<button type="button" class="text-button" data-reviewed-job="${job.id}" data-reviewed-platform="${t.platform}">I've checked this on the platform</button>` : ''}</div>`;
  }).join('');
  const manualLinks = manual ? job.options.manual_platforms.map(p => `<a href="${uploadLinks[p]}" target="_blank" rel="noopener noreferrer">Open ${labels[p]} ↗</a>`).join('') : '';
  return `<article class="post-row"><div class="post-thumb">${icon('video')}</div><div class="post-main"><p class="post-caption">${escapeHTML(job.caption)}</p><span class="post-meta">${escapeHTML(job.filename)} · ${formatBytes(job.size)} · ${Math.round(job.meta.duration)}s</span><div class="post-statuses">${badges}</div>${details}<div class="post-actions"><a href="/api/jobs/${job.id}/video" download>Download video</a><button type="button" class="text-button" data-copy-caption="${job.id}">Copy caption</button>${manualLinks}${!active ? `<button type="button" class="text-button delete" data-delete-job="${job.id}">Remove local copy</button>` : ''}</div></div><time class="post-date" datetime="${new Date(job.created*1000).toISOString()}">${new Date(job.created*1000).toLocaleDateString(undefined,{month:'short',day:'numeric'})}</time></article>`;
}

async function loadJobs() {
  const data = await api('/api/jobs');
  jobs = data.jobs;
  $('#history-count').textContent = jobs.length;
  $('#storage-size').textContent = `${formatBytes(data.storage_bytes)} stored locally`;
  // A decorative meter indicates used space; no invented capacity or disk quota.
  $('#storage-meter').style.width = data.storage_bytes ? '100%' : '0%';
  const empty = `<div class="empty-posts">${icon('history')}<div><strong>Your next post starts here.</strong><p>Upload a video above. Its journey will appear right here.</p></div></div>`;
  $('#recent-posts').innerHTML = jobs.slice(0,3).map(renderPost).join('') || empty;
  $('#all-posts').innerHTML = jobs.map(renderPost).join('') || empty;
}

async function copy(text) {
  try { await navigator.clipboard.writeText(text); toast('Copied to clipboard.'); }
  catch {
    const area = document.createElement('textarea');
    area.value = text; document.body.append(area); area.select();
    const success = document.execCommand('copy'); area.remove();
    toast(success ? 'Copied to clipboard.' : 'Clipboard unavailable. Select and copy the text manually.');
  }
}

$('#auth-form').addEventListener('submit',async event => {
  event.preventDefault(); $('#auth-error').textContent = ''; $('#auth-submit').disabled = true;
  try { await api(setupRequired ? '/api/setup' : '/api/login',{method:'POST',body:JSON.stringify({password:$('#password').value})}); $('#password').value = ''; await openWorkspace(); }
  catch (error) { $('#auth-error').textContent = error.message; }
  finally { $('#auth-submit').disabled = false; }
});
$('#logout').addEventListener('click',async () => { try {await api('/api/logout',{method:'POST'});showAuth(false);} catch(error){toast(error.message);} });
$$('[data-view]').forEach(button => button.addEventListener('click',() => showView(button.dataset.view)));
$$('[data-open-connections]').forEach(button => button.addEventListener('click',() => showView('connections')));
$$('[data-open-history]').forEach(button => button.addEventListener('click',() => showView('activity')));
$('#settings-form').addEventListener('input',() => {settingsDirty = true;updateCallbacks();});
$('#settings-form').addEventListener('change',() => {settingsDirty = true;});
$('#settings-form').addEventListener('submit',async event => {event.preventDefault();try{await saveSettings();}catch{}});
$('#use-current-url').addEventListener('click',() => {$('#base-url').value = location.origin;settingsDirty = true;updateCallbacks();});
$('#platform-grid').addEventListener('change',event => {const p = event.target.dataset.platform;if (!p) return;if(event.target.checked)selected.add(p);else selected.delete(p);renderPlatforms();});
$('#manual-mode').addEventListener('change',() => {if($('#manual-mode').checked && !selected.size)selected = new Set(Object.keys(labels));renderPlatforms();});
$('#caption').addEventListener('input',() => {$('#caption-count').textContent = $('#caption').value.length;});
$('#post-form').addEventListener('submit',uploadPost);
$('#tiktok-options').addEventListener('change',updateBrandOptions);
$('#video-file').addEventListener('change',event => {if(event.target.files[0])pickFile(event.target.files[0]);});
$('#dropzone').addEventListener('click',event => {if(event.target.tagName !== 'VIDEO' && !selectedFile && !busy)$('#video-file').click();});
$('#dropzone').addEventListener('keydown',event => {if((event.key === 'Enter' || event.key === ' ') && !selectedFile){event.preventDefault();$('#video-file').click();}});
$('#dropzone').addEventListener('dragover',event => {event.preventDefault();$('#dropzone').classList.add('dragging');});
$('#dropzone').addEventListener('dragleave',() => $('#dropzone').classList.remove('dragging'));
$('#dropzone').addEventListener('drop',event => {event.preventDefault();$('#dropzone').classList.remove('dragging');pickFile(event.dataTransfer.files[0]);});
$('#remove-file').addEventListener('click',removeFile);
document.addEventListener('click',async event => {
  const button = event.target.closest('button');
  if (!button) return;
  if (button.dataset.copyCallback) { const base = $('#base-url').value.trim().replace(/\/$/,''); await copy(base+'/oauth/'+button.dataset.copyCallback+'/callback'); }
  if (button.dataset.connect) {
    button.disabled = true;
    try {if(settingsDirty)await saveSettings();location.assign('/oauth/'+button.dataset.connect+'/connect');}catch{button.disabled = false;}
  }
  if (button.dataset.disconnect) {
    button.disabled = true;
    try {await api('/api/accounts/'+button.dataset.disconnect+'/disconnect',{method:'POST'});await loadSettings();toast('Disconnected locally. You can revoke app access in the platform account settings too.');}catch(error){toast(error.message);button.disabled = false;}
  }
  if (button.dataset.copyCaption) {const job=jobs.find(j=>j.id===button.dataset.copyCaption);if(job)await copy(job.caption);}
  if (button.dataset.checkJob) {
    button.disabled = true;button.textContent = 'Checking…';
    try {await api(`/api/jobs/${button.dataset.checkJob}/${button.dataset.checkPlatform}/check`,{method:'POST'});await loadJobs();toast('Platform status checked.');}catch(error){toast(error.message);button.disabled=false;button.textContent='Check status';}
  }
  if (button.dataset.deleteJob) {
    button.disabled = true;
    try {await api('/api/jobs/'+button.dataset.deleteJob,{method:'DELETE'});await loadJobs();toast('Removed from your server. Posts on the platforms remain.');}catch(error){toast(error.message);button.disabled=false;}
  }
  if (button.dataset.reviewedJob) {
    button.disabled = true;
    try {await api(`/api/jobs/${button.dataset.reviewedJob}/${button.dataset.reviewedPlatform}/reviewed`,{method:'POST'});await loadJobs();toast('Marked as checked manually. No video was posted or retried.');}catch(error){toast(error.message);button.disabled=false;}
  }
});
(async () => {
  try {const session=await api('/api/session');if(session.authenticated)await openWorkspace();else showAuth(session.setup_required);}
  catch(error){showAuth(false);$('#auth-error').textContent=error.message;}
})();
