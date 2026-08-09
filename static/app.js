// Mobile sidebar drawer: toggle button, tap-outside backdrop, Escape key, scroll lock.
document.addEventListener('DOMContentLoaded', () => {
  const toggle = document.getElementById('menuToggle');
  const sidebar = document.getElementById('sidebar');
  const backdrop = document.getElementById('sidebarBackdrop');
  if (!toggle || !sidebar) return;

  function openSidebar() {
    sidebar.classList.add('open');
    if (backdrop) backdrop.classList.add('open');
    document.body.classList.add('sidebar-locked');
  }
  function closeSidebar() {
    sidebar.classList.remove('open');
    if (backdrop) backdrop.classList.remove('open');
    document.body.classList.remove('sidebar-locked');
  }

  toggle.addEventListener('click', () => {
    sidebar.classList.contains('open') ? closeSidebar() : openSidebar();
  });
  if (backdrop) backdrop.addEventListener('click', closeSidebar);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && sidebar.classList.contains('open')) closeSidebar();
  });
});

// Desktop sidebar collapse: persisted via localStorage so it survives page
// loads and navigation. The inline script in base.html applies the saved
// state before first paint; this just wires up the toggle click itself.
document.addEventListener('DOMContentLoaded', () => {
  const collapseToggle = document.getElementById('sidebarCollapseToggle');
  if (!collapseToggle) return;
  collapseToggle.addEventListener('click', () => {
    const collapsed = document.documentElement.classList.toggle('sidebar-collapsed');
    localStorage.setItem('sidebarCollapsed', collapsed);
  });
});

// Chip-style input for notes (top/middle/base) and tags.
// Keeps a hidden <input> in sync with a comma-separated list, backing a plain HTML form submit.
function initChipField(hiddenId, containerId, textInputId, initialValues) {
  const hidden = document.getElementById(hiddenId);
  const container = document.getElementById(containerId);
  const textInput = document.getElementById(textInputId);
  let values = Array.isArray(initialValues) ? [...initialValues] : [];

  function sync() {
    hidden.value = values.join(',');
    container.innerHTML = values.map((v, i) => `
      <span class="chip">${escapeHtml(v)}<button type="button" data-idx="${i}">×</button></span>
    `).join('');
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  container.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-idx]');
    if (!btn) return;
    values.splice(Number(btn.dataset.idx), 1);
    sync();
  });

  textInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ',') {
      e.preventDefault();
      const val = textInput.value.trim().replace(/,$/, '');
      if (val && !values.includes(val)) {
        values.push(val);
        sync();
      }
      textInput.value = '';
    }
  });

  sync();

  return {
    setValues(newValues) {
      values = Array.isArray(newValues) ? [...new Set(newValues.filter(Boolean))] : [];
      sync();
    },
    getValues() {
      return [...values];
    }
  };
}

// Stats page: grows each bar from 0 to its real width after first paint, so the
// bars visibly animate in rather than just appearing at full length.
function initStatsBars() {
  const bars = document.querySelectorAll('.bar-fill[data-target]');
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      bars.forEach((bar) => {
        bar.style.width = bar.dataset.target + '%';
      });
    });
  });
}

// Reads a payload left in the URL hash by the "Import from Fragrantica" bookmarklet
// (#import=<base64 JSON>). On the Add page, first checks whether a fragrance
// already exists — by Fragrantica URL first (survives local renames), falling
// back to brand+name — and if so, redirects to its Edit page (carrying the
// same import payload) and fills in only the gaps there instead of creating a
// duplicate. chipControllers: { top, middle, base, tags } from initChipField().
async function initFragranticaImport(mode, chipControllers) {
  const hash = window.location.hash;
  if (!hash.startsWith('#import=')) return;

  const encoded = hash.slice('#import='.length);
  let data;
  try {
    const json = decodeURIComponent(escape(atob(decodeURIComponent(encoded))));
    data = JSON.parse(json);
  } catch (err) {
    console.error('Fragrantica import failed to parse:', err);
    showImportBanner('<strong>Import failed.</strong> The bookmarklet sent data the form couldn\'t read. You can still fill the form in manually.');
    history.replaceState(null, '', window.location.pathname + window.location.search);
    return;
  }

  if (mode === 'add') {
    const match = await findExistingFragrance(data.brand, data.name, data.sourceUrl);
    if (match) {
      // Hand off to the Edit page with the same payload still in the hash —
      // don't clear it here, that page needs to run this same import itself.
      window.location.href = '/edit/' + match.id + '?matched=' + match.matchedBy + hash;
      return;
    }
    fillAddMode(data, chipControllers);
  } else {
    const matchedBy = new URLSearchParams(window.location.search).get('matched');
    fillEditModeGaps(data, chipControllers, matchedBy);
  }

  // Queue note icons for caching regardless of mode or which text fields ended
  // up applied — the icon library is shared collection-wide, so it's worth
  // capturing every icon the bookmarklet found on this page.
  const notesImagesField = document.getElementById('notesImagesJson');
  if (notesImagesField) notesImagesField.value = JSON.stringify(data.noteImages || {});

  history.replaceState(null, '', window.location.pathname + window.location.search);
}

