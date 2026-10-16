/* how the catalogue search box reads a plain phrase. One copy, loaded by the studio (index.html) and the
   catalogue page (catalogue.html), so the two boxes never drift. Moved here verbatim from the studio's own script; a plain
   script (not a module), loaded before the page's own code: window.CatPhrase.
     parsePhrase(text) -> {request, role, words, shape} or {hint}
     mergeNames(parsed, rows, products)   plain words only: the designs whose name holds every word first, then `rows`
     explainNoMatch(words, probe)         plain words with no result: one sentence naming the word nothing is called
   It reads and writes no page state. */
(function (root) {
  'use strict';
  const SEARCH_HINT = 'Try: bore 38 to 42 · shaft 40-60 · od 60 · 40 mm bore · valve · family shaft';
  const NUM = '(?:\\d+\\.?\\d*|\\.\\d+)(?:e[+-]?\\d+)?';
  const NUM_RX = new RegExp('^(' + NUM + ')(?:-(' + NUM + '))?$', 'i');
  /* "diameter" and "journal" are size words too; dia / diam / size read as diameter, id as bore */
  const NUM_KW = ['bore', 'od', 'diameter', 'journal', 'length', 'width', 'height', 'volume'], WORD_KW = ['family', 'label'];   /* `like REF` (shape ranking) is not offered: "like" is a filler word */
  const ALIAS = { dia: 'diameter', diam: 'diameter', size: 'diameter', id: 'bore', bor: 'bore' };
  const FILLER = ['and', 'between', 'from', 'with', 'of', 'about', 'like'];
  const isKw = x => NUM_KW.includes(x) || WORD_KW.includes(x);
  function canon(x) {                       // a size word (or its plural / alias) -> the word the catalogue knows; else null
    if (ALIAS[x]) return ALIAS[x];
    if (isKw(x)) return x;
    if (x.length > 3 && x.endsWith('s') && isKw(x.slice(0, -1))) return x.slice(0, -1);
    return null;
  }
  function osa(a, b) {                      // edit distance, a swap of two neighbouring letters counts as one edit
    const d = [];
    for (let i = 0; i <= a.length; i++) d[i] = [i];
    for (let j = 1; j <= b.length; j++) d[0][j] = j;
    for (let i = 1; i <= a.length; i++) for (let j = 1; j <= b.length; j++) {
      d[i][j] = Math.min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
      if (i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) d[i][j] = Math.min(d[i][j], d[i - 2][j - 2] + 1);
    }
    return d[a.length][b.length];
  }
  function nearKw(x) {                      // a misspelt size word (4+ letters, one edit off; two for 7+) -> the right one, else null
    if (x.length < 4) return null;
    for (const k of NUM_KW.concat(['family'])) if (k.length >= 4 && osa(x, k) <= (k.length >= 7 ? 2 : 1)) return k;
    return null;
  }
  const say = (w, why) => ({ hint: 'I did not understand "' + w + '" ' + why });
  function parsePhrase(text) {
    /* -> {request, role[, words]} or {hint}. Case-insensitive. bore|od|diameter|journal|length|width|height|volume (dia, size, id, plurals
       and a one-letter slip are read too): v, a-b, a to b, between a and b, from a to b, units (40mm), the value before the keyword
       (40 mm bore, 38 to 42 bore); several filters (and / comma); family w; label w; like REF; words with no keyword search the part
       labels and the product names; a name plus one size ("shaft 40-60", "shaft between 40 and 60") is that name with that
       diameter; other words beside a keyword are part-label words; raw JSON. A word it cannot place is named in the hint. */
    const t = text.trim();
    if (t[0] === '{') {
      try {
        const raw = JSON.parse(t);
        if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('x');
        if (!raw.group_by && (!raw.find || raw.find === 'part' || raw.find === 'feature')) raw.group_by = 'product';
        return { request: raw };
      } catch (e) { return { hint: 'That is not a valid request. ' + SEARCH_HINT }; }
    }
    const norm = t.replace(/[‒-―−]/g, '-').replace(/[:=,;]/g, ' ')
      .replace(/(\d)\s*mm\b/gi, '$1').replace(/\bmm\b/gi, ' ')
      .replace(new RegExp('\\bbetween\\s+(' + NUM + ')\\s+and\\s+(' + NUM + ')', 'gi'), '$1-$2')
      .replace(new RegExp('(' + NUM + ')\\s*(?:-|to|\\.\\.)\\s*(' + NUM + ')', 'gi'), '$1-$2');
    const tok = norm.split(/\s+/).filter(Boolean);
    if (!tok.length) return { hint: SEARCH_HINT };
    const low = tok.map(x => x.toLowerCase());
    const isNum = i => i >= 0 && i < tok.length && NUM_RX.test(low[i]);
    for (let i = 0; i < tok.length; i++) {      // a size word with a slip, next to a number: read as the size word it nearly is
      if (isNum(i) || FILLER.includes(low[i])) continue;
      const c = canon(low[i]) || ((isNum(i - 1) || isNum(i + 1)) && nearKw(low[i]));
      if (c) low[i] = c;
    }
    const kwAt = i => !isNum(i) && !FILLER.includes(low[i]) && isKw(low[i]);
    const nums = tok.map((x, i) => i).filter(isNum);
    const anyKw = tok.some((x, i) => kwAt(i));
    const needs = ' - a size goes with a size word, for example bore 38 to 42 or shaft 40-60.';
    if (!anyKw && !nums.length) {
      /* plain words: part labels (grouped by product) and, on the page, product names */
      const words = tok.filter(x => !FILLER.includes(x.toLowerCase()));
      if (!words.length) return { hint: SEARCH_HINT };
      return { request: { find: 'part', where: { 'part.label': { like: words.join(' ') } }, group_by: 'product', limit: 1000 }, role: null, words };
    }
    const where = {}, roles = [], lw = [];
    let like = null, pend = null;
    const range = (kw, v) => {
      const m = NUM_RX.exec(v), a = parseFloat(m[1]), b = m[2] !== undefined ? parseFloat(m[2]) : null;
      if (b !== null) return [Math.min(a, b), Math.max(a, b)];
      return { near: a, tol: kw === 'volume' ? Math.max(a * 0.01, 0.5) : 0.5 };
    };
    if (!anyKw) {                              // a name and one size: the name's parts with a diameter of that size
      if (nums.length > 1) return say(tok[nums[1]], '- without a size word only one size can follow a name, for example shaft 40-60.');
      const words = tok.filter((x, i) => !isNum(i) && !FILLER.includes(low[i]));
      if (!words.length) return say(tok[nums[0]], 'on its own' + needs);
      where['part.label'] = { like: words.join(' ') };
      where.diameter = range('diameter', low[nums[0]]);
      return { request: { find: 'part', where, group_by: 'product', limit: 1000 }, role: null, sized: true, nameWords: words };
    }
    for (let i = 0; i < tok.length; i++) {
      const kw = low[i];
      if (FILLER.includes(kw)) continue;
      if (isNum(i)) { if (pend !== null) return say(tok[i], 'after ' + pend + needs); pend = kw; continue; }   // a value waits for its keyword
      if (!isKw(kw)) { lw.push(tok[i]); continue; }                       // a name word beside a size word
      if (kw in where || (kw === 'like' && like)) return say(tok[i], '- it is there twice.');
      let v = pend; pend = null;
      if (v === null) {                        // the value after the keyword
        do { i++; } while (i < tok.length && FILLER.includes(low[i]));
        v = tok[i];
      }
      if (v === undefined) return say(kw, 'on its own - it needs a value after it, for example ' + kw + (NUM_KW.includes(kw) ? ' 40.' : ' shaft.'));
      if (kw === 'family') { where.family = v.toLowerCase(); continue; }
      if (kw === 'label') { where['part.label'] = { like: v }; continue; }
      if (kw === 'like') { like = v; continue; }
      if (!NUM_RX.test(v)) return say(v, 'after "' + kw + '" - a number or a range is needed, for example ' + kw + ' 38 to 42.');
      where[kw] = range(kw, v);
      if (kw === 'bore' || kw === 'od') roles.push(kw);
      else if (kw === 'journal') roles.push('od');   // journals are the outside diameters
    }
    if (pend !== null) return say(pend, 'at the end' + needs);
    if (lw.length) where['part.label'] = { like: (where['part.label'] ? where['part.label'].like + ' ' : '') + lw.join(' ') };
    /* one bore, od or journal filter reads the matching diameters (find=feature); several, or a shape search, read the parts */
    const feature = roles.length === 1 && !like && !('diameter' in where);
    const request = { find: feature ? 'feature' : 'part', where, group_by: 'product', limit: like ? 20 : 1000 };
    if (feature) request.return = ['part.label', 'feat.diameter'];
    if (like) request.rank = { like, method: 'd2' };   /* a shape search lists the 20 closest parts, each with its distance */
    return { request, role: feature ? roles[0] : null, shape: !!like, nameWords: lw };
  }
  function explainNoMatch(words, probe, plain) {
    /* a search with name words found nothing -> a promise of one sentence naming the word nothing is called, or '' when every
       word exists (the size, not a word, is what found nothing). probe(word) -> Promise<bool> (does anything carry that word). */
    const ws = [...new Set((words || []).map(w => w.toLowerCase()))];
    if (!ws.length) return Promise.resolve('');
    return Promise.all(ws.map(w => probe(w).then(ok => [w, ok]).catch(() => [w, true]))).then(r => {
      const bad = r.filter(x => !x[1]).map(x => '"' + x[0] + '"');
      if (bad.length) return 'I did not understand ' + bad.join(' and ') + ' - no part or design is called that.';
      return plain && ws.length > 1 ? 'No part or design matches all of ' + ws.map(w => '"' + w + '"').join(', ') + ' together.' : '';
    });
  }
  function mergeNames(parsed, rows, products) {
    /* plain words only: products whose name holds every word come first (from the portfolio list already loaded), then the label matches, no duplicates */
    if (!parsed.words) return rows;
    const ws = parsed.words.map(w => w.toLowerCase());
    const named = (products || []).filter(p => { const n = (p.name || p.product_id || '').toLowerCase(); return ws.every(w => n.includes(w)); })
      .map(p => ({ id: p.product_id, name: p.name || p.product_id, rev: p.revision, parts: 'name matches' }));
    const have = new Set(named.map(x => x.id));
    return named.concat(rows.filter(x => !have.has(x.id)));
  }
  root.CatPhrase = Object.freeze({ SEARCH_HINT, parsePhrase, mergeNames, explainNoMatch });
})(window);
