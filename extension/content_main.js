(function () {
  const origFetch = window.fetch.bind(window);
  window.fetch = async function (input, init) {
    const res = await origFetch(input, init);
    try {
      const ct = res.headers.get('content-type') || '';
      const url = typeof input === 'string' ? input : (input?.url || '');
      if (ct.includes('spreadsheet') || ct.includes('excel') ||
          (ct.includes('octet-stream') && (url.includes('excel') || url.includes('xlsx') || url.includes('down')))) {
        res.clone().arrayBuffer().then(buf => {
          window.postMessage({ __minbest_type: 'excel', data: Array.from(new Uint8Array(buf)) }, '*');
        });
      }
    } catch (e) {}
    return res;
  };

  const origCreate = URL.createObjectURL.bind(URL);
  URL.createObjectURL = function (obj) {
    if (obj instanceof Blob && (
      obj.type.includes('spreadsheet') || obj.type.includes('excel') ||
      obj.type.includes('octet-stream') || obj.type === ''
    )) {
      obj.arrayBuffer().then(buf => {
        if (buf.byteLength > 1000) { // 실제 파일만 (빈 blob 제외)
          window.postMessage({ __minbest_type: 'excel', data: Array.from(new Uint8Array(buf)) }, '*');
        }
      });
    }
    return origCreate(obj);
  };
})();