async function findExistingFragrance(brand, name, url) {
  if (!brand && !name && !url) return null;
  try {
    const params = new URLSearchParams();
    if (url) params.set('url', url);
    if (brand) params.set('brand', brand);
    if (name) params.set('name', name);
    const resp = await fetch('/api/lookup?' + params.toString());
    if (!resp.ok) return null;
    const result = await resp.json();
    return result && result.found ? { id: result.id, matchedBy: result.matched_by } : null;
  } catch (err) {
    return null; // offline / lookup failed — fail open, treat this as a normal add
  }
}

function showImportBanner(html) {
  const banner = document.getElementById('importBanner');
  if (!banner) return;
  banner.style.display = 'block';
  banner.innerHTML = html;
}

function setImagePreview(url) {
  document.getElementById('importedImageUrl').value = url;
  const preview = document.getElementById('uploadPreview');
  preview.classList.toggle('no-bg', /\.png(\?|$)/i.test(url));
  preview.innerHTML = '<img src="' + url + '" referrerpolicy="no-referrer">';
}

// Turns raw Fragrantica "When To Wear" vote counts into a season list + a
// single Day/Night/Both pick. The threshold is a judgment call, deliberately
// kept here (not in the bookmarklet) so it's easy to retune later:
// - A season counts as a match if it got at least half as many votes as the
//   most-voted season — catches real multi-season fragrances without
//   including every season on a handful of stray votes.
// - Day/Night becomes "Both" unless one side clearly dominates (>=65%).
function deriveSeasonsAndDaynight(w) {
  if (!w) return { seasons: [], daynight: '' };

  const seasonCounts = { Spring: w.spring || 0, Summer: w.summer || 0, Fall: w.fall || 0, Winter: w.winter || 0 };
  const maxSeason = Math.max.apply(null, Object.keys(seasonCounts).map((k) => seasonCounts[k]));
  const seasons = [];
  if (maxSeason > 0) {
    Object.keys(seasonCounts).forEach((name) => {
      if (seasonCounts[name] >= maxSeason * 0.5) seasons.push(name);
    });
  }

  let daynight = '';
  const day = w.day || 0;
  const night = w.night || 0;
  const total = day + night;
  if (total > 0) {
    const dayShare = day / total;
    if (dayShare >= 0.65) daynight = 'Day';
    else if (dayShare <= 0.35) daynight = 'Night';
    else daynight = 'Both';
  }

  return { seasons, daynight };
}

function getCheckedSeasons() {
  return Array.prototype.slice.call(document.querySelectorAll('input[name=seasons]:checked')).map((el) => el.value);
}
function setSeasonCheckboxes(names) {
  // Always start from a clean slate — never just layer checks on top of
  // whatever's already checked. Stale state (browser form-autofill restoring
  // a previous fill, a leftover selection from before this function ran)
  // would otherwise silently survive alongside whatever we're setting now.
  document.querySelectorAll('input[name=seasons]').forEach((el) => { el.checked = false; });
  (names || []).forEach((name) => {
    const el = document.querySelector('input[name=seasons][value="' + name + '"]');
    if (el) {
      el.checked = true;
    } else {
      console.warn('setSeasonCheckboxes: no checkbox found with value="' + name + '" — check the form\'s season value attributes match.');
    }
  });
}
function getCheckedDaynight() {
  const el = document.querySelector('input[name=daynight]:checked');
  return el ? el.value : '';
}
function setDaynightRadio(value) {
  // Same reasoning as setSeasonCheckboxes: always clear first, even if we
  // end up not setting anything (value is empty), so a stale prior selection
  // never survives an import fill it wasn't part of.
  document.querySelectorAll('input[name=daynight]').forEach((el) => { el.checked = false; });
  if (!value) return;
  const el = document.querySelector('input[name=daynight][value="' + value + '"]');
  if (el) {
    el.checked = true;
  } else {
    console.warn('setDaynightRadio: no radio found with value="' + value + '" — check the form\'s day/night value attributes match.');
  }
}

