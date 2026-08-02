(function () {
  function text(el) {
    return el ? (el.textContent || '').replace(/\s+/g, ' ').trim() : '';
  }

  function meta(prop) {
    var el = document.querySelector('meta[property="' + prop + '"]') ||
             document.querySelector('meta[name="' + prop + '"]');
    return el ? (el.getAttribute('content') || '').trim() : '';
  }

  function safe(fn, fallback) {
    try {
      var v = fn();
      return (v === undefined || v === null) ? fallback : v;
    } catch (e) {
      return fallback;
    }
  }

  var brand = safe(function () {
    var el = document.querySelector('[itemprop="brand"] [itemprop="name"]') ||
             document.querySelector('[itemprop="brand"]') ||
             document.querySelector('a[href*="/designers/"]');
    return text(el);
  }, '');

  var name = safe(function () {
    var el = document.querySelector('h1[itemprop="name"]') || document.querySelector('h1');
    return text(el);
  }, '');

  var description = safe(function () {
    // #perfume-description-content is the actual visible write-up. Fall back to
    // itemprop="description" generically, since that attribute can also appear
    // on an earlier, unrelated (possibly truncated) element on the page —
    // querySelector would grab that one first if we searched by itemprop alone.
    var el = document.querySelector('#perfume-description-content') ||
             document.querySelector('[itemprop="description"]');
    var onPage = text(el);
    if (onPage) return onPage;
    return meta('og:description') || meta('description');
  }, '');

  function absolutize(url) {
    try {
      return new URL(url, document.baseURI).href;
    } catch (e) {
      return url;
    }
  }

  function bestFromSrcset(srcset) {
    if (!srcset) return '';
    var candidates = srcset.split(',').map(function (s) { return s.trim(); }).filter(Boolean);
    if (!candidates.length) return '';
    var twoX = candidates.filter(function (c) { return /\s2x$/i.test(c); });
    var chosen = twoX.length ? twoX[0] : candidates[candidates.length - 1];
    return chosen.split(/\s+/)[0] || '';
  }

  var imageUrl = safe(function () {
    // The actual bottle photo is marked itemprop="image" — prefer this over any
    // social-card / og:image meta tag, which is often a generic branded card image.
    var img = document.querySelector('img[itemprop="image"]');
    if (img) {
      var fromSrcset = bestFromSrcset(img.getAttribute('srcset'));
      if (fromSrcset) return absolutize(fromSrcset);
      var src = img.getAttribute('src');
      if (src) return absolutize(src);
    }
    return meta('og:image');
  }, '');

  var notes = safe(function () {
    var tierNames = {
      top: ['top notes', 'top note'],
      middle: ['middle notes', 'heart notes', 'middle note', 'heart note'],
      base: ['base notes', 'base note']
    };
    var all = Array.prototype.slice.call(document.querySelectorAll('body *'));
    var headerPositions = [];
    all.forEach(function (node) {
      if (node.children && node.children.length > 3) return;
      var t = (node.textContent || '').trim().toLowerCase();
      if (!t || t.length > 30) return;
      Object.keys(tierNames).forEach(function (tier) {
        if (tierNames[tier].indexOf(t) !== -1) {
          headerPositions.push({ tier: tier, node: node });
        }
      });
    });

    var noteLinks = Array.prototype.slice.call(document.querySelectorAll('a[href*="/notes/"]'));
    var result = { top: [], middle: [], base: [] };

    if (!headerPositions.length || !noteLinks.length) {
      noteLinks.forEach(function (a) {
        var n = text(a);
        if (n && result.top.indexOf(n) === -1) result.top.push(n);
      });
      return result;
    }

    noteLinks.forEach(function (a) {
      var n = text(a);
      if (!n) return;
      var bestTier = null;
      var bestHeader = null;
      headerPositions.forEach(function (h) {
        var headerBeforeNote = !!(h.node.compareDocumentPosition(a) & Node.DOCUMENT_POSITION_FOLLOWING);
        if (!headerBeforeNote) return;
        var isCloser = !bestHeader || !!(bestHeader.compareDocumentPosition(h.node) & Node.DOCUMENT_POSITION_FOLLOWING);
        if (isCloser) {
          bestHeader = h.node;
          bestTier = h.tier;
        }
      });
      if (bestTier && result[bestTier].indexOf(n) === -1) result[bestTier].push(n);
    });

    return result;
  }, { top: [], middle: [], base: [] });

  var tags = safe(function () {
    var out = [];

    // Primary signal: Fragrantica renders main accords as divs with an inline
    // style carrying both a background color and a width:NN% (the bar length),
    // with the accord name in a nested <span>. No stable class name to rely on.
    var styled = Array.prototype.slice.call(document.querySelectorAll('div[style*="width"]'));
    styled.forEach(function (el) {
      var style = el.getAttribute('style') || '';
      if (!/width\s*:\s*[\d.]+%/i.test(style)) return;
      if (!/background/i.test(style)) return;
      var span = el.querySelector('span');
      var t = text(span || el).replace(/[0-9]+(\.[0-9]+)?\s*%/g, '').trim().toLowerCase();
      if (t && t.length > 1 && t.length < 30 && out.indexOf(t) === -1) out.push(t);
    });

    // Fallback: any element whose class name mentions "accord", in case the
    // inline-style pattern above doesn't match (older markup, A/B test, etc.).
    if (!out.length) {
      var byClass = Array.prototype.slice.call(document.querySelectorAll('[class*="accord" i]'));
      byClass.forEach(function (el) {
        var t = text(el).replace(/[0-9]+(\.[0-9]+)?\s*%/g, '').trim().toLowerCase();
        if (t && t.length > 1 && t.length < 30 && out.indexOf(t) === -1) out.push(t);
      });
    }

    return out.slice(0, 8);
  }, []);

  var noteImages = safe(function () {
    // Fragrantica's note icons carry alt="<Note Name>" that matches the note's
    // display text exactly. Matching by alt text (rather than DOM proximity to
    // the /notes/ link) works regardless of how the icon and link are nested
    // relative to each other, which we can't rely on.
    var map = {};
    var allNoteNames = [].concat(notes.top, notes.middle, notes.base);
    if (!allNoteNames.length) return map;

    var byAltLower = {};
    Array.prototype.slice.call(document.querySelectorAll('img[alt]')).forEach(function (img) {
      var alt = (img.getAttribute('alt') || '').trim().toLowerCase();
      if (alt && !byAltLower[alt]) byAltLower[alt] = img;
    });

    allNoteNames.forEach(function (name) {
      var img = byAltLower[name.trim().toLowerCase()];
      if (!img) return;
      var src = bestFromSrcset(img.getAttribute('srcset')) || img.getAttribute('src');
      if (src) map[name] = absolutize(src);
    });

    return map;
  }, {});

  var price = safe(function () {
    // Fragrantica's price box is a sponsored ad unit (retailer names + affiliate
    // "goto.php" tracking links) — we deliberately only pull the single headline
    // reference number for this product, not the retailer list or any of those
    // links. goto.php is Fragrantica's consistent affiliate-redirect pattern, so
    // "a goto.php link whose only bold content is a plain number, near USD" is a
    // reasonably durable signal without depending on the ad box's styling classes.
    var links = Array.prototype.slice.call(document.querySelectorAll('a[href*="goto.php"]'));
    for (var i = 0; i < links.length; i++) {
      var bold = links[i].querySelector('b');
      if (!bold) continue;
      var raw = (bold.textContent || '').trim();
      if (!/^[0-9]+(\.[0-9]+)?$/.test(raw)) continue;
      if (!/USD/i.test(links[i].textContent || '')) continue;
      var num = parseFloat(raw);
      if (!isNaN(num) && num > 0) return num;
    }
    return null;
  }, null);

  var whenToWear = safe(function () {
    // The "When To Wear" widget shows community vote counts for each season
    // and day/night. We grab the raw counts here and leave the "which
    // seasons count as a match" threshold logic to the app itself (app.js) —
    // that's a judgment call worth being able to tune without redistributing
    // a new bookmarklet every time.
    var result = { winter: 0, spring: 0, summer: 0, fall: 0, day: 0, night: 0 };
    var cards = Array.prototype.slice.call(document.querySelectorAll('.tw-rating-card'));
    var card = null;
    for (var i = 0; i < cards.length; i++) {
      var label = cards[i].querySelector('.tw-rating-card-label');
      if (label && /when to wear/i.test(label.textContent || '')) {
        card = cards[i];
        break;
      }
    }
    if (!card) return result;

    var items = Array.prototype.slice.call(card.querySelectorAll('[index]'));
    items.forEach(function (item) {
      var labelEl = item.querySelector(':scope > span') || item.querySelector('span');
      if (!labelEl) return;
      var key = (labelEl.textContent || '').trim().toLowerCase();
      if (!(key in result)) return;
      var countEl = item.querySelector('.tabular-nums');
      var count = countEl ? parseInt((countEl.textContent || '').replace(/[^0-9]/g, ''), 10) : NaN;
      if (!isNaN(count)) result[key] = count;
    });
    return result;
  }, { winter: 0, spring: 0, summer: 0, fall: 0, day: 0, night: 0 });

  var payload = {
    brand: brand,
    name: name,
    description: description,
    notes: notes,
    noteImages: noteImages,
    tags: tags,
    price: price,
    whenToWear: whenToWear,
    imageUrl: imageUrl,
    sourceUrl: window.location.href
  };

  var json = JSON.stringify(payload);
  var encoded = encodeURIComponent(btoa(unescape(encodeURIComponent(json))));
  window.open('__ADD_URL__#import=' + encoded, '_blank');
})();
