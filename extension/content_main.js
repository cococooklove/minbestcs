(function () {
  function postExcel(buf) {
    if (buf.byteLength < 100) return;
    window.postMessage({ __minbest_type: 'excel', data: Array.from(new Uint8Array(buf)) }, '*');
  }

  function looksLikeExcel(ct, url) {
    if (ct.includes('spreadsheet') || ct.includes('excel') || ct.includes('ms-excel')) return true;
    if (ct.includes('octet-stream')) return true;
    if (url && (url.includes('excel') || url.includes('xlsx') ||
                url.includes('download') || url.includes('export'))) return true;
    return false;
  }

  // 1. fetch 인터셉트
  const origFetch = window.fetch.bind(window);
  window.fetch = async function (input, init) {
    const res = await origFetch(input, init);
    try {
      const ct = res.headers.get('content-type') || '';
      const url = typeof input === 'string' ? input : (input?.url || '');
      if (looksLikeExcel(ct, url)) {
        res.clone().arrayBuffer().then(postExcel);
      }
    } catch (e) {}
    return res;
  };

  // 2. XHR 인터셉트 (blob / arraybuffer 응답)
  const origXhrOpen = XMLHttpRequest.prototype.open;
  const origXhrSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__mbUrl = url || '';
    return origXhrOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function () {
    this.addEventListener('load', function () {
      if (this.status !== 200) return;
      const ct = this.getResponseHeader('content-type') || '';
      if (!looksLikeExcel(ct, this.__mbUrl)) return;
      try {
        if (this.responseType === 'arraybuffer' && this.response) {
          postExcel(this.response);
        } else if (this.responseType === 'blob' && this.response) {
          this.response.arrayBuffer().then(postExcel);
        }
      } catch (e) {}
    });
    return origXhrSend.apply(this, arguments);
  };

  // 3. blob URL 생성 인터셉트
  const origCOU = URL.createObjectURL.bind(URL);
  URL.createObjectURL = function (obj) {
    if (obj instanceof Blob && obj.size > 100) {
      obj.arrayBuffer().then(buf => {
        if (looksLikeExcel(obj.type || '', '')) postExcel(buf);
      });
    }
    return origCOU(obj);
  };

  // 4. <a> 태그 다운로드 클릭 인터셉트
  document.addEventListener('click', function (e) {
    const a = e.target.closest('a');
    if (!a) return;
    const href = a.href || '';
    if (a.hasAttribute('download') || looksLikeExcel('', href)) {
      e.preventDefault();
      e.stopPropagation();
      fetch(href, { credentials: 'include' })
        .then(r => r.arrayBuffer())
        .then(postExcel)
        .catch(() => {});
    }
  }, true);

  // 5. window.open 인터셉트 (새 창 다운로드)
  const origWindowOpen = window.open.bind(window);
  window.open = function (url, ...args) {
    if (url && looksLikeExcel('', String(url))) {
      fetch(String(url), { credentials: 'include' })
        .then(r => r.arrayBuffer())
        .then(postExcel)
        .catch(() => {});
      return null;
    }
    return origWindowOpen(url, ...args);
  };
})();