function fillAddMode(data, chipControllers) {
  const setField = (id, val) => {
    const el = document.getElementById(id);
    if (el && val) el.value = val;
  };
  setField('fBrand', data.brand);
  setField('fName', data.name);
  setField('fDescription', data.description);
  setField('fPrice', data.price);
  setField('fFragranticaUrl', data.sourceUrl);

  if (data.notes) {
    if (data.notes.top && data.notes.top.length) chipControllers.top.setValues(data.notes.top);
    if (data.notes.middle && data.notes.middle.length) chipControllers.middle.setValues(data.notes.middle);
    if (data.notes.base && data.notes.base.length) chipControllers.base.setValues(data.notes.base);
  }
  if (data.tags && data.tags.length) chipControllers.tags.setValues(data.tags);
  if (data.imageUrl) setImagePreview(data.imageUrl);

  const derived = deriveSeasonsAndDaynight(data.whenToWear);
  // Unconditional on purpose (unlike the edit-mode gap-fill below) — this is
  // a fresh fill, so it should always clear any stale season/day-night state
  // rather than only doing so when there's something new to apply.
  setSeasonCheckboxes(derived.seasons);
  setDaynightRadio(derived.daynight);

  const found = [];
  const missing = [];
  (data.brand ? found : missing).push('brand');
  (data.name ? found : missing).push('name');
  (data.description ? found : missing).push('description');
  const noteCount = (data.notes?.top?.length || 0) + (data.notes?.middle?.length || 0) + (data.notes?.base?.length || 0);
  (noteCount ? found : missing).push('notes');
  (data.imageUrl ? found : missing).push('image');
  (data.tags && data.tags.length ? found : missing).push('accord tags');
  (data.price ? found : missing).push('price');
  (data.sourceUrl ? found : missing).push('Fragrantica link');
  (derived.seasons.length ? found : missing).push('season');
  (derived.daynight ? found : missing).push('day/night');

  showImportBanner(
    '<strong>Imported from Fragrantica.</strong> Found: ' + (found.join(', ') || 'nothing') + '.' +
    (missing.length ? ' Missing (fill in manually): ' + missing.join(', ') + '.' : '') +
    ' Season and day/night are inferred from community vote counts — double-check them along with everything else below before saving.'
  );
}

function fillEditModeGaps(data, chipControllers, matchedBy) {
  const filled = [];
  const skipped = [];

  const fillTextIfEmpty = (id, label, value) => {
    if (!value) return;
    const el = document.getElementById(id);
    if (!el) return;
    if (!el.value.trim()) { el.value = value; filled.push(label); }
    else { skipped.push(label); }
  };
  fillTextIfEmpty('fBrand', 'brand', data.brand);
  fillTextIfEmpty('fName', 'name', data.name);
  fillTextIfEmpty('fDescription', 'description', data.description);
  fillTextIfEmpty('fPrice', 'price', data.price);
  fillTextIfEmpty('fFragranticaUrl', 'Fragrantica link', data.sourceUrl);

  const fillTierIfEmpty = (ctrl, label, values) => {
    if (!values || !values.length) return;
    if (ctrl.getValues().length === 0) { ctrl.setValues(values); filled.push(label); }
    else { skipped.push(label); }
  };
  if (data.notes) {
    fillTierIfEmpty(chipControllers.top, 'top notes', data.notes.top);
    fillTierIfEmpty(chipControllers.middle, 'middle notes', data.notes.middle);
    fillTierIfEmpty(chipControllers.base, 'base notes', data.notes.base);
  }
  fillTierIfEmpty(chipControllers.tags, 'accord tags', data.tags);

  if (data.imageUrl) {
    const preview = document.getElementById('uploadPreview');
    const hasExistingImage = preview && !preview.querySelector('.ph');
    if (!hasExistingImage) { setImagePreview(data.imageUrl); filled.push('image'); }
    else { skipped.push('image'); }
  }

  const derived = deriveSeasonsAndDaynight(data.whenToWear);
  if (derived.seasons.length) {
    if (getCheckedSeasons().length === 0) { setSeasonCheckboxes(derived.seasons); filled.push('season'); }
    else { skipped.push('season'); }
  }
  if (derived.daynight) {
    if (!getCheckedDaynight()) { setDaynightRadio(derived.daynight); filled.push('day/night'); }
    else { skipped.push('day/night'); }
  }

  const matchExplain = matchedBy === 'url'
    ? 'Matched by its Fragrantica link'
    : matchedBy === 'name'
      ? 'Matched by brand + name'
      : 'Matched an existing fragrance';

  showImportBanner(
    '<strong>' + matchExplain + ' — this is already in your collection.</strong> ' +
    'Filled in: ' + (filled.join(', ') || 'nothing new') + '.' +
    (skipped.length ? ' Already had (left untouched): ' + skipped.join(', ') + '.' : '') +
    ' Nothing is saved yet — review below and click Save Changes.'
  );
}

// Note Library page: a note that already has an icon keeps its replace form
// collapsed behind a small button, so the page isn't cluttered with an open
// upload form under every note that's already fine.
function toggleNoteReplace(button) {
  const form = button.nextElementSibling;
  const opening = !form.classList.contains('open');
  form.classList.toggle('open', opening);
  button.style.display = opening ? 'none' : '';
}
