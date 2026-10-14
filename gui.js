'use strict';
const el = id => document.getElementById(id);
const token = document.querySelector('meta[name=session-token]').content;
let selectedFile = null;
let resultURL = null;
let busy = false;

function status(message, kind = '') {
  el('status').textContent = message;
  el('status').className = `status ${kind}`;
}
function forgetResult() {
  if (resultURL) URL.revokeObjectURL(resultURL);
  resultURL = null;
  el('download').hidden = true;
}
function selectFile(file) {
  if (busy || !file) return;
  if (!/\.pdf$/i.test(file.name) || file.size > 100 * 1024 * 1024 || !file.size) {
    status('请选择一份不超过 100 MB 的 PDF 文件。', 'error');
    return;
  }
  selectedFile = file;
  forgetResult();
  el('inventory').hidden = true;
  el('file-name').textContent = file.name;
  el('file-size').textContent = `${(file.size / 1024 / 1024).toFixed(2)} MB · 点击可更换文件`;
  el('clean').disabled = el('inspect').disabled = false;
  status('文件已就绪，点击下方按钮处理。');
}
function setBusy(value) {
  busy = value;
  for (const id of ['clean', 'inspect', 'drop', 'pages', 'xobject', 'pattern', 'annotations']) {
    el(id).disabled = value || (['clean', 'inspect'].includes(id) && !selectedFile);
  }
  el('clean').innerHTML = value ? '正在处理…' : '去水印并保存 <span aria-hidden="true">↗</span>';
}
async function request(action) {
  const params = new URLSearchParams({name: selectedFile.name});
  for (const id of ['pages', 'xobject', 'pattern']) {
    if (el(id).value.trim()) params.set(id, el(id).value.trim());
  }
  if (el('annotations').checked) params.set('annotations', '1');
  const response = await fetch(`api/${action}?${params}`, {
    method: 'POST', headers: {'Content-Type': 'application/pdf', 'X-Session-Token': token}, body: selectedFile
  });
  if (!response.ok) {
    let message = '处理失败，请确认本地服务仍在运行。';
    try {message = (await response.json()).error || message;} catch (_) {}
    throw new Error(message);
  }
  return response;
}

el('drop').addEventListener('click', () => el('file').click());
el('file').addEventListener('change', () => {selectFile(el('file').files[0]); el('file').value = '';});
for (const event of ['dragenter', 'dragover']) {
  el('drop').addEventListener(event, e => {e.preventDefault(); if (!busy) el('drop').classList.add('dragging');});
}
el('drop').addEventListener('dragleave', () => el('drop').classList.remove('dragging'));
el('drop').addEventListener('drop', e => {
  e.preventDefault(); el('drop').classList.remove('dragging');
  if (e.dataTransfer.files.length !== 1) {status('每次请拖入一份 PDF。', 'error'); return;}
  selectFile(e.dataTransfer.files[0]);
});
// Dropping outside the target must not navigate away to the PDF.
window.addEventListener('dragover', e => e.preventDefault());
window.addEventListener('drop', e => e.preventDefault());

el('clean').addEventListener('click', async () => {
  if (busy || !selectedFile) return;
  setBusy(true); forgetResult(); status('正在去除水印，请稍候…');
  try {
    const response = await request('clean');
    resultURL = URL.createObjectURL(await response.blob());
    const link = el('download');
    link.href = resultURL;
    link.download = selectedFile.name.replace(/\.pdf$/i, '.clean.pdf');
    link.hidden = false;
    link.click();
    status(`已去除 ${response.headers.get('X-Removed-Paints')} 处水印绘制，已开始下载。`, 'success');
  } catch (error) {status(error.message, 'error');}
  finally {setBusy(false);}
});
el('inspect').addEventListener('click', async () => {
  if (busy || !selectedFile) return;
  setBusy(true); status('正在识别 PDF 对象…');
  try {
    const report = await (await request('inspect')).json();
    const table = document.createElement('table');
    const head = document.createElement('tr');
    for (const value of ['页码', '对象路径', '类型', '选择状态']) {
      const th = document.createElement('th'); th.textContent = value; head.appendChild(th);
    }
    table.appendChild(head);
    for (const object of report.objects) {
      const row = document.createElement('tr');
      for (const value of [object.page, object.path || '注释', object.type.replace('/', ''), object.selected ? '将删除' : '保留']) {
        const cell = document.createElement('td'); cell.textContent = value; row.appendChild(cell);
      }
      if (object.selected) row.lastChild.className = 'candidate';
      table.appendChild(row);
    }
    el('inventory').replaceChildren(table); el('inventory').hidden = false;
    status(`已检查 ${report.pages.length} 页，${report.objects.filter(o => o.selected).length} 个对象匹配当前选项。`);
  } catch (error) {status(error.message, 'error');}
  finally {setBusy(false);}
});
for (const id of ['pages', 'xobject', 'pattern', 'annotations']) {
  el(id).addEventListener('input', () => {forgetResult(); el('inventory').hidden = true; if (selectedFile) status('选项已更新，可重新识别或处理。');});
}
window.addEventListener('beforeunload', forgetResult);
