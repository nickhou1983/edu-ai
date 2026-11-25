import { useEffect, useRef, useState } from "react";
import type { DragEvent } from "react";
import { describeError, pending, type Api, type Submission } from "./api";
import { dateText, Empty, Icon, Problem, statusText, StudyNotebook, useRemote } from "./ui";

const pollHistory = (entries: Submission[]) => entries.some(pending);

function Preview({ file }: { file: File }) {
  const [url, setUrl] = useState("");
  useEffect(() => {
    const next = URL.createObjectURL(file);
    setUrl(next);
    return () => URL.revokeObjectURL(next);
  }, [file]);
  return url && <img src={url} alt="" />;
}

export function Desk({ api, sample, sampleOnly, open, mastery }: {
  api: Api; sample: boolean; sampleOnly: boolean; open: (id: string) => void; mastery: () => void;
}) {
  const history = useRemote<Submission[]>(api, "/submissions", pollHistory);
  const input = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [drag, setDrag] = useState(false);

  function add(selected: File[]) {
    setError("");
    if (files.length + selected.length > 4) return setError("一次最多上传 4 页。");
    if (selected.some((file) => !["image/jpeg", "image/png"].includes(file.type) || file.size > 8 * 1024 * 1024))
      return setError("请选择不超过 8 MB 的 JPG 或 PNG。");
    setFiles([...files, ...selected]);
  }

  function reorder(index: number, offset: number) {
    setFiles((current) => {
      const next = [...current];
      [next[index], next[index + offset]] = [next[index + offset], next[index]];
      return next;
    });
  }

  async function upload(useSample = false) {
    if (!useSample && !files.length) return;
    setBusy(true);
    setError("");
    try {
      const pages = useSample
        ? [new File([await api.blob("/demo/sample")], "合成数学样卷.png", { type: "image/png" })]
        : files;
      const body = new FormData();
      for (const file of pages) body.append("files", file);
      const submission = await api.json<Submission>("/submissions", { method: "POST", body });
      open(submission.id);
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setBusy(false);
    }
  }

  function drop(event: DragEvent) {
    event.preventDefault();
    setDrag(false);
    if (!busy) add(Array.from(event.dataTransfer.files));
  }

  return <>
    <section className="hero" aria-labelledby="desk-title">
      <div className="hero-message">
        <p className="eyebrow"><Icon name="spark" size={16} /> 初一数学 · 课后练习簿</p>
        <h1 id="desk-title">把错题，变成<br />会做的题。</h1>
        <p className="hero-copy">拍下作业，看清哪一步还不熟。再练几道同类题，把方法变成自己的。</p>
        <div className="hero-actions">
          <a href="#upload" className="button hero-action"><Icon name="upload" /> 上传作业<Icon name="arrow" size={17} /></a>
          <button className="hero-map-link" onClick={mastery}>看看知识点地图 <Icon name="arrow" size={16} /></button>
        </div>
      </div>
      <StudyNotebook subject="math" />
    </section>
    <ol className="pathline" aria-label="学习流程">
      <li><b>01</b><span>上传题图<small>从你的作业开始</small></span></li>
      <li><b>02</b><span>查看批改与掌握度<small>找到还不熟的地方</small></span></li>
      <li><b>03</b><span>做针对性练习<small>把方法练成自己的</small></span></li>
    </ol>
    <div className="desk-grid">
      <section className="panel" id="upload" aria-labelledby="upload-title">
        <p className="eyebrow section-code"><Icon name="upload" size={16} />从这里，开启一轮学习</p>
        <h2 id="upload-title">选几张清楚的题图</h2>
        <p className="muted">拍全题目和作答区域，上传前请裁掉姓名等信息。</p>
        {sampleOnly && <p className="small callout">本地演示使用合成样卷，不会分析自行上传的题图。</p>}
        <div className={`dropzone${drag ? " hovering" : ""}`} onDragOver={(event) => { event.preventDefault(); setDrag(true); }}
          onDragLeave={() => setDrag(false)} onDrop={drop}>
          <div className="upload-symbol"><Icon name="image" size={26} /></div>
          <button className="text-button" onClick={() => input.current?.click()} disabled={busy}>选择图片</button>
          <span className="small muted">或把图片拖到这里 · JPG/PNG · 每页最多 8 MB</span>
          <input ref={input} type="file" accept="image/jpeg,image/png" multiple hidden
            onChange={(event) => { add(Array.from(event.target.files ?? [])); event.target.value = ""; }} />
        </div>
        <p className="muted small">上传后自动等比缩小，不改动本地原图。最多 6400 万像素，宽高均至少 120 像素。</p>
        {!!files.length && (
          <ol className="file-list">
            {files.map((file, index) => <li key={`${file.name}-${index}`}>
              <Preview file={file} /><div className="file-name"><b>第 {index + 1} 页</b><span>{file.name}</span></div>
              <button className="mini-button" aria-label={`上移第 ${index + 1} 页`} disabled={busy || index === 0}
                onClick={() => reorder(index, -1)}>↑</button>
              <button className="mini-button" aria-label={`下移第 ${index + 1} 页`} disabled={busy || index === files.length - 1}
                onClick={() => reorder(index, 1)}>↓</button>
              <button className="mini-button" disabled={busy} onClick={() => setFiles(files.filter((_, i) => i !== index))}
                aria-label={`移除第 ${index + 1} 页`}>×</button>
            </li>)}
          </ol>
        )}
        {error && <Problem>{error}</Problem>}
        <div className="actions">
          <button className="button primary" disabled={busy || !files.length} onClick={() => void upload()}>
            {busy ? "正在提交…" : "开始分析"}<Icon name="arrow" size={17} />
          </button>
          {sample && <button className="button secondary" disabled={busy} onClick={() => void upload(true)}>试用合成样卷</button>}
        </div>
      </section>
      <aside className="panel ledger" aria-labelledby="history-title">
        <p className="eyebrow section-code"><Icon name="book" size={16} />学习的脚印，都在这里</p>
        <h2 id="history-title">最近作业</h2>
        {history.error && <Problem retry={history.reload}>{history.error}</Problem>}
        {!history.data && !history.error && <p className="muted" role="status">正在读取记录…</p>}
        {history.data?.length === 0 && <Empty title="还没有作业记录">上传后，可以在这里回看每一份批改报告。</Empty>}
        {history.data && history.data.length > 0 && <ul className="ledger-list">
          {history.data.map((entry) => <li key={entry.id}><button onClick={() => open(entry.id)}>
            <Icon name="mark" /><span>作业 · {dateText(entry.createdAt)}</span>
            <span className={`job-state ${entry.status}`}>{statusText[entry.status]}</span>
          </button></li>)}
        </ul>}
        <button className="text-button ledger-link" onClick={mastery}>查看知识点地图 <Icon name="arrow" size={16} /></button>
      </aside>
    </div>
  </>;
}
