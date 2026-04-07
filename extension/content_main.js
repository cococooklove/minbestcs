(function () {
  function looksLikeExcel(url) {
    return /excel|xlsx|export|download/i.test(url);
  }

  function getServerUrl() {
    return document.documentElement.getAttribute('data-minbest-server') || '';
  }

  async function interceptDownload(url, method, body) {
    const serverUrl = getServerUrl();
    if (!serverUrl) return false;

    document.dispatchEvent(new CustomEvent('__minbest_progress', { detail: '파일 수신 중...' }));
    try {
      const opts = { credentials: 'include' };
      if (method === 'POST' && body) {
        opts.method = 'POST';
        opts.body = body;
      }
      const res = await fetch(url, opts);
      if (!res.ok) throw new Error(`Naver 응답 오류 (${res.status})`);
      const buf = await res.arrayBuffer();

      document.dispatchEvent(new CustomEvent('__minbest_progress', { detail: '서버로 파일 전송 중...' }));
      const blob = new Blob([buf], {
        type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
      });
      const form = new FormData();
      form.append('file', blob, 'reviews.xlsx');
      const r = await fetch(`${serverUrl}/api/upload-excel`, { method: 'POST', body: form });
      const data = await r.json();
      document.dispatchEvent(new CustomEvent('__minbest_progress', {
        detail: r.ok ? '완료' : `실패: ${data.error || '업로드 실패'}`
      }));
    } catch (e) {
      document.dispatchEvent(new CustomEvent('__minbest_progress', { detail: `실패: ${e.message}` }));
    }
    return true; // 인터셉트 성공
  }

  // 1. location.href setter 오버라이드 (window.location.href = url)
  try {
    const desc = Object.getOwnPropertyDescriptor(Location.prototype, 'href');
    if (desc && desc.set) {
      Object.defineProperty(Location.prototype, 'href', {
        ...desc,
        set(url) {
          if (looksLikeExcel(url) && getServerUrl()) {
            interceptDownload(url, 'GET', null);
            return; // 브라우저 다운로드 차단
          }
          desc.set.call(this, url);
        }
      });
    }
  } catch (e) {}

  // 2. location.assign 오버라이드
  const origAssign = Location.prototype.assign;
  Location.prototype.assign = function (url) {
    if (looksLikeExcel(url) && getServerUrl()) {
      interceptDownload(url, 'GET', null);
      return;
    }
    return origAssign.call(this, url);
  };

  // 3. form.submit() 오버라이드 (hidden form POST 다운로드)
  const origSubmit = HTMLFormElement.prototype.submit;
  HTMLFormElement.prototype.submit = function () {
    const action = this.action || '';
    if (looksLikeExcel(action) && getServerUrl()) {
      interceptDownload(action, 'POST', new FormData(this));
      return;
    }
    return origSubmit.call(this);
  };

  // 4. <a download> 클릭 오버라이드
  document.addEventListener('click', function (e) {
    const a = e.target.closest('a');
    if (!a) return;
    const href = a.href || '';
    if ((a.hasAttribute('download') || looksLikeExcel(href)) && getServerUrl()) {
      e.preventDefault();
      e.stopPropagation();
      interceptDownload(href, 'GET', null);
    }
  }, true);

  // 5. window.open 오버라이드
  const origOpen = window.open.bind(window);
  window.open = function (url, ...args) {
    if (url && looksLikeExcel(String(url)) && getServerUrl()) {
      interceptDownload(String(url), 'GET', null);
      return null;
    }
    return origOpen(url, ...args);
  };
})();
